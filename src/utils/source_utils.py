"""
Filename: source_utils.py
"""

import os
import json
import re
import time
import shutil
import subprocess
from pathlib import Path

from storage import ContractWorkspace
import sys
from typing import Optional


def _find_sample_path(gen_contract_dir: Path, idx: int) -> Optional[Path]:
    for ext in (".json", ".sol"):
        p = gen_contract_dir / f"sample_{idx}{ext}"
        if p.exists():
            return p
    return None


def _load_flat_source(contract_dir: Path) -> str:
    """Return the full flat source (single string) for a contract directory."""
    jsn = contract_dir / "source.json"
    if jsn.exists():
        data = json.loads(jsn.read_text())
        return "\n\n".join(
            fc.get("content", "")
            for fc in data.get("sources", {}).values()
        )
    sol = contract_dir / "source.sol"
    if sol.exists():
        return sol.read_text()
    return ""


def _load_flat_sample(sample_path: Path, contract_name: str) -> str:
    """
    Return flat source for a generated sample.
    For multi-file (sample.json), concatenates all source files.
    """
    if sample_path.suffix == ".json":
        try:
            data = json.loads(sample_path.read_text())
            return "\n\n".join(
                fc.get("content", "")
                for fc in data.get("sources", {}).values()
            )
        except Exception:
            return ""
    return sample_path.read_text()


def _find_matching_brace(source: str, pos: int) -> int:
    depth, i, n = 0, pos, len(source)
    in_d = in_s = False
    while i < n:
        c = source[i]
        if (in_d or in_s) and c == '\\' and i+1 < n: i += 2; continue
        if c == '"' and not in_s: in_d = not in_d
        elif c == "'" and not in_d: in_s = not in_s
        elif not in_d and not in_s:
            if c == '{': depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0: return i
        i += 1
    return -1


def _find_functions(source: str) -> list[dict]:
    """
    Trova tutte le funzioni con body non vuoto nel sorgente.
    Ritorna lista di dict con:
        fn_start  : inizio della firma (incluso NatSpec precedente)
        body_open : posizione di '{' che apre il body
        body_close: posizione di '}' che chiude il body (incluso)
        fn_sig    : testo della firma
        natspec   : commenti NatSpec precedenti
    """
    fn_re = re.compile(
        r'\b(function)\s+\w+\s*\([^)]*\)[^;{]*',
        re.DOTALL,
    )
    results = []
    for m in fn_re.finditer(source):
        tail  = source[m.end():]
        semi  = tail.find(';')
        brace = tail.find('{')
        if brace == -1: continue
        if semi != -1 and semi < brace: continue

        abs_b = m.end() + brace
        close = _find_matching_brace(source, abs_b)
        if close == -1: continue

        # Body content (senza le graffe)
        body_content = source[abs_b + 1 : close]
        body_stripped = re.sub(r'//[^\n]*', '', body_content)
        body_stripped = re.sub(r'/\*.*?\*/', '', body_stripped, flags=re.DOTALL)
        body_stripped = body_stripped.strip()

        # Salta body vuoti e modifier
        if not body_stripped:
            continue

        # Cerca NatSpec precedente (righe /// o /** subito sopra la firma)
        prefix = source[:m.start()]
        natspec_lines = []
        for line in reversed(prefix.splitlines()):
            s = line.strip()
            if s.startswith('///') or s.startswith('*') or s.startswith('/**') or s.startswith('*/'):
                natspec_lines.insert(0, line)
            elif not s:
                continue
            else:
                break

        # fn_start include il NatSpec
        natspec_text = "\n".join(natspec_lines)
        fn_start = m.start() - len(natspec_text) - 1 if natspec_text else m.start()
        fn_start = max(0, fn_start)

        results.append({
            "fn_start":   fn_start,
            "body_open":  abs_b,
            "body_close": close + 1,
            "fn_sig":     m.group(0).strip(),
            "natspec":    natspec_text,
        })

    return results




def _main_source_text(contract_dir: Path) -> tuple[str, str | None]:
    """
    Restituisce (testo_file_principale, main_filepath_nel_json | None).

    Per contratti multi-file restituisce il contenuto completo del file
    principale (incluso SPDX, pragma, import, struct globali).
    Per single-file restituisce tutto il sorgente.
    """
    jsn = contract_dir / "source.json"
    if jsn.exists():
        data    = json.loads(jsn.read_text())
        sources = data.get("sources", {})
        meta    = _load_json(contract_dir / "metadata.json")
        cname   = meta.get("contract_name", "")
        # Trova il file principale per nome del contratto
        for fp, fc in sources.items():
            if cname and cname.lower() in Path(fp).stem.lower():
                return fc.get("content", ""), fp
        # Fallback: primo file
        if sources:
            fp, fc = next(iter(sources.items()))
            return fc.get("content", ""), fp
        return "", None
    sol = contract_dir / "source.sol"
    return (sol.read_text() if sol.exists() else ""), None


def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}

def _load_source(contract_dir: Path) -> str:
    """Carica il sorgente flat (per contratti single-file) o concatenato."""
    jsn = contract_dir / "source.json"
    sol = contract_dir / "source.sol"
    if jsn.exists():
        data = json.loads(jsn.read_text())
        return "\n\n".join(
            f"// === {fp} ===\n{fc.get('content','')}"
            for fp, fc in data.get("sources", {}).items()
        )
    if sol.exists():
        return sol.read_text()
    return ""