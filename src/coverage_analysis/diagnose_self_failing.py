#!/usr/bin/env python3
"""
diagnose_self_failing.py
========================
Investigate contracts whose ORIGINAL implementation fails its OWN synthetic
transactions (synth_orig_pass < 0.5). This shouldn't happen if synthetic txs
are validated at generation time, so it's a threat to validity that must be
explained before the discriminability claim holds.

For each suspect contract it reports:
  - constructor args used at DCR generation (deploy_params.json) vs at replay
    (synth_replay_result) — a mismatch is the prime suspect.
  - the dominant failure reason on the failing synthetic txs (revert_reason).
  - which functions fail, and whether failures cluster on specific _event_id /
    _branch_guard (points to a DCR path that doesn't reproduce on fresh deploy).
  - whether failures look state-dependent (e.g. need a balance/allowance the
    fresh deploy lacks) vs nondeterministic (timestamp/block).

Run from /workdir/src/coverage_analysis/.
"""
import json
from collections import Counter
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
SRC = _SCRIPT_DIR.parent

DATASET    = SRC / "dataset" / "contracts_dataset_1to20"
DCR_REPLAY = SRC / "dcr_replay" / "synth_replay_results"
DCR_GEN    = SRC / "dcr_generation" / "generated_dcr_1to20"

# The 7 self-failing originals from the CSV (synth_orig_pass < 0.5).
# We resolve them by name->address from the replay results to stay robust.
SUSPECT_NAMES = {
    "NyanToken", "MerryKekmas2023", "WCANFTRanking", "NotFoundSeriesOne",
    "iso3166SharesV1", "PurchaseContract", "RektWTF",
}


def load(p):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def find_suspects():
    """Map suspect contract name -> address by scanning synth replay results."""
    found = {}
    for p in DCR_REPLAY.glob("*/synth_replay_result_1000.json"):
        d = load(p)
        if not d:
            continue
        name = d.get("contract_name", "")
        if name in SUSPECT_NAMES and d.get("pass_rate", 1.0) < 0.5:
            found[name] = p.parent.name
    return found


def analyse(name, addr):
    print(f"\n{'='*70}")
    print(f"  {name}   ({addr})")
    print(f"{'='*70}")

    synth_res = load(DCR_REPLAY / addr / "synth_replay_result_1000.json")
    if not synth_res:
        print("  no synth replay result")
        return

    pass_rate = synth_res.get("pass_rate")
    print(f"  synth_orig pass_rate : {pass_rate}")
    print(f"  passed/failed        : {synth_res.get('passed')}/{synth_res.get('failed')}")

    # Constructor args comparison
    replay_args = synth_res.get("constructor_args")
    dp = load(DATASET / addr / "deploy_params.json")
    gen_args = dp.get("constructor_args") if dp else None
    print(f"\n  constructor args @ replay : {replay_args}")
    print(f"  constructor args @ DCR gen: {gen_args}")
    if gen_args is not None and replay_args is not None and gen_args != replay_args:
        print(f"  ⚠  CONSTRUCTOR ARGS MISMATCH — prime suspect for self-failure")
    elif gen_args == replay_args:
        print(f"  ✓  constructor args match")

    # Failure analysis on tx_results
    txs = synth_res.get("tx_results", [])
    failed = [t for t in txs if not t.get("events_match", False)]
    print(f"\n  failing txs: {len(failed)}/{len(txs)}")

    # Dominant revert reasons
    reasons = Counter()
    for t in failed:
        r = (t.get("revert_reason") or t.get("error") or "").strip()
        # normalise: take first 60 chars
        reasons[r[:60] or "(empty)"] += 1
    print(f"  top failure reasons:")
    for r, c in reasons.most_common(5):
        print(f"    {c:>4}× {r}")

    # Which functions fail
    fn_fail = Counter(t.get("_fn_sig", "?").split("(")[0] for t in failed)
    print(f"  failing functions:")
    for fn, c in fn_fail.most_common(8):
        print(f"    {c:>4}× {fn}")

    # Cluster on event_id / branch_guard
    ev = Counter(t.get("_event_id", "") for t in failed if t.get("_event_id"))
    if ev:
        print(f"  failing DCR event_ids (top):")
        for e, c in ev.most_common(5):
            print(f"    {c:>4}× {e}")

    # Heuristic classification
    reason_blob = " ".join((t.get("revert_reason") or t.get("error") or "").lower()
                           for t in failed)
    print(f"\n  heuristic:")
    if gen_args != replay_args and gen_args is not None:
        print(f"    → likely CONSTRUCTOR MISMATCH (re-deploy with gen args & recheck)")
    elif any(w in reason_blob for w in ("balance", "allowance", "insufficient", "exceeds")):
        print(f"    → likely STATE-DEPENDENT (needs balance/allowance fresh deploy lacks)")
    elif any(w in reason_blob for w in ("timestamp", "block", "deadline", "expired")):
        print(f"    → likely NONDETERMINISTIC (time/block dependent)")
    elif any(w in reason_blob for w in ("owner", "caller", "auth", "only")):
        print(f"    → likely ACCESS CONTROL (msg.sender differs on fresh deploy)")
    else:
        print(f"    → UNCLEAR — inspect reasons above manually")


def main():
    print("Resolving suspect contracts ...")
    suspects = find_suspects()
    print(f"  found {len(suspects)}/{len(SUSPECT_NAMES)}: {list(suspects.keys())}")
    if len(suspects) < len(SUSPECT_NAMES):
        missing = SUSPECT_NAMES - set(suspects)
        print(f"  (not resolved by name<0.5 scan: {missing} — "
              f"may have different pass_rate now)")

    for name, addr in sorted(suspects.items()):
        analyse(name, addr)

    print(f"\n{'='*70}")
    print("  SUMMARY GUIDANCE")
    print(f"{'='*70}")
    print("""  - CONSTRUCTOR MISMATCH  → fixable: rerun synth replay for these addrs
                            after aligning deploy_params; self-failure vanishes.
  - STATE-DEPENDENT       → expected limitation; exclude from discriminability
                            or note that fresh-deploy lacks prerequisite state.
  - NONDETERMINISTIC      → exclude these view fns / txs; document as threat.
  - ACCESS CONTROL        → the DCR sequence used a caller not reproducible on
                            fresh deploy; document and exclude.
  Any contract that self-fails synthetic MUST be excluded from the
  discriminability denominator, or the 'synthetic caught a bug' claim is
  confounded (synthetic also breaks the original).""")


if __name__ == "__main__":
    main()