#!/usr/bin/env python3
"""
comprehensive_analysis.py
=========================
Four-pillar analysis of DCR-guided synthetic transactions as a test oracle
for LLM-generated smart contracts, for IEEE TSE.

Pillars
-------
  1. COVERAGE        — do synthetic txs exercise more functions than historical?
  2. CORRECTNESS     — pass rate of generated contracts (synthetic vs historical)
  3. DISCRIMINABILITY— do generated contracts that PASS historical replay FAIL
                       synthetic replay? (synthetic catches bugs historical misses)
  4. STATE DIVERGENCE— per-tx comparison of state_after between original and
                       generated; first_divergence_tx + which view fns diverge.

Data sources (all already on disk)
----------------------------------
  historical × original : <dataset>/<addr>/replay_result_1000.json
  historical × generated: <generated>/<addr>/replay_sample_<N>_result_1000.json
  synthetic  × original : <dcr_replay>/synth_replay_results/<addr>/synth_replay_result_1000.json
  synthetic  × generated: <dcr_replay>/synth_replay_results_generated/<addr>/synth_replay_sample_<N>_result_1000.json
  DCR coverage          : <dcr_gen>/generated_dcr_1to20/<addr>/coverage.json

Every per-tx entry carries `state_after` (per-tx view-fn snapshot) and `_fn_sig`,
and the original/generated tx lists are aligned by hash (verified).

Usage
-----
  python comprehensive_analysis.py
  python comprehensive_analysis.py --k 1000 --output report.json
  python comprehensive_analysis.py --no-details        # summary only
  python comprehensive_analysis.py --csv table.csv     # also dump per-contract CSV
"""

import argparse
import csv
import json
from pathlib import Path
from statistics import mean, median


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_SRC        = _SCRIPT_DIR.parent

