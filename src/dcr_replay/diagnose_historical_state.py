"""
diagnose_historical_state.py
Check whether historical x original (replay_transactions.py output) already
has state_after populated — same check we just ran on synthetic.

Run from /workdir/src/dcr_replay/ (or anywhere; paths are absolute via parent).

If historical results ALSO have state_after ~= n for contracts with view fns,
then NO rerun is needed at all — you already have per-tx snapshots on all the
replays that matter, and we can go straight to the comprehensive analysis.
"""
import json
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
dataset = SRC / "dataset" / "contracts_dataset_1to20"

print("Scanning historical x original results (replay_result_1000.json)...\n")
print(f"{'contract':<20} {'n_view':>6} {'n_tx':>6} {'sa_data':>8} {'sa_null':>8} {'_view_state':>11}")
print("-" * 64)

checked = 0
no_view_fns = 0
with_snaps = 0
for p in sorted(dataset.glob("*/replay_result_1000.json")):
    d = json.loads(p.read_text())
    n_view = d.get("n_view_fns", None)
    txs = d.get("tx_results", [])
    if not txs:
        continue
    n = len(txs)
    sa_data = sum(1 for t in txs if t.get("state_after"))
    sa_null = sum(1 for t in txs if "state_after" in t and not t.get("state_after"))
    vs_data = sum(1 for t in txs if t.get("_view_state"))
    name = d.get("contract_name", "?")[:18]

    if n_view == 0:
        no_view_fns += 1
    if sa_data > 0:
        with_snaps += 1

    # Only print the first ~15 with view fns to keep it readable
    if n_view and n_view > 0 and checked < 15:
        print(f"{name:<20} {str(n_view):>6} {n:>6} {sa_data:>8} {sa_null:>8} {vs_data:>11}")
        checked += 1

# Totals across ALL contracts
total = 0
total_with_view = 0
total_with_sa_data = 0
for p in dataset.glob("*/replay_result_1000.json"):
    d = json.loads(p.read_text())
    total += 1
    if d.get("n_view_fns", 0):
        total_with_view += 1
    txs = d.get("tx_results", [])
    if any(t.get("state_after") for t in txs):
        total_with_sa_data += 1

print()
print(f"TOTALS over {total} historical results:")
print(f"  contracts with >0 view fns          : {total_with_view}")
print(f"  contracts with populated state_after: {total_with_sa_data}")
print()
if total_with_sa_data >= total_with_view * 0.8 and total_with_view > 0:
    print(">>> Historical state_after IS populated. NO RERUN NEEDED.")
    print(">>> You already have per-tx snapshots on all four replays.")
elif total_with_sa_data == 0:
    print(">>> Historical state_after is EMPTY everywhere. Rerun needed:")
    print(">>> the historical runs predate the JS state_after being active.")
else:
    print(">>> Partial. Some historical runs have snapshots, some don't.")
    print(">>> Likely the JS fix landed mid-run; rerun the ones missing them.")