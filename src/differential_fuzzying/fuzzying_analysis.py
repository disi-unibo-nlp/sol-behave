"""
fuzzing_analysis.py
===================

Analysis and reporting of differential fuzzing results from
differential_fuzzing.py.

Metrics reported
----------------
  fn_pass_rate        per function: fraction of compiled samples where
                      echidna found NO counterexample (higher = more equivalent)
  contract_pass_rate  per contract: fraction of samples where ALL functions
                      passed (no divergence on any function)
  pass_at_k           per contract: True if at least one sample had all
                      functions passing (best-case equivalence)
  fn_coverage         fraction of state-changing ABI functions that echidna
                      could exercise (those with fuzzable types)
  fn_pass_at_k        per function: True if at least one sample had no
                      divergence for this function

Usage
-----
  python fuzzing_analysis.py
  python fuzzing_analysis.py --results fuzzing_results.json
  python fuzzing_analysis.py --results fuzzing_results.json \\
      --comparison ../transaction_replay/comparison.json
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS = str(_SCRIPT_DIR / "fuzzying_results" / "fuzzing_results.json")
DEFAULT_OUTPUT  = str(_SCRIPT_DIR / "fuzzing_analysis.json")


# python3 fuzzying_analysis.py --results fuzzying_results_Qwen3_6-27B-UD-Q4_K_XL_gguf/fuzzing_results.json

# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_results(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return data.get("results", [])


def load_comparison(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text())
    return {c["address"].lower(): c for c in data.get("contracts", [])}


# ---------------------------------------------------------------------------
# Per-contract analysis
# ---------------------------------------------------------------------------

def analyse_contract(r: dict, comparison: dict) -> dict:
    address = r.get("address", "").lower()
    name    = r.get("contract_name", address[:12] + "…")

    if r.get("state") != "DONE":
        return {
            "address":       address,
            "contract_name": name,
            "status":        "setup_failed",
            "reason":        r.get("reason", "?"),
        }

    k                   = r.get("k", 0)
    fn_pass_rates       = r.get("fn_pass_rates", {})
    fn_pass_at_k        = r.get("fn_pass_at_k", {})
    contract_pass_rate  = r.get("contract_pass_rate")
    pass_at_k           = r.get("pass_at_k", False)
    fn_coverage         = r.get("fn_coverage")
    n_fns_fuzzed        = r.get("n_fns_fuzzed", 0)
    n_fns_total         = r.get("n_fns_total", 0)
    sample_all_pass     = r.get("sample_all_pass", [])

    mean_fn_pass_rate = (
        round(sum(fn_pass_rates.values()) / len(fn_pass_rates), 4)
        if fn_pass_rates else None
    )

    return {
        "address":              address,
        "contract_name":        name,
        "status":               "done",
        "k":                    k,
        # Function-level
        "n_fns_fuzzed":         n_fns_fuzzed,
        "n_fns_total":          n_fns_total,
        "fn_coverage":          fn_coverage,
        "fn_pass_rates":        fn_pass_rates,
        "fn_pass_at_k":         fn_pass_at_k,
        "mean_fn_pass_rate":    mean_fn_pass_rate,
        # Worst / best function
        "worst_fn":             min(fn_pass_rates, key=fn_pass_rates.get)
                                if fn_pass_rates else None,
        "worst_fn_rate":        min(fn_pass_rates.values()) if fn_pass_rates else None,
        "best_fn":              max(fn_pass_rates, key=fn_pass_rates.get)
                                if fn_pass_rates else None,
        "best_fn_rate":         max(fn_pass_rates.values()) if fn_pass_rates else None,
        # Contract-level
        "contract_pass_rate":   contract_pass_rate,
        "pass_at_k":            pass_at_k,
    }


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------

def compute_aggregates(analyses: list[dict], threshold: float = 0.5) -> dict:
    done   = [a for a in analyses if a.get("status") == "done"]
    failed = [a for a in analyses if a.get("status") == "setup_failed"]
    if not done:
        return {"error": "no completed contracts"}

    n = len(done)

    contract_rates = [a["contract_pass_rate"] for a in done
                      if a.get("contract_pass_rate") is not None]
    mean_fn_rates  = [a["mean_fn_pass_rate"] for a in done
                      if a.get("mean_fn_pass_rate") is not None]
    fn_coverages   = [a["fn_coverage"] for a in done
                      if a.get("fn_coverage") is not None]
    pass_at_k_n    = sum(1 for a in done if a.get("pass_at_k"))

    # All fn pass rates across all contracts
    all_fn_rates = [v for a in done for v in a.get("fn_pass_rates", {}).values()]

    # Most commonly diverging functions (worst fn_pass_rate)
    worst_fns = Counter(a["worst_fn"] for a in done if a.get("worst_fn"))

    # Rank contracts by mean function pass rate (continuous fit score)
    ranked = sorted(
        (a for a in done if a.get("mean_fn_pass_rate") is not None),
        key=lambda a: a["mean_fn_pass_rate"],
        reverse=True,
    )
    top5    = [(a["contract_name"], a["mean_fn_pass_rate"],
                a.get("contract_pass_rate"), a.get("k", 0)) for a in ranked[:5]]
    bottom5 = [(a["contract_name"], a["mean_fn_pass_rate"],
                a.get("contract_pass_rate"), a.get("k", 0)) for a in ranked[-5:]]

    def _mean(vals):
        return round(sum(vals) / len(vals), 4) if vals else None

    return {
        "n_contracts":              n,
        "n_setup_failed":           len(failed),
        "mean_contract_pass_rate":  _mean(contract_rates),
        "mean_fn_pass_rate":        _mean(mean_fn_rates),
        "mean_fn_pass_rate_all":    _mean(all_fn_rates),
        "mean_fn_coverage":         _mean(fn_coverages),
        "pass_at_k":                pass_at_k_n,
        "pass_at_k_pct":            round(pass_at_k_n / n, 4) if n else None,
        "most_diverging_fns":       worst_fns.most_common(10),
        "top5_fn_pass":             top5,
        "bottom5_fn_pass":          bottom5,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(analyses: list[dict], aggregates: dict) -> None:
    done = [a for a in analyses if a.get("status") == "done"]
    done_sorted = sorted(done,
                         key=lambda a: (a.get("contract_pass_rate") or 0))

    def _pct(v): return f"{v:.1%}" if v is not None else "  n/a "

    col_name = 24
    w = 7
    header = (
        f"  {'Contract':<{col_name}}"
        f"  {'k':>{w}}"
        f"  {'CtrPass':>{w}}"
        f"  {'FnPass':>{w}}"
        f"  {'FnCov':>{w}}"
        f"  {'p@k':>{w}}"
    )
    sep = "─" * (len(header) - 2)

    print()
    print("  DIFFERENTIAL FUZZING RESULTS  (per-function, per-sample)")
    print("  " + sep)
    print(header)
    print("  " + sep)

    for a in done_sorted:
        p_at_k = "✓" if a.get("pass_at_k") else "✗"
        print(
            f"  {a['contract_name']:<{col_name}}"
            f"  {a.get('k',0):>{w}}"
            f"  {_pct(a.get('contract_pass_rate')):>{w}}"
            f"  {_pct(a.get('mean_fn_pass_rate')):>{w}}"
            f"  {_pct(a.get('fn_coverage')):>{w}}"
            f"  {p_at_k:>{w}}"
        )

    setup_failed = [a for a in analyses if a.get("status") == "setup_failed"]
    if setup_failed:
        print(f"  (+ {len(setup_failed)} contract(s) failed setup)")

    agg = aggregates
    print("  " + sep)
    print()
    print("  AGGREGATE STATISTICS")
    print("  " + sep)

    if "error" in agg:
        print(f"  {agg['error']}")
    else:
        print(f"  Contracts completed         : {agg['n_contracts']}")
        print(f"  Setup failed                : {agg['n_setup_failed']}")
        print()
        print(f"  ── Function-level ───────────────────────────────────")
        print(f"  Mean fn pass rate (all)     : {_pct(agg['mean_fn_pass_rate_all'])}")
        print(f"  Mean fn pass rate (per ctrt): {_pct(agg['mean_fn_pass_rate'])}")
        print(f"  Mean fn coverage            : {_pct(agg['mean_fn_coverage'])}")
        print()
        print(f"  ── Contract-level ───────────────────────────────────")
        print(f"  Mean contract pass rate     : {_pct(agg['mean_contract_pass_rate'])}")
        print(f"  pass@k (≥1 perfect sample)  : "
              f"{agg['pass_at_k']}/{agg['n_contracts']}"
              f"  ({_pct(agg['pass_at_k_pct'])})")
        print()

        # Top / bottom contracts by mean function pass rate
        cn = 24
        if agg.get("top5_fn_pass"):
            print(f"  ── Top 5 contracts (mean fn pass rate) ──────────────")
            print(f"    {'Contract':<{cn}}  {'FnPass':>7}  {'CtrPass':>7}  {'k':>3}")
            for name, fn, ctr, k in agg["top5_fn_pass"]:
                print(f"    {name:<{cn}}  {_pct(fn):>7}  {_pct(ctr):>7}  {k:>3}")
            print()
        if agg.get("bottom5_fn_pass"):
            print(f"  ── Worst 5 contracts (mean fn pass rate) ────────────")
            print(f"    {'Contract':<{cn}}  {'FnPass':>7}  {'CtrPass':>7}  {'k':>3}")
            for name, fn, ctr, k in agg["bottom5_fn_pass"]:
                print(f"    {name:<{cn}}  {_pct(fn):>7}  {_pct(ctr):>7}  {k:>3}")
            print()

        if agg["most_diverging_fns"]:
            print(f"  ── Most diverging functions ─────────────────────────")
            for fn, count in agg["most_diverging_fns"]:
                print(f"    {count:>4}×  {fn}")

        # Per-function detail for contracts with worst_fn
        worst_contracts = [a for a in done_sorted
                           if a.get("contract_pass_rate", 1.0) < 1.0][:5]
        if worst_contracts:
            print()
            print(f"  ── Per-function breakdown (worst contracts) ─────────")
            for a in worst_contracts:
                print(f"\n  {a['contract_name']}  (contract_pass={_pct(a.get('contract_pass_rate'))})")
                for fn, rate in sorted(a.get("fn_pass_rates", {}).items(),
                                       key=lambda x: x[1]):
                    p_at_k = "✓" if a.get("fn_pass_at_k", {}).get(fn) else "✗"
                    print(f"    {fn:<35}  pass_rate={_pct(rate)}  pass@k={p_at_k}")
    print("  " + sep)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Analyse differential fuzzing results (per-function, per-sample).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--results",    type=Path, default=Path(DEFAULT_RESULTS))
    p.add_argument("--comparison", type=Path, default=None)
    p.add_argument("--output",     type=Path, default=Path(DEFAULT_OUTPUT))
    p.add_argument("--threshold",  type=float, default=0.5)
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not args.results.exists():
        print(f"ERROR: results file not found: {args.results}")
        print("  Run differential_fuzzing.py first.")
        sys.exit(1)

    comparison: dict = {}
    if args.comparison and args.comparison.exists():
        comparison = load_comparison(args.comparison)
        print(f"  Loaded comparison data for {len(comparison)} contracts.")

    results  = load_results(args.results)
    analyses = [analyse_contract(r, comparison) for r in results]
    aggregates = compute_aggregates(analyses, args.threshold)

    args.output.write_text(json.dumps({
        "aggregates": aggregates,
        "contracts":  analyses,
    }, indent=2))
    print(f"  Analysis written to {args.output}")

    print_report(analyses, aggregates)


if __name__ == "__main__":
    main()