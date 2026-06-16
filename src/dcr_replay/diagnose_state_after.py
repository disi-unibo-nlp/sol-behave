"""
diagnose_state_after.py
Settle whether the JS-side state_after is actually firing on every tx.

Run from /workdir/src/dcr_replay/.

Checks a synth-original result (which ran fast at 69 tx/s):
  - is state_after populated with DATA on most txs?  -> JS path IS running every tx
    (then 69 tx/s means it's actually fast enough, and historical's null was a
     different problem — maybe that contract had no view fns)
  - is state_after null/absent on most txs?          -> JS path is NOT running
    (the deployed deploy_server.js on disk is an OLDER version without the
     every-tx state_after, OR view_selectors didn't reach it)
"""
import json
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
synth_orig = SRC / "dcr_replay" / "synth_replay_results"

# Pick a contract known to have view fns (from earlier: ClaimRewards had 2)
print("Scanning synth-original results for state_after population...\n")
checked = 0
for p in sorted(synth_orig.glob("*/synth_replay_result_1000.json")):
    d = json.loads(p.read_text())
    if d.get("n_view_fns", None) == 0:
        continue
    txs = d.get("tx_results", [])
    if not txs:
        continue
    n = len(txs)
    sa_data = sum(1 for t in txs if t.get("state_after"))   # non-null, non-empty
    sa_null = sum(1 for t in txs if "state_after" in t and not t.get("state_after"))
    vs_data = sum(1 for t in txs if t.get("_view_state"))
    name = d.get("contract_name", "?")[:18]
    print(f"{name:<20} n={n:>4}  state_after_data={sa_data:>4}  "
          f"state_after_null={sa_null:>4}  _view_state={vs_data:>3}")
    checked += 1
    if checked >= 8:
        break

print()
print("Interpretation:")
print("  state_after_data ~= n   -> JS path runs every tx (and is fast enough)")
print("  state_after_data ~= 0   -> JS path NOT running; deploy_server.js on disk")
print("                             is the old version. Fast _view_state is the")
print("                             only snapshot source. Patch + rerun needed for")
print("                             historical scripts, but no need to disable JS.")