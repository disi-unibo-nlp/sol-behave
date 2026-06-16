#!/usr/bin/env python3
"""
verify_fresh_data.py
Check whether the on-disk synth replay results reflect the ownership FIX,
or are stale (pre-fix). Run from /workdir/src/coverage_analysis/.

The tell: the 7 previously-self-failing contracts. Post-fix their
synth_orig pass_rate should be HIGH (>0.5, mostly ~1.0). If they're still
at 0.001-0.213, the replay results on disk are stale and the analysis read
old data.
"""
import json
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
SYNTH_ORIG = SRC / "dcr_replay" / "synth_replay_results"

SUSPECTS = {
    "NyanToken":        "0x0ebe30595a44e5288c24161ddfc1e9fa08e33a0c",
    "PurchaseContract": "0xbc962e2e290d276040babc4dfb8f9a4ae2a8085f",
    "NotFoundSeriesOne":"0x82c8c6231e7a4c40d014cb426a49b42863524c88",
    "iso3166SharesV1":  "0xb72749a16410db6b3feac000a5a90c2f6cf7935e",
    "MerryKekmas2023":  "0x0f689c9a84b9541ceed15693bdfd1419c2e9dc72",
    "WCANFTRanking":    "0x6b0774c4a0619113443bc5723db22799e9c51d9a",
    "RektWTF":          "0xf24cc30a5f8a98566d9109561dc90fd3d64d74c6",
}

def load(p):
    try: return json.loads(p.read_text())
    except Exception: return None

print(f"Reading from: {SYNTH_ORIG}\n")
print(f"{'contract':<20}{'pass_rate':>10}{'mtime':>22}   verdict")
print("-"*70)

import datetime
stale = 0
fresh = 0
for name, addr in SUSPECTS.items():
    p = SYNTH_ORIG / addr / "synth_replay_result_1000.json"
    if not p.exists():
        print(f"{name:<20}{'MISSING':>10}")
        continue
    d = load(p)
    pr = d.get("pass_rate") if d else None
    mtime = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    if pr is not None and pr >= 0.5:
        verdict = "FRESH (fixed)"
        fresh += 1
    else:
        verdict = "STALE (pre-fix)"
        stale += 1
    print(f"{name:<20}{(f'{pr:.3f}' if pr is not None else 'n/a'):>10}{mtime:>22}   {verdict}")

print()
if stale == 0:
    print(">>> All suspects FRESH. On-disk replay data reflects the fix.")
    print(">>> If the CSV still shows old numbers, the CSV/analysis wasn't re-run.")
    print(">>> Action: rerun comprehensive_analysis.py to regenerate the CSV.")
elif fresh == 0:
    print(">>> All suspects STALE. The replays were NOT re-run against new synth txs.")
    print(">>> Likely cause: replay ran without --force and skipped DONE checkpoints,")
    print(">>>   OR wrote to a different --output-dir than the analysis reads.")
    print(">>> Action: rerun replay_synth_transactions.py + replay_synth_generated.py")
    print(">>>   with --force, then re-run the analysis.")
else:
    print(f">>> MIXED: {fresh} fresh, {stale} stale. Partial rerun.")

# Also report the directory mtime spread to see when results were last written
print(f"\nMost recent result file mtimes in {SYNTH_ORIG.name}:")
allres = sorted(SYNTH_ORIG.glob("*/synth_replay_result_1000.json"),
                key=lambda p: p.stat().st_mtime, reverse=True)[:3]
for p in allres:
    mt = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    print(f"  {mt}  {p.parent.name[:16]}")