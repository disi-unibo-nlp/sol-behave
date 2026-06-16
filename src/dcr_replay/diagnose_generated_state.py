"""
diagnose_generated_state.py
Confirm historical x generated (replay_generated.py output) has state_after
populated per-tx, completing the 2x2 matrix. These live in the repaired_oneshot
dir as replay_sample_{N}_result_{K}.json.

Run from /workdir/src/transaction_replay/ or anywhere.
"""
import json
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent if (Path(__file__).resolve().parent.name in ("dcr_replay","transaction_replay")) else Path("/workdir/src")
GEN = SRC / "source_generation" / "repaired_oneshot"

print(f"Scanning historical x generated results in {GEN}\n")
print(f"{'contract':<20} {'sample':>6} {'n_tx':>6} {'sa_data':>8} {'sa_null':>8}")
print("-" * 52)

checked = 0
total_files = 0
files_with_sa = 0
for p in sorted(GEN.glob("*/replay_sample_*_result_1000.json")):
    total_files += 1
    try:
        d = json.loads(p.read_text())
    except Exception:
        continue
    txs = d.get("tx_results", [])
    if not txs:
        continue
    n = len(txs)
    sa_data = sum(1 for t in txs if t.get("state_after"))
    sa_null = sum(1 for t in txs if "state_after" in t and not t.get("state_after"))
    if sa_data > 0:
        files_with_sa += 1
    name = d.get("contract_name", "?")[:18]
    idx  = d.get("sample_idx", "?")
    if checked < 15 and sa_data > 0:
        print(f"{name:<20} {str(idx):>6} {n:>6} {sa_data:>8} {sa_null:>8}")
        checked += 1

print()
print(f"TOTALS: {total_files} sample-result files, {files_with_sa} with populated state_after")
print()
if total_files and files_with_sa >= total_files * 0.7:
    print(">>> historical x generated HAS per-tx state_after. 2x2 matrix complete.")
    print(">>> All four replays ready. Go straight to comprehensive analysis.")
elif files_with_sa == 0:
    print(">>> historical x generated has NO state_after. This quadrant needs")
    print(">>> a rerun of replay_generated.py (the others are fine).")
else:
    print(">>> Partial coverage; some samples have snapshots, some don't.")