"""
pipeline.py

ContractPipeline: tutta la logica di processing per un singolo contratto,
con checkpoint granulare e ripresa da interruzione.

Flusso per contratto:
  NOT_STARTED
    → step 1: rimozione commenti          → save child_clean.sol
    → step 2: annotazione LLM funzioni    → save source_with_comments.sol
    → step 3: header LLM                  → (incluso in source_with_comments)
    → step 4: sostituzione body con TODO  → save stub.sol
  STUB_READY
    → step 5..5+k: generazione + compile  → save sample_N.sol
  GENERATING(completed_k=i)
    → riprende dal sample i+1
  DONE | FAILED

Se il processo viene interrotto durante GENERATING, alla ripresa
si salta direttamente al sample successivo senza rigenerare lo stub.
Se viene interrotto durante NOT_STARTED, si ricomincia dall'inizio
(idempotente perché i file intermedi vengono riscritti).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_client import LLMClient
    from storage import ContractWorkspace


import sys
sys.path.append("/workdir/src/utils/")
from source_utils import _find_matching_brace

# ─────────────────────────────────────────────────────────────────────────────
#  Costanti e configurazione
# ─────────────────────────────────────────────────────────────────────────────

# Token limits are defined in generate_batch.py and passed as parameters.
# Defaults here are used only when pipeline functions are called standalone.
MAX_TOKENS_COMMENTS = int(os.getenv("MAX_TOKENS_COMMENTS", "256"))
MAX_TOKENS_HEADER   = int(os.getenv("MAX_TOKENS_HEADER",   "512"))
MAX_TOKENS_COMPLETE = int(os.getenv("MAX_TOKENS_COMPLETE", "4096"))

_VALID_NATSPEC_TAGS = frozenset({
    "title", "author", "notice", "dev", "param", "return",
    "inheritdoc", "custom",
})
_EMPTY_RETURN = re.compile(
    r'^\s*///\s*@return\s+(?:'
    r'None|none|void|nothing|-|n/a|N/A'
    r'|[Nn]o return value\.?'
    r'|[Tt]he function does not return.*'
    r'|[Tt]his function does not return.*'
    r'|[Tt]he transaction receipt.*'
    r'|[Nn]ot applicable\.?'
    r'|[Nn]/[Aa]\.?'
    r'|[Tt]here is no return.*'
    r'|[Nn]othing.*'
    r')\s*$',
    re.IGNORECASE | re.MULTILINE,
)
_INVALID_TAG = re.compile(r'^\s*///\s*@(\w+)', re.MULTILINE)

# Pattern per @return senza nome del parametro su funzioni con return named
# es: `/// @return The number of tokens` quando la firma ha `returns (uint256 balance)`
# → "Documentation tag does not contain the name of its return parameter"
# Non possiamo fixarlo senza la firma, quindi rimuoviamo @return con descrizione lunga
# senza un token identificatore all'inizio (es. @return The ... → rimuovi)
_RETURN_NO_NAME = re.compile(
    r'^\s*///\s*@return\s+(?:The|the|A |An |Whether|If|True|False|Returns?)\s+.*$',
    re.MULTILINE,
)

def sanitize_natspec(source: str) -> str:
    """
    Rimuove/corregge tag NatSpec che causano errori di compilazione:
    1. Tag non validi (@emit, @event, @throws, ecc.)
    2. @return su void o con descrizione senza nome parametro
    3. @param senza nome → "No description given for param None"
    4. @param con nome che non esiste nella firma → rimuovi quel @param
       (es. @param None, @param _, @param account su funzione senza params)
    """
    lines, result = source.splitlines(), []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("///"):
            result.append(line)
            continue

        # @param senza nome o con nome invalido (None, _, numeri)
        m_param = re.match(r'^\s*///\s*@param\s*(\S*)', line)
        if m_param:
            param_name = m_param.group(1)
            # Rimuovi se: nessun nome, "None", "_", o inizia con numero
            if not param_name or param_name.lower() in ("none", "n/a", "_") \
               or param_name.startswith(("0","1","2","3","4","5","6","7","8","9")):
                continue
            # Spazio doppio = nome mancante, descrizione attaccata
            if re.match(r'^\s*///\s*@param\s{2,}', line):
                continue

        # Tag non validi
        m_tag = _INVALID_TAG.match(line)
        if m_tag and m_tag.group(1).lower() not in _VALID_NATSPEC_TAGS:
            continue

        cleaned = re.sub(r'\s*@(?!' + '|'.join(_VALID_NATSPEC_TAGS) + r')\w+\b[^@\n]*', '', line)
        result.append(cleaned)

    text = "\n".join(result)
    # Rimuovi @return void/nothing/ecc.
    text = _EMPTY_RETURN.sub('', text)
    # Rimuovi @return con descrizione che inizia con articolo (senza nome param)
    # es: "/// @return The balance" → causa "does not contain the name"
    text = _RETURN_NO_NAME.sub('', text)
    return text




from hardhat_utils import (
    hardhat_config      as _hu_hardhat_config,
    install_npm_deps    as _hu_install_npm_deps,
    detect_remappings   as _hu_detect_remappings,
    write_sources       as _hu_write_sources,
    load_source_texts   as _hu_load_source_texts,
    setup_workspace     as _hu_setup_workspace,
    prepare_and_compile as _hu_prepare_and_compile,
    extract_pragma_versions,
    primary_compiler_version,
)


def _hardhat_config(meta: dict, source_texts: list[str] | None = None) -> str:
    """Wrapper → hardhat_utils.hardhat_config (con multi-compiler support)."""
    return _hu_hardhat_config(meta, source_texts=source_texts or [])


def _npm_install(source: str, work_dir: Path, compiler_version: str = "",
                  all_sources: list[str] | None = None) -> None:
    """Wrapper → hardhat_utils.install_npm_deps."""
    texts = [source] + (all_sources or [])
    _hu_install_npm_deps(work_dir, texts, compiler_version=compiler_version)


def _get_indent(source: str, pos: int) -> str:
    start = source.rfind('\n', 0, pos)
    start = start + 1 if start != -1 else 0
    return "".join(c for c in source[start:pos] if c in (' ', '\t'))


def _extract_errors(output: str) -> str:
    lines = output.splitlines()
    err, capture = [], False
    kw = ("Error","Warning","TypeError","DeclarationError","SyntaxError",
          "ParserError","DocstringParsingError","HardhatError","Error HH")
    for line in lines:
        s = line.strip()
        if re.match(r'.+\.sol:\d+:', line) or s.startswith(kw): capture = True
        if capture:
            if s.startswith("at ") and "node_modules" in line: break
            err.append(line)
    if not err:
        err = [l for l in lines if l.strip() and "node_modules" not in l
               and not l.strip().startswith("at ")]
    return "\n".join(err[:60])


def _check_compile_result(cp) -> tuple[bool, str]:
    """Interpreta il risultato di hardhat compile."""
    if cp.returncode != 0:
        combined = cp.stdout + cp.stderr
        errors   = _extract_errors(combined)
        error_keywords = ("Error:", "TypeError:", "DeclarationError:",
                          "SyntaxError:", "ParserError:", "IOError:",
                          "CompilerError:", "Error HH", "HardhatError")
        only_warnings = all(
            "Warning:" in line and not any(kw in line for kw in error_keywords)
            for line in combined.splitlines()
            if line.strip() and not line.strip().startswith("at ")
               and "node_modules" not in line
               and line.strip() not in ("", "Compilation failed")
        )
        has_real_error = any(kw in combined for kw in error_keywords) and not only_warnings
        if has_real_error:
            return False, errors
    return True, ""


def try_compile(
    source: str, meta: dict, work_dir: Path,
    original_dir: Path | None = None,
    sample_json: Path | None = None,
) -> tuple[bool, str]:
    contracts_dir = work_dir / "contracts"
    if contracts_dir.exists(): shutil.rmtree(contracts_dir)
    contracts_dir.mkdir(parents=True)

    # Inietta SPDX se mancante
    if "SPDX-License-Identifier" not in source:
        source = "// SPDX-License-Identifier: MIT\n" + source

    # Caso 1: sample già salvato come JSON strutturato (multi-file)
    # Usiamo il source.json ORIGINALE come template per la struttura delle dipendenze,
    # e sostituiamo solo il file principale con il sorgente generato (`source`).
    # NON usiamo il sample_json come template perché il modello potrebbe aver generato
    # un flat file con import relativi che non funzionano fuori dalla struttura originale.
    if original_dir is not None:
        src_json = original_dir / "source.json"
        if src_json.exists():
            try:
                data = json.loads(src_json.read_text())
                sources = data.get("sources", {})
                if sources:
                    # Identifica il file principale (quello che contiene il contratto target)
                    # come il file con il nome più simile al contract_name o
                    # l'ultimo file non-libreria elencato.
                    cname   = meta.get("contract_name", "Contract")
                    main_fp = None
                    for fp in sources:
                        base = Path(fp).stem
                        if base.lower() == cname.lower() or cname in base:
                            main_fp = fp
                            break
                    if main_fp is None:
                        # fallback: primo file non-libreria
                        for fp in sources:
                            if not any(d in fp.lower() for d in
                                       ("lib/","interface","mock","test")):
                                main_fp = fp
                                break
                    if main_fp is None:
                        main_fp = list(sources.keys())[-1]

                    # Sostituisci il contenuto del file principale con il sorgente generato
                    new_sources = dict(sources)
                    new_sources[main_fp] = {"content": source}

                    # Scrivi tutti i file nel workspace con la struttura originale.
                    # Riscrivi gli import relativi ../../../ nei file di supporto
                    # in path assoluti @openzeppelin/... risolvibili via npm.
                    for fp, content in new_sources.items():
                        dest = contracts_dir / fp
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        text = content.get("content", "")
                        if fp != main_fp:
                            text = _rewrite_relative_imports(text, fp)
                        dest.write_text(text)

                    all_src_texts = [c.get("content", "") for c in new_sources.values()]
                    _npm_install(source, work_dir,
                                 compiler_version=meta.get("compiler_version", ""),
                                 all_sources=all_src_texts)
                    remaps = _hu_detect_remappings(work_dir, all_src_texts)
                    cfg = _hu_hardhat_config(meta, source_texts=all_src_texts,
                                             remappings=remaps or None)
                    (work_dir / "hardhat.config.js").write_text(cfg)

                    cp = subprocess.run(
                        ["npx", "hardhat", "compile", "--force"],
                        cwd=work_dir, capture_output=True, text=True,
                    )
                    return _check_compile_result(cp)
            except Exception as _e:
                import traceback as _tb
                print(f"      [try_compile multifile error] {_e}")
                if os.getenv("DEBUG_COMPILE"):
                    _tb.print_exc()

    # Fallback: contratto single-file, scrivi direttamente
    main = contracts_dir / "contract.sol"
    main.write_text(source)
    _npm_install(source, work_dir, compiler_version=meta.get("compiler_version", ""))
    remaps = _hu_detect_remappings(work_dir, [source])
    cfg = _hu_hardhat_config(meta, source_texts=[source], remappings=remaps or None)
    (work_dir / "hardhat.config.js").write_text(cfg)

    cp = subprocess.run(
        ["npx", "hardhat", "compile", "--force"],
        cwd=work_dir, capture_output=True, text=True,
    )
    return _check_compile_result(cp)


def _rewrite_relative_imports(content: str, file_path: str) -> str:
    """
    Riscrive gli import relativi ../../../ nei file di supporto copiati da Etherscan
    in path assoluti @openzeppelin/contracts/... che Hardhat può risolvere via npm.

    Esempio:
      import "../../../utils/Context.sol"
      → import "@openzeppelin/contracts/utils/Context.sol"

    Questo risolve HH408 (outside of project) e HH404 per file OZ con path relativi.
    """
    import re as _re

    def _resolve(m: re.Match) -> str:
        quote  = m.group(1)
        path   = m.group(2)
        if not path.startswith('../'):
            return m.group(0)

        # Normalizza il path relativo rispetto al file corrente
        base   = Path(file_path).parent
        target = (base / path).resolve()

        # Prova a identificare il package npm dal path normalizzato
        parts = target.parts
        # Cerca pattern come utils/Context.sol, token/ERC20/..., access/Ownable.sol
        _OZ_ROOTS = ('utils', 'token', 'access', 'governance', 'proxy',
                     'security', 'finance', 'metatx', 'interfaces')
        for i, part in enumerate(parts):
            if part in _OZ_ROOTS:
                rel = '/'.join(parts[i:])
                return f'import {quote}@openzeppelin/contracts/{rel}{quote}'

        # Fallback: restituisci l'import originale invariato
        return m.group(0)

    return re.sub(r'import\s*(["\'])(\.\.\/[^"\']+)(["\'])', _resolve, content)



    """Interpreta il risultato di hardhat compile."""
    if cp.returncode != 0:
        combined = cp.stdout + cp.stderr
        errors   = _extract_errors(combined)
        error_keywords = ("Error:", "TypeError:", "DeclarationError:",
                          "SyntaxError:", "ParserError:", "IOError:",
                          "CompilerError:", "Error HH", "HardhatError")
        only_warnings  = all(
            "Warning:" in line and not any(kw in line for kw in error_keywords)
            for line in combined.splitlines()
            if line.strip() and not line.strip().startswith("at ")
               and "node_modules" not in line
               and line.strip() not in ("", "Compilation failed")
        )
        has_real_error = any(kw in combined for kw in error_keywords) and not only_warnings
        if has_real_error:
            return False, errors
    return True, ""