DEF_DATASET   = _SRC / "dataset" / "contracts_dataset_1to20"
DEF_GENERATED = _SRC / "source_generation" / "generated_dcr_1to20_Qwen3_6-27B-UD-Q4_K_XL_gguf"
DEF_DCR_REPLAY = _SRC / "dcr_replay"
DEF_DCR_GEN   = _SRC / "dcr_generation" / "generated_dcr_1to20"
DEF_OUTPUT    = _SCRIPT_DIR / "comprehensive_analysis.json"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _load(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(mean(xs), 4) if xs else None


def fmt(v, spec=":.1%"):
    if v is None:
        return "n/a"
    return format(v, spec.lstrip(":"))


def _fn_of(tx):
    """Function name from a tx result's _fn_sig (strip arg list)."""
    return (tx.get("_fn_sig", "") or "").split("(")[0]


# ---------------------------------------------------------------------------
# State divergence between two aligned tx lists
# ---------------------------------------------------------------------------

def compute_divergence(orig_txs: list, gen_txs: list) -> dict:
    """
    Compare state_after tx-by-tx between an original and a generated replay.
    Assumes the two lists are hash-aligned (verified upstream); we still guard
    by zipping to the shorter length and double-checking hashes when present.

    Returns:
      first_divergence_tx : 1-based index of first tx where any view fn differs
                            (None if never diverges)
      diverging_fns       : set of view-fn names that ever diverge
      n_diverging_txs     : how many txs show any divergence
      n_compared          : how many txs had state on both sides to compare
      divergence_rate     : n_diverging_txs / n_compared
    """
    first = None
    diverging_fns = set()
    n_div = 0
    n_cmp = 0

    for i, (o, g) in enumerate(zip(orig_txs, gen_txs), 1):
        # If hashes are present and disagree, alignment broke — stop comparing.
        oh, gh = o.get("hash"), g.get("hash")
        if oh and gh and oh != gh:
            break

        os_, gs_ = o.get("state_after"), g.get("state_after")
        if not os_ or not gs_:
            continue  # no state to compare on this tx (e.g. no view fns / revert)
        n_cmp += 1

        # Compare the intersection of view-fn keys
        tx_diverged = False
        for fn in set(os_) & set(gs_):
            if os_[fn] != gs_[fn]:
                diverging_fns.add(fn)
                tx_diverged = True
        if tx_diverged:
            n_div += 1
            if first is None:
                first = i

    return {
        "first_divergence_tx": first,
        "diverging_fns":       sorted(diverging_fns),
        "n_diverging_txs":     n_div,
        "n_compared":          n_cmp,
        "divergence_rate":     round(n_div / n_cmp, 4) if n_cmp else None,
    }


def best_sample_divergence(orig_txs: list, sample_files: list) -> dict:
    """
    A contract has multiple generated samples. We report the divergence for the
    sample that BEST matches the original (lowest divergence_rate), since that
    is the most charitable correctness assessment of the generator. We also keep
    the worst and the mean across samples.
    """
    per_sample = []
    for sf in sample_files:
        gd = _load(sf)
        if not gd:
            continue
        g_txs = gd.get("tx_results", [])
        if not g_txs:
            continue
        div = compute_divergence(orig_txs, g_txs)
        div["sample_idx"] = gd.get("sample_idx")
        div["sample_pass_rate"] = gd.get("pass_rate")
        if div["n_compared"]:
            per_sample.append(div)

    if not per_sample:
        return {"n_samples_with_state": 0}

    rates = [d["divergence_rate"] for d in per_sample if d["divergence_rate"] is not None]
    best  = min(per_sample, key=lambda d: (d["divergence_rate"] is None, d["divergence_rate"]))
    worst = max(per_sample, key=lambda d: (d["divergence_rate"] is None, d["divergence_rate"]))

    # Union of all view fns that diverge in ANY sample
    all_div_fns = sorted({fn for d in per_sample for fn in d["diverging_fns"]})

    return {
        "n_samples_with_state":  len(per_sample),
        "best_divergence_rate":  best.get("divergence_rate"),
        "best_first_div_tx":     best.get("first_divergence_tx"),
        "best_sample_idx":       best.get("sample_idx"),
        "worst_divergence_rate": worst.get("divergence_rate"),
        "mean_divergence_rate":  _avg(rates),
        "any_diverging_fns":     all_div_fns,
        # A contract "diverges" if even the best sample diverges at all.
        "diverges":              bool(best.get("first_divergence_tx") is not None),
    }


# ---------------------------------------------------------------------------
# Per-contract assembly
# ---------------------------------------------------------------------------

def analyse_contract(addr, paths, k):
    dataset, generated, dcr_replay, dcr_gen = paths
    r = {"address": addr}

    # ── load the four replays ─────────────────────────────────────────────
    hist_orig = _load(dataset / addr / f"replay_result_{k}.json")
    synth_orig = _load(dcr_replay / "synth_replay_results" / addr / f"synth_replay_result_{k}.json")

    hist_gen_files  = sorted((generated / addr).glob(f"replay_sample_*_result_{k}.json")) \
                      if (generated / addr).exists() else []
    synth_gen_dir   = dcr_replay / "synth_replay_results_generated" / addr
    synth_gen_files = sorted(synth_gen_dir.glob(f"synth_replay_sample_*_result_{k}.json")) \
                      if synth_gen_dir.exists() else []

    cov = _load(dcr_gen / addr / "coverage.json")

    name = (hist_orig or synth_orig or {}).get("contract_name", addr[:10])
    r["contract_name"] = name

    # ── PILLAR 1: COVERAGE ────────────────────────────────────────────────
    hist_fns  = set((hist_orig  or {}).get("fn_pass_rates", {}).keys())
    synth_fns = set((synth_orig or {}).get("fn_pass_rates", {}).keys())
    r["hist_fn_count"]  = len(hist_fns)
    r["synth_fn_count"] = len(synth_fns)
    r["fns_only_synth"] = sorted(synth_fns - hist_fns)
    r["fns_only_hist"]  = sorted(hist_fns - synth_fns)
    if cov:
        fc = cov.get("function_coverage", {})
        r["dcr_fn_coverage"] = fc.get("coverage_ratio")
    else:
        r["dcr_fn_coverage"] = None

    # ── PILLAR 2: CORRECTNESS (pass rates) ────────────────────────────────
    r["hist_orig_pass"]  = (hist_orig  or {}).get("pass_rate")
    r["synth_orig_pass"] = (synth_orig or {}).get("pass_rate")

    # generated pass rates: mean across samples
    hist_gen_rates  = [_load(f).get("pass_rate") for f in hist_gen_files  if _load(f)]
    synth_gen_rates = [_load(f).get("pass_rate") for f in synth_gen_files if _load(f)]
    r["hist_gen_pass"]  = _avg(hist_gen_rates)
    r["synth_gen_pass"] = _avg(synth_gen_rates)
    r["n_hist_samples"]  = len(hist_gen_files)
    r["n_synth_samples"] = len(synth_gen_files)

    # ── PILLAR 3: DISCRIMINABILITY ────────────────────────────────────────
    # A generated sample is "discriminated" if it passes historical replay
    # (>= threshold) but fails synthetic replay (< threshold). We compute it
    # per sample index where BOTH a historical and synthetic result exist.
    THRESH = 0.5
    hist_by_idx  = {}
    for f in hist_gen_files:
        d = _load(f)
        if d: hist_by_idx[d.get("sample_idx")] = d.get("pass_rate")
    synth_by_idx = {}
    for f in synth_gen_files:
        d = _load(f)
        if d: synth_by_idx[d.get("sample_idx")] = d.get("pass_rate")

    common_idx = set(hist_by_idx) & set(synth_by_idx)
    discriminated = 0
    both_pass = 0
    for idx in common_idx:
        hp, sp = hist_by_idx[idx], synth_by_idx[idx]
        if hp is None or sp is None:
            continue
        if hp >= THRESH and sp < THRESH:
            discriminated += 1
        if hp >= THRESH and sp >= THRESH:
            both_pass += 1
    r["n_common_samples"]    = len(common_idx)
    r["n_discriminated"]     = discriminated   # pass hist, fail synth
    r["n_both_pass"]         = both_pass
    # contract-level flag: at least one sample is caught only by synthetic
    r["discriminated"]       = discriminated > 0

    # ── PILLAR 4: STATE DIVERGENCE ────────────────────────────────────────
    # Synthetic transaction set
    if synth_orig and synth_gen_files:
        r["synth_divergence"] = best_sample_divergence(
            synth_orig.get("tx_results", []), synth_gen_files)
    else:
        r["synth_divergence"] = {"n_samples_with_state": 0}

    # Historical transaction set
    if hist_orig and hist_gen_files:
        r["hist_divergence"] = best_sample_divergence(
            hist_orig.get("tx_results", []), hist_gen_files)
    else:
        r["hist_divergence"] = {"n_samples_with_state": 0}

    return r


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def aggregate(records):
    def has(r, k): return r.get(k) is not None

    n = len(records)
    agg = {"n_contracts": n}

    # Pillar 1
    agg["hist_avg_fn_count"]  = _avg([r["hist_fn_count"]  for r in records])
    agg["synth_avg_fn_count"] = _avg([r["synth_fn_count"] for r in records])
    agg["dcr_avg_fn_coverage"] = _avg([r["dcr_fn_coverage"] for r in records])
    agg["contracts_synth_broader"] = sum(
        1 for r in records if r["synth_fn_count"] > r["hist_fn_count"])
    agg["contracts_hist_broader"] = sum(
        1 for r in records if r["hist_fn_count"] > r["synth_fn_count"])

    # Pillar 2
    agg["hist_orig_avg_pass"]  = _avg([r["hist_orig_pass"]  for r in records])
    agg["synth_orig_avg_pass"] = _avg([r["synth_orig_pass"] for r in records])
    agg["hist_gen_avg_pass"]   = _avg([r["hist_gen_pass"]   for r in records])
    agg["synth_gen_avg_pass"]  = _avg([r["synth_gen_pass"]  for r in records])

    # Pillar 3 — the headline
    agg["contracts_with_both_replays"] = sum(
        1 for r in records if r["n_common_samples"] > 0)
    agg["contracts_discriminated"] = sum(
        1 for r in records if r["discriminated"])
    agg["total_samples_discriminated"] = sum(
        r["n_discriminated"] for r in records)
    agg["total_common_samples"] = sum(r["n_common_samples"] for r in records)
    if agg["total_common_samples"]:
        agg["discriminability_rate"] = round(
            agg["total_samples_discriminated"] / agg["total_common_samples"], 4)
    else:
        agg["discriminability_rate"] = None

    # Pillar 4
    synth_div_rates = [r["synth_divergence"].get("best_divergence_rate")
                       for r in records
                       if r["synth_divergence"].get("n_samples_with_state")]
    hist_div_rates  = [r["hist_divergence"].get("best_divergence_rate")
                       for r in records
                       if r["hist_divergence"].get("n_samples_with_state")]
    agg["synth_contracts_with_divergence_data"] = len(synth_div_rates)
    agg["hist_contracts_with_divergence_data"]  = len(hist_div_rates)
    agg["synth_contracts_diverging"] = sum(
        1 for r in records if r["synth_divergence"].get("diverges"))
    agg["hist_contracts_diverging"] = sum(
        1 for r in records if r["hist_divergence"].get("diverges"))
    agg["synth_mean_best_divergence_rate"] = _avg(synth_div_rates)
    agg["hist_mean_best_divergence_rate"]  = _avg(hist_div_rates)

    return agg


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

def print_report(records, agg, no_details=False):
    SEP = "═" * 78
    print(f"\n{SEP}")
    print("  COMPREHENSIVE ANALYSIS — DCR SYNTHETIC vs HISTORICAL ORACLE")
    print(SEP)

    print("\n── PILLAR 1: COVERAGE ──────────────────────────────────────────")
    print(f"  Avg functions hit — historical : {fmt(agg['hist_avg_fn_count'], ':.2f')}")
    print(f"  Avg functions hit — synthetic  : {fmt(agg['synth_avg_fn_count'], ':.2f')}")
    print(f"  DCR avg fn coverage ratio      : {fmt(agg['dcr_avg_fn_coverage'])}")
    print(f"  Contracts synthetic broader    : {agg['contracts_synth_broader']}")
    print(f"  Contracts historical broader   : {agg['contracts_hist_broader']}")

    print("\n── PILLAR 2: CORRECTNESS (pass rates) ──────────────────────────")
    print(f"  Original  — historical txs     : {fmt(agg['hist_orig_avg_pass'])}")
    print(f"  Original  — synthetic txs      : {fmt(agg['synth_orig_avg_pass'])}")
    print(f"  Generated — historical txs     : {fmt(agg['hist_gen_avg_pass'])}")
    print(f"  Generated — synthetic txs      : {fmt(agg['synth_gen_avg_pass'])}")

    print("\n── PILLAR 3: DISCRIMINABILITY (headline) ───────────────────────")
    print(f"  Contracts with both replays    : {agg['contracts_with_both_replays']}")
    print(f"  Contracts discriminated        : {agg['contracts_discriminated']}")
    print(f"    (>=1 sample passes historical but fails synthetic)")
    print(f"  Samples discriminated / total  : "
          f"{agg['total_samples_discriminated']} / {agg['total_common_samples']}")
    print(f"  Discriminability rate          : {fmt(agg['discriminability_rate'])}")

    print("\n── PILLAR 4: STATE DIVERGENCE ──────────────────────────────────")
    print(f"  [synthetic txs]")
    print(f"    contracts with div data      : {agg['synth_contracts_with_divergence_data']}")
    print(f"    contracts diverging          : {agg['synth_contracts_diverging']}")
    print(f"    mean best divergence rate    : {fmt(agg['synth_mean_best_divergence_rate'])}")
    print(f"  [historical txs]")
    print(f"    contracts with div data      : {agg['hist_contracts_with_divergence_data']}")
    print(f"    contracts diverging          : {agg['hist_contracts_diverging']}")
    print(f"    mean best divergence rate    : {fmt(agg['hist_mean_best_divergence_rate'])}")
    print(SEP)

    if no_details:
        return

    # Per-contract table — sorted to surface the most interesting (discriminated
    # or diverging) contracts first.
    def sort_key(r):
        return (not r["discriminated"],
                not r["synth_divergence"].get("diverges", False),
                -(r["synth_divergence"].get("best_divergence_rate") or 0))
    rows = sorted(records, key=sort_key)

    print(f"\n{'contract':<18}{'h_pass':>7}{'s_pass':>7}{'hg_pass':>8}{'sg_pass':>8}"
          f"{'disc':>5}{'s_div':>7}{'1stTx':>6}  diverging_fns")
    print("-" * 92)
    for r in rows[:60]:   # cap to keep console readable
        sd = r["synth_divergence"]
        name = r["contract_name"][:16]
        print(f"{name:<18}"
              f"{fmt(r['hist_orig_pass']):>7}"
              f"{fmt(r['synth_orig_pass']):>7}"
              f"{fmt(r['hist_gen_pass']):>8}"
              f"{fmt(r['synth_gen_pass']):>8}"
              f"{('YES' if r['discriminated'] else '-'):>5}"
              f"{fmt(sd.get('best_divergence_rate')):>7}"
              f"{str(sd.get('best_first_div_tx') or '-'):>6}"
              f"  {','.join(sd.get('any_diverging_fns', [])[:4])}")
    if len(rows) > 60:
        print(f"  ... ({len(rows)-60} more in the JSON report)")
    print(SEP)


# ---------------------------------------------------------------------------
# CSV dump
# ---------------------------------------------------------------------------

def write_csv(records, path: Path):
    cols = ["address", "contract_name",
            "hist_orig_pass", "synth_orig_pass", "hist_gen_pass", "synth_gen_pass",
            "hist_fn_count", "synth_fn_count", "dcr_fn_coverage",
            "discriminated", "n_discriminated", "n_common_samples",
            "synth_diverges", "synth_best_div_rate", "synth_first_div_tx",
            "hist_diverges", "hist_best_div_rate"]
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in records:
            sd, hd = r["synth_divergence"], r["hist_divergence"]
            w.writerow([
                r["address"], r["contract_name"],
                r["hist_orig_pass"], r["synth_orig_pass"],
                r["hist_gen_pass"], r["synth_gen_pass"],
                r["hist_fn_count"], r["synth_fn_count"], r["dcr_fn_coverage"],
                r["discriminated"], r["n_discriminated"], r["n_common_samples"],
                sd.get("diverges"), sd.get("best_divergence_rate"), sd.get("best_first_div_tx"),
                hd.get("diverges"), hd.get("best_divergence_rate"),
            ])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Four-pillar comprehensive analysis for the TSE paper.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--dataset",    type=Path, default=DEF_DATASET)
    p.add_argument("--generated",  type=Path, default=DEF_GENERATED)
    p.add_argument("--dcr-replay", type=Path, default=DEF_DCR_REPLAY, dest="dcr_replay")
    p.add_argument("--dcr-gen",    type=Path, default=DEF_DCR_GEN, dest="dcr_gen")
    p.add_argument("--k",          type=int,  default=1000)
    p.add_argument("--output",     type=Path, default=DEF_OUTPUT)
    p.add_argument("--csv",        type=Path, default=None)
    p.add_argument("--no-details", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    paths = (args.dataset, args.generated, args.dcr_replay, args.dcr_gen)

    for label, pth in [("dataset", args.dataset), ("generated", args.generated),
                       ("dcr_replay", args.dcr_replay), ("dcr_gen", args.dcr_gen)]:
        print(f"  {label:<11}: {pth}  {'' if pth.exists() else '[MISSING]'}")

    # Universe of addresses: anything with at least one replay result
    addrs = set()
    addrs |= {p.parent.name for p in args.dataset.glob(f"*/replay_result_{args.k}.json")}
    addrs |= {p.parent.name for p in (args.dcr_replay / "synth_replay_results").glob(
        f"*/synth_replay_result_{args.k}.json")}
    addrs = sorted(a for a in addrs if a.startswith("0x"))

    print(f"\nAnalysing {len(addrs)} contracts (k={args.k}) ...")

    records = [analyse_contract(a, paths, args.k) for a in addrs]
    records = [r for r in records if r]   # drop empties

    agg = aggregate(records)
    print_report(records, agg, no_details=args.no_details)

    args.output.write_text(json.dumps({"aggregate": agg, "contracts": records}, indent=2))
    print(f"\nFull JSON report : {args.output}")

    if args.csv:
        write_csv(records, args.csv)
        print(f"Per-contract CSV : {args.csv}")


if __name__ == "__main__":
    main()