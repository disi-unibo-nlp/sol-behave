#!/usr/bin/env python3
"""
verify_owner_mechanism.py
Confirm the self-failure is an owner-binding gap: the failing owner-only
synthetic txs have a `from` that is NOT the fresh-deploy owner (0xf39F...).

If confirmed, the fix is to rewrite `from` -> OWNER_ADDR for those txs at
replay time (or to set the contract owner to the tx sender). Run from
/workdir/src/coverage_analysis/.
"""
import json
from collections import Counter
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
DCR_REPLAY = SRC / "dcr_replay" / "synth_replay_results"
DCR_GEN    = SRC / "dcr_generation" / "generated_dcr_1to20"
OWNER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266".lower()

SUSPECTS = {
    "MerryKekmas2023":  "0x0f689c9a84b9541ceed15693bdfd1419c2e9dc72",
    "NotFoundSeriesOne":"0x82c8c6231e7a4c40d014cb426a49b42863524c88",
    "NyanToken":        "0x0ebe30595a44e5288c24161ddfc1e9fa08e33a0c",
    "PurchaseContract": "0xbc962e2e290d276040babc4dfb8f9a4ae2a8085f",
    "RektWTF":          "0xf24cc30a5f8a98566d9109561dc90fd3d64d74c6",
    "WCANFTRanking":    "0x6b0774c4a0619113443bc5723db22799e9c51d9a",
    "iso3166SharesV1":  "0xb72749a16410db6b3feac000a5a90c2f6cf7935e",
}

def load(p):
    try: return json.loads(p.read_text())
    except Exception: return None

for name, addr in SUSPECTS.items():
    res = load(DCR_REPLAY / addr / "synth_replay_result_1000.json")
    if not res:
        print(f"{name}: no result"); continue
    txs = res.get("tx_results", [])
    failed = [t for t in txs if not t.get("events_match", False)]
    passed = [t for t in txs if t.get("events_match", False)]

    # distribution of `from` on failing vs passing txs
    from_failed = Counter((t.get("from") or "").lower() for t in failed)
    from_passed = Counter((t.get("from") or "").lower() for t in passed)

    failed_from_owner = sum(c for f, c in from_failed.items() if f == OWNER)
    failed_from_other = sum(c for f, c in from_failed.items() if f != OWNER)

    print(f"\n{name} ({addr[:10]})  pass_rate={res.get('pass_rate')}")
    print(f"  failing txs from OWNER  : {failed_from_owner}")
    print(f"  failing txs from NON-own: {failed_from_other}")
    print(f"  distinct senders (fail) : {len(from_failed)}")
    # Show top 3 failing senders
    for f, c in from_failed.most_common(3):
        tag = "  <-- OWNER" if f == OWNER else ""
        print(f"    {c:>4}× {f or '(none)'}{tag}")
    # Check the original synthetic tx file to see what `from` was intended
    synth = load(DCR_GEN / addr / "transactions_synthetic.json")
    if synth:
        froms = Counter((t.get("from") or "").lower() for t in synth)
        print(f"  synthetic file distinct froms: {len(froms)}")
        for f, c in froms.most_common(2):
            tag = "  <-- OWNER" if f == OWNER else ""
            print(f"    {c:>4}× {f or '(none)'}{tag}")

print("\n" + "="*60)
print("If failing txs are mostly from NON-owner senders, the fix is to")
print("rewrite from->OWNER for owner-only calls at replay (or impersonate")
print("the actual owner). That recovers these contracts.")