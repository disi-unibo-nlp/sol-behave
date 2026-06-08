"""
blacklist.py  —  blacklist condivisa tra seed_from_etherscan.py e crawl.py.

Tiene traccia di tutti gli indirizzi già visti, con il motivo per cui
sono stati scartati o inclusi.  Questo permette di eseguire scan
incrementali su range di blocchi diversi senza riprocessare gli stessi
indirizzi.

Formato del file (blacklist.json):
{
  "updated": "2024-01-01T00:00:00",
  "total": 12345,
  "entries": {
    "0xabc...": {"reason": "proxy",          "ts": "..."},
    "0xdef...": {"reason": "not_verified",   "ts": "..."},
    "0x123...": {"reason": "low_tx",         "ts": "..."},
    "0x456...": {"reason": "compile_failed", "ts": "..."},
    "0x789...": {"reason": "saved",          "ts": "..."},
    "0xaaa...": {"reason": "low_pass_rate",  "ts": "..."},
  }
}

Motivi standard
───────────────
  proxy           — contratto proxy, nessun sorgente utile
  not_verified    — nessun sorgente verificato su Etherscan
  not_contract    — è un EOA, non un contratto (da eth_getCode)
  low_tx          — meno di min_tx transazioni storiche
  eoa             — indirizzo wallet, non contratto
  compile_failed  — compilazione Hardhat fallita
  deploy_failed   — deploy fresh fallito anche con argomenti LLM
  low_pass_rate   — pass_rate < soglia dopo il replay
  saved           — salvato nel dataset con successo
  candidate       — trovato come candidato, non ancora processato da crawl
"""

import json
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Blacklist:
    """
    Blacklist persistente su disco.  Thread-unsafe (single-process).

    Uso tipico:
        bl = Blacklist(Path("blacklist.json"))

        if bl.contains(addr):
            continue

        bl.add(addr, "proxy")       # scarta
        bl.add(addr, "saved")       # includi nel dataset
        bl.save()                   # flush su disco
    """

    def __init__(self, path: Path, autosave_every: int = 50):
        self._path         = path
        self._autosave_n   = autosave_every
        self._dirty        = 0
        self._entries: dict[str, dict] = {}
        self._load()

    # ── Persistenza ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._entries = data.get("entries", {})
                print(f"  [blacklist] loaded {len(self._entries):,} entries"
                      f" from {self._path}")
            except Exception as e:
                print(f"  [blacklist] ⚠ could not load {self._path}: {e}")
                self._entries = {}
        else:
            self._entries = {}

    def save(self) -> None:
        self._path.write_text(json.dumps({
            "updated": _now(),
            "total":   len(self._entries),
            "entries": self._entries,
        }, indent=2))
        self._dirty = 0

    def _maybe_autosave(self) -> None:
        self._dirty += 1
        if self._dirty >= self._autosave_n:
            self.save()

    # ── API ───────────────────────────────────────────────────────────────────

    def contains(self, address: str) -> bool:
        """True se l'indirizzo è già nella blacklist (qualunque motivo)."""
        return address.lower() in self._entries

    def get_reason(self, address: str) -> str | None:
        entry = self._entries.get(address.lower())
        return entry["reason"] if entry else None

    def add(self, address: str, reason: str, extra: dict | None = None) -> None:
        """Aggiunge o aggiorna un indirizzo."""
        entry = {"reason": reason, "ts": _now()}
        if extra:
            entry.update(extra)
        self._entries[address.lower()] = entry
        self._maybe_autosave()

    def addresses(self) -> set[str]:
        """Restituisce l'insieme di tutti gli indirizzi nella blacklist."""
        return set(self._entries.keys())

    def stats(self) -> dict[str, int]:
        """Conta per motivo."""
        from collections import Counter
        return dict(Counter(e["reason"] for e in self._entries.values()))

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, address: str) -> bool:
        return self.contains(address)