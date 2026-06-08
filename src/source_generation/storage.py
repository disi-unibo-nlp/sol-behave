"""
storage.py

Gestione della struttura su disco per un singolo contratto nel pipeline
di generazione e valutazione.

Layout directory generata (<generated_dir>/<address>/):
    child_clean.sol        — sorgente originale senza commenti
    stub.sol               — body sostituiti con // TODO: implement this
    renames.json           — mapping {nome_orig: nome_rinominato} (se presente)
    completion_prompt.json — prompt di completamento (system + user), salvato UNA volta
    sample_1.sol ... sample_k.sol  — contratti generati
    result.json            — compile@k finale + dettaglio per sample
    progress.json          — checkpoint (stato + compile_results per sample)

Layout directory di input (<contract_dir>/):
    source_with_comments_<model>.sol  — child con NatSpec aggiunti (input per DCR)

progress.json:
  {
    "state":          "NOT_STARTED" | "STUB_READY" | "GENERATING" | "DONE" | "FAILED",
    "completed_k":    <int>,
    "k":              <int>,
    "error":          <str|null>,
    "compile_results": {
      "1": {"compiled": true,  "todos_left": 0},
      "2": {"compiled": false, "todos_left": 2, "error": "..."},
      ...
    }
  }
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_PROGRESS_FILE        = "progress.json"
_COMPLETION_PROMPT    = "completion_prompt.json"
_RESULT_FILE          = "result.json"

STATES = frozenset({
    "NOT_STARTED",
    "STUB_READY",
    "GENERATING",
    "DONE",
    "FAILED",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _model_slug(model_name: str) -> str:
    """Converte un nome modello in un suffisso file sicuro."""
    slug = re.sub(r'[^a-zA-Z0-9_-]', '_', model_name)
    return slug[:60]   # tronca se troppo lungo


class ContractWorkspace:
    """
    Interfaccia per leggere e scrivere tutti i file associati a un contratto
    nel pipeline di generazione. Nasconde la logica dei path.

    contract_dir: directory di INPUT (nel dataset originale, es. contracts_dataset_pass_08/0x...)
    generated_dir: directory di OUTPUT (es. generated_compile_k/0x...)
    """

    def __init__(self, generated_dir: Path, contract_dir: Path | None = None):
        self._dir         = generated_dir
        self._contract_dir = contract_dir  # per source_with_comments
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── Path helpers ──────────────────────────────────────────────────────────

    @property
    def directory(self) -> Path:
        return self._dir

    def _path(self, filename: str) -> Path:
        return self._dir / filename

    def sample_path(self, i: int) -> Path:
        """Path del sample i-esimo. Usa .sol per contratti single-file, .json per multi-file."""
        ext = "json" if self.is_multifile else "sol"
        # Supporta anche la presenza di file legacy .sol per contratti multi-file
        legacy = self._dir / f"sample_{i}.sol"
        if self.is_multifile and legacy.exists():
            return legacy  # backward compat: riusa il .sol già scritto
        return self._dir / f"sample_{i}.{ext}"

    @property
    def is_multifile(self) -> bool:
        """True se il contratto originale ha un source.json multi-file."""
        if self._contract_dir is None:
            return False
        src_json = self._contract_dir / "source.json"
        if not src_json.exists():
            return False
        try:
            data = json.loads(src_json.read_text())
            return len(data.get("sources", {})) > 1
        except Exception:
            return False

    # ── Progress / checkpoint ─────────────────────────────────────────────────

    @property
    def progress(self) -> dict:
        p = self._path(_PROGRESS_FILE)
        if not p.exists():
            return {"state": "NOT_STARTED", "completed_k": 0, "k": 0,
                    "error": None, "compile_results": {}}
        try:
            data = json.loads(p.read_text())
            if "compile_results" not in data:
                data["compile_results"] = {}
            return data
        except Exception:
            return {"state": "NOT_STARTED", "completed_k": 0, "k": 0,
                    "error": None, "compile_results": {}}

    @property
    def state(self) -> str:
        return self.progress.get("state", "NOT_STARTED")

    def set_state(
        self,
        state:       str,
        completed_k: int | None = None,
        k:           int | None = None,
        error:       str | None = None,
        **extra: Any,
    ) -> None:
        assert state in STATES, f"Invalid state: {state!r}. Must be one of {STATES}"
        current = self.progress
        update = {
            "state":           state,
            "completed_k":     completed_k if completed_k is not None else current.get("completed_k", 0),
            "k":               k           if k           is not None else current.get("k", 0),
            "error":           error,
            "updated_at":      _now(),
            "compile_results": current.get("compile_results", {}),
            **extra,
        }
        self._path(_PROGRESS_FILE).write_text(json.dumps(update, indent=2))

    def record_compile_result(
        self, i: int, compiled: bool,
        todos_left: int = 0, error: str | None = None,
    ) -> None:
        """
        Aggiorna compile_results[str(i)] nel progress.json.
        Chiamato dopo ogni tentativo di compilazione, indipendentemente
        dallo stato generale del pipeline.
        """
        current = self.progress
        results = current.get("compile_results", {})
        results[str(i)] = {
            "compiled":   compiled,
            "todos_left": todos_left,
        }
        if error:
            results[str(i)]["error"] = error[:300]   # tronca errori lunghissimi
        current["compile_results"] = results
        current["updated_at"] = _now()
        self._path(_PROGRESS_FILE).write_text(json.dumps(current, indent=2))

    def compiled_samples(self) -> list[int]:
        """Restituisce gli indici (1-based) dei sample che hanno compilato."""
        return [
            int(k) for k, v in self.progress.get("compile_results", {}).items()
            if v.get("compiled")
        ]

    def is_done(self) -> bool:
        return self.state in ("DONE", "FAILED")

    def is_stub_ready(self) -> bool:
        return self.state in ("STUB_READY", "GENERATING", "DONE")

    def next_sample_index(self) -> int:
        """Restituisce il prossimo sample da generare (1-based)."""
        return self.progress.get("completed_k", 0) + 1

    # ── Source files (in generated_dir) ───────────────────────────────────────

    def save_child_clean(self, text: str) -> None:
        """Sorgente originale senza commenti (output step 1)."""
        self._path("child_clean.sol").write_text(text)

    def load_child_clean(self) -> str | None:
        p = self._path("child_clean.sol")
        return p.read_text() if p.exists() else None

    def save_stub(self, text: str, renames: dict | None = None) -> None:
        """Stub con TODO (output step 4). Salva anche renames se presenti."""
        self._path("stub.sol").write_text(text)
        if renames:
            self._path("renames.json").write_text(json.dumps(renames, indent=2))

    def save_preamble(self, text: str) -> None:
        """Salva il preamble (pragma + imports + base contracts) separatamente.
        Usato in fase 2 per assemblare il sample senza ricaricare il sorgente."""
        self._path("preamble.sol").write_text(text)

    def load_preamble(self) -> str:
        p = self._path("preamble.sol")
        return p.read_text() if p.exists() else ""

    def load_stub(self) -> str | None:
        p = self._path("stub.sol")
        return p.read_text() if p.exists() else None

    def load_renames(self) -> dict:
        p = self._path("renames.json")
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}

    # ── source_with_comments (in contract_dir, con suffisso modello) ──────────

    def save_source_with_comments(self, text: str, model_name: str) -> Path:
        """
        Salva il child con NatSpec aggiunti nella directory di INPUT del contratto,
        con il nome del modello come suffisso.

        Es: contracts_dataset_pass_08/0xabc.../source_with_comments_Qwen3_14B_AWQ.sol

        Questo file viene ri-usato per:
          1. Generare il DCR graph (non dipende dal modello che ha generato lo stub)
          2. Rigenerare con un altro modello senza rieseguire steps 1-3

        Ritorna il path del file salvato.
        """
        target_dir = self._contract_dir if self._contract_dir else self._dir
        slug = _model_slug(model_name)
        dest = target_dir / f"source_with_comments_{slug}.sol"
        dest.write_text(text)
        return dest

    def load_source_with_comments(self, model_name: str) -> str | None:
        """Carica source_with_comments per il modello specificato."""
        target_dir = self._contract_dir if self._contract_dir else self._dir
        slug = _model_slug(model_name)
        p = target_dir / f"source_with_comments_{slug}.sol"
        return p.read_text() if p.exists() else None

    def find_any_source_with_comments(self) -> str | None:
        """
        Carica il primo source_with_comments disponibile, indipendentemente
        dal modello. Utile quando si vuole riutilizzare un file esistente
        senza sapere con quale modello è stato generato.
        """
        target_dir = self._contract_dir if self._contract_dir else self._dir
        for p in sorted(target_dir.glob("source_with_comments_*.sol")):
            return p.read_text()
        return None

    # ── Completion prompt (unico per contratto, riusabile con altri modelli) ──

    def save_completion_prompt(self, system: str, user: str) -> None:
        """
        Salva il prompt di completamento UNA sola volta.
        Il prompt è lo stesso per tutti i k sample e per tutti i modelli:
        cambia solo chi risponde, non la domanda.
        Non sovrascrive se già presente (idempotente).
        """
        p = self._path(_COMPLETION_PROMPT)
        if p.exists():
            return   # già salvato da un run precedente
        p.write_text(json.dumps({
            "system":   system,
            "user":     user,
            "saved_at": _now(),
        }, indent=2))

    def load_completion_prompt(self) -> dict | None:
        """
        Carica il prompt di completamento salvato.
        Ritorna {"system": ..., "user": ...} oppure None se non presente.
        """
        p = self._path(_COMPLETION_PROMPT)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except Exception:
            return None

    # ── Samples ───────────────────────────────────────────────────────────────

    def save_sample(self, i: int, source: str) -> None:
        """
        Salva il sample i-esimo su disco.
        - Single-file: scrive il sorgente assemblato in sample_N.sol
        - Multi-file:  inietta il sorgente nel source.json originale e
                       salva la struttura completa in sample_N.json
        """
        if self.is_multifile and self._contract_dir is not None:
            try:
                src_json = self._contract_dir / "source.json"
                data     = json.loads(src_json.read_text())
                sources  = data.get("sources", {})
                cname    = self._meta_contract_name()

                # Identifica il file principale
                main_fp = None
                for fp in sources:
                    base = Path(fp).stem
                    if base.lower() == cname.lower() or cname in base:
                        main_fp = fp; break
                if main_fp is None:
                    for fp in sources:
                        if not any(d in fp.lower() for d in
                                   ("lib/", "interface", "mock", "test")):
                            main_fp = fp; break
                if main_fp is None:
                    main_fp = list(sources.keys())[-1]

                new_sources = dict(sources)
                new_sources[main_fp] = {"content": source}
                data["sources"] = new_sources
                self.sample_path(i).write_text(json.dumps(data, indent=2))
                return
            except Exception:
                pass  # fallback a single-file

        self.sample_path(i).write_text(source)

    def load_sample(self, i: int) -> str | None:
        """
        Carica il sorgente del sample i-esimo.
        Per multi-file restituisce il contenuto del file principale.
        """
        p = self.sample_path(i)
        if not p.exists():
            return None
        text = p.read_text()
        if p.suffix == ".json":
            try:
                data    = json.loads(text)
                sources = data.get("sources", {})
                cname   = self._meta_contract_name()
                for fp, content in sources.items():
                    base = Path(fp).stem
                    if base.lower() == cname.lower() or cname in base:
                        return content.get("content", "")
                # fallback: ultimo file
                return list(sources.values())[-1].get("content", "")
            except Exception:
                pass
        return text

    def _meta_contract_name(self) -> str:
        """Legge contract_name dai metadata del contratto originale."""
        if self._contract_dir is None:
            return "Contract"
        try:
            meta = json.loads((self._contract_dir / "metadata.json").read_text())
            return meta.get("contract_name", "Contract")
        except Exception:
            return "Contract"

    def existing_samples(self) -> list[int]:
        """Restituisce gli indici (1-based) dei sample già scritti su disco."""
        indices = set()
        for ext in ("sol", "json"):
            for p in self._dir.glob(f"sample_*.{ext}"):
                try:
                    idx = int(p.stem.split("_")[1])
                    indices.add(idx)
                except (IndexError, ValueError):
                    pass
        return sorted(indices)

    # ── Result ────────────────────────────────────────────────────────────────

    def save_result(self, result: dict) -> None:
        self._path(_RESULT_FILE).write_text(json.dumps(result, indent=2))

    def load_result(self) -> dict | None:
        p = self._path(_RESULT_FILE)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except Exception:
            return None

    # ── Convenience ───────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return f"ContractWorkspace({self._dir}, state={self.state!r})"