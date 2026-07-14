#!/usr/bin/env python3
"""
additional_stats.py
=================

Mines artifacts already on disk to answer four reviewer questions. No new
experiments: everything below is recovered from files the pipeline already wrote.

  Q3 — Pruned reverted calls.
       During DCR sequence construction we keep only transactions that SUCCEEDED
       on the reference. How many were attempted and discarded?
       Source: <dcr>/<addr>/coverage.json -> ucb_stats.{attempt_counts,success_counts}
       Reported: prune rate = 1 - successes/attempts, pooled and per-contract.

  Q4 — Drain phase.
       How many events per contract are classified as ownership-changing /
       "absorbing" and deferred to the drain phase (i.e. never scheduled in the
       middle of a sequence)?
       Source: <dcr>/<addr>/source_dcr.json -> events[].ownership_change, and the
       same structural name test the generator uses (renounce/transferOwnership).

  Q5 — Parameter provenance (bonus; reviewer Q5).
       Where did each synthetic transaction's arguments come from?
       Source: <dcr>/<addr>/transactions_synthetic.json -> _param_source

  Q9 — Cost per oracle.
       Wall-clock cost of each oracle, per contract.
       Source: the aggregate results JSONs each stage writes
       (duration_seconds + contract counts).

Usage
-----
  python3 additional_stats.py                       # uses the defaults below
  python3 additional_stats.py --model gemma-4-31B-it-qat-UD-Q4_K_XL.gguf
  python3 additional_stats.py --json out.json
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from statistics import mean, median
from typing import Optional

SRC = Path("/workdir/src")

# Same structural test the sequence generator uses to spot privilege-drop events.
_ABSORBING_RE = re.compile(r"(renounce(_)?ownership|renouncerole|revokerole)", re.I)


def _is_absorbing(ev: dict) -> bool:
    fn = (ev.get("function_name") or ev.get("id") or "")
    if _ABSORBING_RE.search(fn):
        return True
    if "transferownership" in fn.lower().replace("_", ""):
        return True
    return bool(ev.get("ownership_change", False)) and \
        ("renounce" in fn.lower() or "transfer" in fn.lower())


def _load(p: Path) -> Optional[dict]:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _fmt_hms(sec: float) -> str:
    s = int(sec)
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"


def _pct(v):
    return f"{v*100:.1f}%" if v is not None else "n/a"


# ---------------------------------------------------------------------------
# Q3 + Q4 + Q5 — walk the DCR dir for one model
# ---------------------------------------------------------------------------

def per_contract_stats(dcr_dir: Path) -> dict:
    q3_rows, q4_rows = [], []
    param_src = Counter()
    n_txs_total = 0

    tot_attempts = tot_successes = 0

    for cdir in sorted(d for d in dcr_dir.iterdir() if d.is_dir()):
        addr = cdir.name

        # ---- Q3: attempts vs successes (the prune rate) --------------------
        cov = _load(cdir / "coverage.json")
        if cov:
            ucb = cov.get("ucb_stats", {})
            att = sum(ucb.get("attempt_counts", {}).values())
            suc = sum(ucb.get("success_counts", {}).values())
            if att > 0:
                tot_attempts += att
                tot_successes += suc
                q3_rows.append({
                    "address":    addr,
                    "attempts":   att,
                    "successes":  suc,
                    "pruned":     att - suc,
                    "prune_rate": round(1 - suc / att, 4),
                })

        # ---- Q4: drain-phase / absorbing events ----------------------------
        dcr = _load(cdir / "source_dcr.json")
        if dcr:
            events = dcr.get("events", [])
            if events:
                n_own = sum(1 for e in events if e.get("ownership_change", False))
                n_abs = sum(1 for e in events if _is_absorbing(e))
                q4_rows.append({
                    "address":          addr,
                    "n_events":         len(events),
                    "n_ownership":      n_own,
                    "n_absorbing":      n_abs,
                    "frac_deferred":    round(n_abs / len(events), 4),
                })

        # ---- Q5: parameter provenance --------------------------------------
        txs = _load(cdir / "transactions_synthetic.json")
        if isinstance(txs, list):
            n_txs_total += len(txs)
            for t in txs:
                param_src[t.get("_param_source", "unknown")] += 1

    return {
        "q3_rows": q3_rows,
        "q4_rows": q4_rows,
        "param_src": param_src,
        "n_txs_total": n_txs_total,
        "pooled_attempts": tot_attempts,
        "pooled_successes": tot_successes,
    }


# ---------------------------------------------------------------------------
# Q9 — cost per oracle, from the aggregate result files
# ---------------------------------------------------------------------------

def cost_stats(safe: str) -> list[dict]:
    """Pull duration + contract counts from whatever aggregate files exist."""
    out = []

    def add(label, path: Path, dur_key="duration_seconds",
            n_keys=("contracts_replayed", "total_contracts", "done", "total")):
        d = _load(path)
        if not d:
            return
        dur = d.get(dur_key)
        n = None
        for k in n_keys:
            if d.get(k):
                n = d[k]
                break
        # differential fuzzing writes a slightly different shape
        if n is None and "results" in d:
            n = sum(1 for r in d["results"] if r.get("state") == "DONE")
        if dur and n:
            out.append({
                "oracle":         label,
                "n_contracts":    n,
                "total_seconds":  round(dur, 1),
                "total_hms":      _fmt_hms(dur),
                "sec_per_contract": round(dur / n, 1),
                "file":           str(path),
            })

    # M2 — historical replay (generated side)
    add("M2 historical replay",
        SRC / "transaction_replay" / f"generated_replay_results_{safe}.json")
    # M3 — synthetic replay (generated side, matched oracle)
    add("M3 synthetic replay",
        SRC / "dcr_replay" / f"synth_generated_replay_results_{safe}.json")
    # M3 — sequence GENERATION cost (model-independent per model dir)
    #      (no duration is written; we note this explicitly below)
    # M1 — differential fuzzing
    for cand in [
        SRC / "differential_fuzzing" / f"fuzzying_results_{safe}" / "summary.json",
        SRC / "differential_fuzzing" / f"fuzzing_results_{safe}.json",
        SRC / "differential_fuzzing" / f"fuzzying_results_{safe}.json",
    ]:
        add("M1 differential fuzzing", cand)

    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma-4-31B-it-qat-UD-Q4_K_XL.gguf",
                    help="Model whose DCR graphs/sequences to analyse.")
    ap.add_argument("--dcr-root", type=Path,
                    default=SRC / "dcr_generation", dest="dcr_root")
    ap.add_argument("--dcr-prefix", default="generated_dcr_1to20_", dest="dcr_prefix")
    ap.add_argument("--json", type=Path, default=None,
                    help="Also write everything as JSON here.")
    args = ap.parse_args()

    safe = args.model.replace(".", "_").replace(" ", "_")
    dcr_dir = args.dcr_root / f"{args.dcr_prefix}{safe}"
    if not dcr_dir.exists():
        raise SystemExit(f"ERROR: DCR dir not found: {dcr_dir}")

    print(f"\nModel: {args.model}")
    print(f"DCR:   {dcr_dir}\n")

    st = per_contract_stats(dcr_dir)
    sep = "─" * 74

    # ══════════════════════════════════════════════════════════════════════
    # Q3 — pruned reverted calls
    # ══════════════════════════════════════════════════════════════════════
    print("=" * 74)
    print("  Q3 — REVERTED CALLS PRUNED DURING SEQUENCE CONSTRUCTION")
    print("=" * 74)
    rows = st["q3_rows"]
    if not rows:
        print("  no coverage.json with ucb_stats found")
    else:
        A, S = st["pooled_attempts"], st["pooled_successes"]
        rates = [r["prune_rate"] for r in rows]
        print(f"  Contracts with data          : {len(rows)}")
        print(f"  Total call attempts          : {A:,}")
        print(f"  ... succeeded (kept)         : {S:,}")
        print(f"  ... reverted (pruned)        : {A-S:,}")
        print()
        print(f"  POOLED prune rate            : {_pct(1 - S/A)}"
              f"   ← of every call the generator tried, this fraction reverted")
        print(f"  Mean per-contract prune rate : {_pct(mean(rates))}")
        print(f"  Median                       : {_pct(median(rates))}")
        print(f"  Min / Max                    : {_pct(min(rates))} / {_pct(max(rates))}")
        print()
        worst = sorted(rows, key=lambda r: -r["prune_rate"])[:5]
        print("  Highest prune rates (generator struggled to satisfy guards):")
        for r in worst:
            print(f"    {r['address'][:14]}…  {_pct(r['prune_rate'])}  "
                  f"({r['pruned']:,}/{r['attempts']:,} pruned)")
        print()
        print("  For the paper: the synthetic corpus keeps only calls that SUCCEED on")
        print("  the reference. The pruned calls are ones the reference itself rejects,")
        print("  i.e. inadmissible for the intended lifecycle — not defect signal we")
        print("  discard. They are, however, exactly what a NEGATIVE-testing oracle")
        print("  would use; state this as a threat to validity.")
    print("=" * 74)

    # ══════════════════════════════════════════════════════════════════════
    # Q4 — drain phase
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 74)
    print("  Q4 — EVENTS DEFERRED TO THE DRAIN PHASE")
    print("=" * 74)
    rows = st["q4_rows"]
    if not rows:
        print("  no source_dcr.json with events found")
    else:
        n_ev   = [r["n_events"] for r in rows]
        n_own  = [r["n_ownership"] for r in rows]
        n_abs  = [r["n_absorbing"] for r in rows]
        fr     = [r["frac_deferred"] for r in rows]
        with_any = sum(1 for r in rows if r["n_absorbing"] > 0)
        print(f"  Contracts with a DCR graph        : {len(rows)}")
        print(f"  Mean events per contract          : {mean(n_ev):.1f}")
        print()
        print(f"  Events flagged ownership_change   : {sum(n_own)} "
              f"(mean {mean(n_own):.2f}/contract)")
        print(f"  Events deferred to drain (absorb.): {sum(n_abs)} "
              f"(mean {mean(n_abs):.2f}/contract)")
        print(f"  Contracts with >=1 deferred event : {with_any} "
              f"({with_any/len(rows)*100:.1f}%)")
        print()
        print(f"  Mean fraction of a graph deferred : {_pct(mean(fr))}")
        print(f"  Max fraction deferred             : {_pct(max(fr))}")
        print()
        print("  For the paper: only privilege-DROP events (renounce/transferOwnership)")
        print("  are deferred, and they are still EXECUTED — at the end, in priority")
        print("  order — so they are not skipped, only descheduled from the middle of")
        print("  the sequence, where they would zero out every subsequent owner-only")
        print(f"  call. They are {_pct(mean(fr))} of a typical graph.")
    print("=" * 74)

    # ══════════════════════════════════════════════════════════════════════
    # Q5 (bonus) — parameter provenance
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 74)
    print("  Q5 — WHERE SYNTHETIC TRANSACTION PARAMETERS COME FROM")
    print("=" * 74)
    ps, tot = st["param_src"], st["n_txs_total"]
    if not tot:
        print("  no transactions_synthetic.json found")
    else:
        print(f"  Synthetic transactions analysed : {tot:,}")
        print()
        for src, n in ps.most_common():
            print(f"    {src:<22} {n:>7,}   {_pct(n/tot)}")
        print()
        print("  For the paper: `dcr_call_params` = values the LLM proposed at DCR")
        print("  extraction time; `guard_synthesis` = values adjusted at run time to")
        print("  satisfy a branch guard; `historical` = replayed from real calldata.")
        print("  Note we do NOT backtrack or invoke a solver when a guard cannot be")
        print("  satisfied — the call is simply attempted and, if it reverts, pruned.")
    print("=" * 74)

    # ══════════════════════════════════════════════════════════════════════
    # Q9 — cost
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 74)
    print("  Q9 — WALL-CLOCK COST PER ORACLE")
    print("=" * 74)
    costs = cost_stats(safe)
    if not costs:
        print("  no aggregate result files found for this model — check paths:")
        print(f"    {SRC/'transaction_replay'}/generated_replay_results_{safe}.json")
        print(f"    {SRC/'dcr_replay'}/synth_generated_replay_results_{safe}.json")
        print(f"    {SRC/'differential_fuzzing'}/...")
    else:
        print(f"  {'Oracle':<26}{'N':>6}{'total':>12}{'sec/contract':>15}")
        print("  " + sep)
        for c in costs:
            print(f"  {c['oracle']:<26}{c['n_contracts']:>6}"
                  f"{c['total_hms']:>12}{c['sec_per_contract']:>15.1f}")
        print("  " + sep)
        print()
        print("  NOTE: DCR sequence GENERATION is a one-off, model-independent cost not")
        print("  captured above (make_sequences_hardhat.py writes no duration). Time it")
        print("  with `/usr/bin/time -v` on a re-run, or quote the orchestrator log.")
        print("  Also note M2/M3 cost scales with K (=1000 txs) and samples per contract,")
        print("  not with contract size directly.")
    print("=" * 74)

    if args.json:
        args.json.write_text(json.dumps({
            "model": args.model,
            "q3": {
                "pooled_attempts":  st["pooled_attempts"],
                "pooled_successes": st["pooled_successes"],
                "pooled_prune_rate": round(
                    1 - st["pooled_successes"] / st["pooled_attempts"], 4)
                    if st["pooled_attempts"] else None,
                "per_contract": st["q3_rows"],
            },
            "q4": {"per_contract": st["q4_rows"]},
            "q5": {"n_txs": st["n_txs_total"], "param_source": dict(st["param_src"])},
            "q9": costs,
        }, indent=2))
        print(f"\nJSON written to {args.json}")


if __name__ == "__main__":
    main()