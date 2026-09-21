#!/usr/bin/env python3
"""
apply_analyzer_label.py
=======================
Generalizes analyse_synth_replay_depth.py so it can be used for BOTH historical
and synthetic replay analysis:
  - adds a --label CLI flag (default "REPLAY")
  - parameterizes the printed title and the "oracle catches these" lines
  - writes the label into the output JSON

Backward compatible: without --label the output reads "REPLAY DEPTH".

Usage:
  python3 apply_analyzer_label.py /workdir/src/dcr_replay/analyse_synth_replay_depth.py
"""
import shutil, sys
from datetime import datetime
from pathlib import Path

DEFAULT = "/workdir/src/dcr_replay/analyse_synth_replay_depth.py"

def replace_once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"[FAIL] '{label}': expected 1 match, found {n}. Already patched?")
    return text.replace(old, new)

def main():
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(DEFAULT)
    if not target.exists():
        raise SystemExit(f"[FAIL] not found: {target}")
    src = target.read_text()
    if "--label" in src or "def print_summary(rows, agg, k, label" in src:
        raise SystemExit("[SKIP] already has --label. Nothing done.")

    bak = target.with_suffix(target.suffix + "." + datetime.now().strftime("%Y%m%d_%H%M%S") + ".bak")
    shutil.copy2(target, bak); print(f"[ok] backup: {bak}")

    # 1. print_summary signature takes label
    src = replace_once(src,
        "def print_summary(rows, agg, k):",
        'def print_summary(rows, agg, k, label="REPLAY"):',
        "print_summary signature")

    # 2. parameterized title
    src = replace_once(src,
        '    print(f"\\n  SYNTHETIC REPLAY DEPTH — original vs generated  (K={k})")',
        '    print(f"\\n  {label} DEPTH — original vs generated  (K={k})")',
        "title")

    # 3. parameterized "oracle catches these" in aggregates
    src = replace_once(src,
        '    print(f"  Status DIVERGED (mean<100%)     : {agg[\'n_status_diverged\']}  ← synthetic oracle catches these")',
        '    print(f"  Status DIVERGED (mean<100%)     : {agg[\'n_status_diverged\']}  ← oracle catches these")',
        "diverged line")

    # 4. parameterized diverged-contracts header
    src = replace_once(src,
        '        print("\\n  Diverged contracts (synthetic oracle flags these):")',
        '        print("\\n  Diverged contracts (oracle flags these):")',
        "diverged header")

    # 5. CLI --label
    src = replace_once(src,
        '    p.add_argument("--output", type=Path, default=_SCRIPT_DIR / "synth_replay_depth_results.json")',
        '    p.add_argument("--label", default="REPLAY",\n'
        '                   help="Title shown in the printed table, e.g. HISTORICAL or SYNTHETIC.")\n'
        '    p.add_argument("--output", type=Path, default=_SCRIPT_DIR / "replay_depth_results.json")',
        "CLI --label")

    # 6. write label into JSON + pass to print_summary
    src = replace_once(src,
        '    agg = compute_aggregates(rows)\n'
        '    a.output.write_text(json.dumps({"k": a.k, "aggregates": agg, "contracts": rows}, indent=2))\n'
        '    print(f"Results written to {a.output}")\n'
        '    print_summary(rows, agg, a.k)',
        '    agg = compute_aggregates(rows)\n'
        '    a.output.write_text(json.dumps({\n'
        '        "label":      a.label,\n'
        '        "k":          a.k,\n'
        '        "orig_dir":   str(a.orig_dir),\n'
        '        "gen_dir":    str(a.gen_dir),\n'
        '        "aggregates": agg,\n'
        '        "contracts":  rows,\n'
        '    }, indent=2))\n'
        '    print(f"Results written to {a.output}")\n'
        '    print_summary(rows, agg, a.k, a.label)',
        "main: label into JSON")

    target.write_text(src)
    import py_compile; py_compile.compile(str(target), doraise=True)
    print(f"[ok] patched + compiles: {target}")
    print("\nNow supports:  --label HISTORICAL   /   --label SYNTHETIC")

if __name__ == "__main__":
    main()