#!/usr/bin/env python3
"""
verify_ownership_drop.py
Test the hypothesis: the self-failures are caused by a privilege-dropping call
(renounceOwnership, or transferOwnership to a non-owner) appearing in the synth
sequence, after which all owner-only calls revert.

For each suspect, find the FIRST occurrence of renounceOwnership /
transferOwnership in the replayed order, and check whether failures cluster
AFTER that index. Run from /workdir/src/coverage_analysis/.
"""
import json
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
DCR_REPLAY = SRC / "dcr_replay" / "synth_replay_results"

SUSPECTS = {
    "MerryKekmas2023":  "0x0f689c9a84b9541ceed15693bdfd1419c2e9dc72",
    "NotFoundSeriesOne":"0x82c8c6231e7a4c40d014cb426a49b42863524c88",
    "NyanToken":        "0x0ebe30595a44e5288c24161ddfc1e9fa08e33a0c",
    "PurchaseContract": "0xbc962e2e290d276040babc4dfb8f9a4ae2a8085f",
    "RektWTF":          "0xf24cc30a5f8a98566d9109561dc90fd3d64d74c6",
    "WCANFTRanking":    "0x6b0774c4a0619113443bc5723db22799e9c51d9a",
    "iso3166SharesV1":  "0xb72749a16410db6b3feac000a5a90c2f6cf7935e",
}

DROP_FNS = {"renounceOwnership", "transferOwnership"}

def load(p):
    try: return json.loads(p.read_text())
    except Exception: return None

for name, addr in SUSPECTS.items():
    res = load(DCR_REPLAY / addr / "synth_replay_result_1000.json")
    if not res:
        print(f"{name}: no result"); continue
    txs = res.get("tx_results", [])

    # Find first privilege-dropping call in replay order
    first_drop_idx = None
    first_drop_fn  = None
    for i, t in enumerate(txs):
        fn = (t.get("_fn_sig", "") or "").split("(")[0]
        if fn in DROP_FNS:
            first_drop_idx = i
            first_drop_fn = fn
            break

    # Pass/fail before and after that index
    def status(t): return t.get("events_match", False)
    if first_drop_idx is not None:
        before = txs[:first_drop_idx]
        after  = txs[first_drop_idx:]
        pass_before = sum(1 for t in before if status(t))
        pass_after  = sum(1 for t in after  if status(t))
        print(f"\n{name}  pass_rate={res.get('pass_rate')}")
        print(f"  first privilege-drop: {first_drop_fn} at replay index {first_drop_idx}")
        print(f"  BEFORE drop: {pass_before}/{len(before)} pass ({pass_before/max(len(before),1):.0%})")
        print(f"  AFTER  drop: {pass_after}/{len(after)} pass ({pass_after/max(len(after),1):.0%})")
        # Is the drop call itself the LAST success?
        drop_tx = txs[first_drop_idx]
        print(f"  the drop call itself: {'PASS' if status(drop_tx) else 'FAIL'}")
    else:
        print(f"\n{name}  pass_rate={res.get('pass_rate')}")
        print(f"  no renounceOwnership/transferOwnership found in replay order")
        # then the cause is different — show first failing fn
        first_fail = next((t for t in txs if not status(t)), None)
        if first_fail:
            print(f"  first failing fn: {first_fail.get('_fn_sig')}")
            print(f"  reason: {(first_fail.get('revert_reason') or '')[:120]}")

print("\n" + "="*60)
print("If pass% drops sharply AFTER the privilege-drop call, the cause is")
print("STATEFUL ORDERING: the DCR sequence renounces/transfers ownership")
print("early, breaking all later owner-only calls. This is a property of")
print("the synthetic SEQUENCE, not a replay bug.")