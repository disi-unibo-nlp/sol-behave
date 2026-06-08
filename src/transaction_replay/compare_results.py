"""
compare_results.py
==================

Compare transaction replay pass rates between the original (ground-truth)
contracts and the LLM-generated (repaired) samples.

Inputs
------
  --original-results   replay_results.json from replay_transactions.py
                       (one entry per contract, one pass_rate each)

  --generated-results  generated_replay_results.json from replay_generated.py
                       (one entry per contract, one pass_rate per sample)

For each contract address present in both files the script computes:

  original_pass_rate       pass rate of the ground-truth source
  mean_gen_pass_rate       mean pass rate across all replayed samples
  delta                    mean_gen_pass_rate − original_pass_rate
  best_gen_pass_rate       highest pass rate among all samples
  worst_gen_pass_rate      lowest pass rate among all samples
  samples_replayed         number of samples that reached DONE
  pass@1                   fraction of contracts where ≥1 sample ≥ threshold
  pass@k                   fraction of contracts where ≥k samples ≥ threshold
                           (k = total compiled samples per contract)

Output
------
  comparison.json          full per-contract table + aggregate stats
  (stdout)                 human-readable summary table

Usage
-----
  python compare_results.py [options]

  # Default paths:
  python compare_results.py

  # Explicit paths:
  python compare_results.py \\
      --original-results  ../transaction_replay/replay_results.json \\
      --generated-results ./generated_replay_results.json \\
      --output            ./comparison.json \\
      --pass-threshold    0.50
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Path defaults (relative to this script's location)
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_ORIGINAL  = str(_SCRIPT_DIR / "replay_results.json")
DEFAULT_GENERATED = str(_SCRIPT_DIR / "generated_replay_results.json")
DEFAULT_OUTPUT    = str(_SCRIPT_DIR / "comparison.json")
DEFAULT_THRESHOLD = 0.50


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

def load_original_results(path: Path) -> dict[str, dict]:
    """
    Load replay_results.json (from replay_transactions.py).
    Returns {address_lower: result_dict}.
    """
    data = json.loads(path.read_text())
    out  = {}
    for r in data.get("results", []):
        addr = r.get("address", "").lower()
        if addr:
            out[addr] = r
    return out


def load_generated_results(path: Path) -> dict[str, dict]:
    """
    Load generated_replay_results.json (from replay_generated.py).
    Returns {address_lower: contract_summary_dict}.
    """
    data = json.loads(path.read_text())
    out  = {}
    for c in data.get("contracts", []):
        addr = c.get("address", "").lower()
        if addr:
            out[addr] = c
    return out


# ---------------------------------------------------------------------------
# Per-contract comparison
# ---------------------------------------------------------------------------

def compare_contract(
    address:   str,
    orig:      dict,
    gen:       dict,
    threshold: float,
) -> dict:
    """
    Produce a comparison dict for one contract.
    """
    orig_pass_rate = orig.get("pass_rate")      # float or None
    orig_state     = orig.get("state", "?")
    contract_name  = (orig.get("contract_name")
                      or gen.get("contract_name", address[:12] + "…"))

    # Collect all sample pass rates from the generated results
    sample_rates: list[float] = gen.get("pass_rates", [])

    # Also pull per-sample data for the full table
    samples_data = []
    for s in gen.get("samples", []):
        if s.get("state") == "DONE" and s.get("pass_rate") is not None:
            samples_data.append({
                "sample_idx": s.get("sample_idx"),
                "pass_rate":  s.get("pass_rate"),
                "passed":     s.get("passed"),
                "failed":     s.get("failed"),
                "k":          s.get("k"),
            })

    n_replayed  = len(sample_rates)
    mean_gen    = round(sum(sample_rates) / n_replayed, 4) if n_replayed else None
    best_gen    = round(max(sample_rates), 4) if n_replayed else None
    worst_gen   = round(min(sample_rates), 4) if n_replayed else None
    delta       = (round(mean_gen - orig_pass_rate, 4)
                   if mean_gen is not None and orig_pass_rate is not None
                   else None)

    # pass@1: at least one sample meets threshold
    pass_at_1 = any(r >= threshold for r in sample_rates) if sample_rates else False

    # pass@k: all samples meet threshold (k = compiled_samples for this contract)
    pass_at_k = all(r >= threshold for r in sample_rates) if sample_rates else False

    # Fraction of samples that individually meet threshold
    frac_above = (sum(1 for r in sample_rates if r >= threshold) / n_replayed
                  if n_replayed else None)

    return {
        "address":            address,
        "contract_name":      contract_name,
        "original_state":     orig_state,
        "original_pass_rate": orig_pass_rate,
        "compiled_samples":   gen.get("compiled_samples", 0),
        "samples_replayed":   n_replayed,
        "mean_gen_pass_rate": mean_gen,
        "best_gen_pass_rate": best_gen,
        "worst_gen_pass_rate": worst_gen,
        "delta":              delta,
        "pass_at_1":          pass_at_1,
        "pass_at_k":          pass_at_k,
        "frac_samples_above_threshold": frac_above,
        "samples":            samples_data,
    }


# ---------------------------------------------------------------------------
# Aggregate statistics
# ---------------------------------------------------------------------------

def compute_aggregates(
    comparisons: list[dict],
    threshold:   float,
) -> dict:
    """Compute dataset-level summary statistics from per-contract comparisons."""
    # Only contracts where both original and generated replays succeeded
    valid = [c for c in comparisons
             if c["original_pass_rate"] is not None
             and c["mean_gen_pass_rate"] is not None]

    if not valid:
        return {"error": "no valid contracts to aggregate"}

    orig_rates  = [c["original_pass_rate"]  for c in valid]
    mean_rates  = [c["mean_gen_pass_rate"]   for c in valid]
    best_rates  = [c["best_gen_pass_rate"]   for c in valid]
    deltas      = [c["delta"]                for c in valid if c["delta"] is not None]

    n = len(valid)

    # pass@1 and pass@k across the dataset
    pass_at_1_count = sum(1 for c in valid if c["pass_at_1"])
    pass_at_k_count = sum(1 for c in valid if c["pass_at_k"])

    # Contracts where mean generated rate >= original
    improved = sum(1 for c in valid
                   if c["delta"] is not None and c["delta"] >= 0)

    # Contracts where mean generated rate >= threshold
    gen_above_threshold = sum(1 for c in valid
                              if c["mean_gen_pass_rate"] is not None
                              and c["mean_gen_pass_rate"] >= threshold)

    return {
        "n_contracts":              n,
        "threshold":                threshold,

        # Original source stats
        "orig_mean_pass_rate":      round(sum(orig_rates) / n, 4),
        "orig_min_pass_rate":       round(min(orig_rates), 4),
        "orig_max_pass_rate":       round(max(orig_rates), 4),

        # Generated (mean-per-contract) stats
        "gen_mean_pass_rate":       round(sum(mean_rates) / n, 4),
        "gen_min_pass_rate":        round(min(mean_rates), 4),
        "gen_max_pass_rate":        round(max(mean_rates), 4),

        # Best-sample stats
        "gen_best_mean_pass_rate":  round(sum(best_rates) / n, 4),

        # Delta (generated − original)
        "mean_delta":               round(sum(deltas) / len(deltas), 4) if deltas else None,
        "contracts_improved":       improved,
        "contracts_improved_pct":   round(improved / n, 4),

        # pass@ metrics
        "pass_at_1":                pass_at_1_count,
        "pass_at_1_pct":            round(pass_at_1_count / n, 4),
        "pass_at_k":                pass_at_k_count,
        "pass_at_k_pct":            round(pass_at_k_count / n, 4),
        "gen_above_threshold":      gen_above_threshold,
        "gen_above_threshold_pct":  round(gen_above_threshold / n, 4),
    }


# ---------------------------------------------------------------------------
# Human-readable table
# ---------------------------------------------------------------------------

def _pct(v: Optional[float]) -> str:
    return f"{v:.1%}" if v is not None else "  n/a "


def print_comparison_table(comparisons: list[dict], aggregates: dict) -> None:
    valid = [c for c in comparisons
             if c["original_pass_rate"] is not None
             and c["mean_gen_pass_rate"] is not None]

    # Sort by delta descending (best improvement first), then by contract name
    valid_sorted = sorted(
        valid,
        key=lambda c: (-(c["delta"] or 0), c["contract_name"]),
    )
    also_missing = [c for c in comparisons
                    if c["original_pass_rate"] is None
                    or c["mean_gen_pass_rate"] is None]

    col_name  = 24
    col_rate  =  8
    col_delta =  8
    col_n     =  6
    col_p1    =  5

    header = (
        f"{'Contract':<{col_name}}  "
        f"{'Orig':>{col_rate}}  "
        f"{'GenMean':>{col_rate}}  "
        f"{'GenBest':>{col_rate}}  "
        f"{'Delta':>{col_delta}}  "
        f"{'Smpl':>{col_n}}  "
        f"{'p@1':>{col_p1}}"
    )
    sep = "─" * len(header)

    print()
    print("  CONTRACT COMPARISON TABLE")
    print("  " + sep)
    print("  " + header)
    print("  " + sep)

    for c in valid_sorted:
        delta_str = (f"{c['delta']:+.1%}" if c["delta"] is not None else "  n/a")
        p1_str    = "✓" if c["pass_at_1"] else "✗"
        name      = c["contract_name"][:col_name]
        print(
            f"  {name:<{col_name}}  "
            f"{_pct(c['original_pass_rate']):>{col_rate}}  "
            f"{_pct(c['mean_gen_pass_rate']):>{col_rate}}  "
            f"{_pct(c['best_gen_pass_rate']):>{col_rate}}  "
            f"{delta_str:>{col_delta}}  "
            f"{c['samples_replayed']:>{col_n}}  "
            f"{p1_str:>{col_p1}}"
        )

    if also_missing:
        print(f"  (+ {len(also_missing)} contract(s) with incomplete data not shown)")

    print("  " + sep)
    print()
    print("  AGGREGATE STATISTICS")
    print("  " + sep)
    if "error" in aggregates:
        print(f"  {aggregates['error']}")
    else:
        print(f"  Contracts compared        : {aggregates['n_contracts']}")
        print(f"  Threshold                 : {aggregates['threshold']:.0%}")
        print()
        print(f"  Original mean pass rate   : {_pct(aggregates['orig_mean_pass_rate'])}")
        print(f"  Generated mean pass rate  : {_pct(aggregates['gen_mean_pass_rate'])}")
        print(f"  Generated best pass rate  : {_pct(aggregates['gen_best_mean_pass_rate'])}")
        print(f"  Mean delta (gen − orig)   : "
              f"{aggregates['mean_delta']:+.1%}" if aggregates.get("mean_delta") is not None
              else "  Mean delta (gen − orig)   :    n/a")
        print()
        print(f"  Contracts improved        : "
              f"{aggregates['contracts_improved']} / {aggregates['n_contracts']}"
              f"  ({aggregates['contracts_improved_pct']:.0%})")
        print(f"  Gen ≥ threshold (mean)    : "
              f"{aggregates['gen_above_threshold']} / {aggregates['n_contracts']}"
              f"  ({aggregates['gen_above_threshold_pct']:.0%})")
        print()
        print(f"  pass@1 (≥1 sample above)  : "
              f"{aggregates['pass_at_1']} / {aggregates['n_contracts']}"
              f"  ({aggregates['pass_at_1_pct']:.0%})")
        print(f"  pass@k (all samples above): "
              f"{aggregates['pass_at_k']} / {aggregates['n_contracts']}"
              f"  ({aggregates['pass_at_k_pct']:.0%})")
    print("  " + sep)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compare original vs generated contract replay pass rates.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--original-results", type=Path, default=Path(DEFAULT_ORIGINAL),
        dest="original_results",
        help="replay_results.json from replay_transactions.py.",
    )
    p.add_argument(
        "--generated-results", type=Path, default=Path(DEFAULT_GENERATED),
        dest="generated_results",
        help="generated_replay_results.json from replay_generated.py.",
    )
    p.add_argument(
        "--output", type=Path, default=Path(DEFAULT_OUTPUT),
        help="Output path for the comparison JSON.",
    )
    p.add_argument(
        "--pass-threshold", type=float, default=DEFAULT_THRESHOLD,
        dest="pass_threshold",
        help="Pass rate threshold for pass@1 / pass@k metrics.",
    )
    p.add_argument(
        "--sort", choices=["delta", "original", "generated", "name"],
        default="delta",
        help="Sort order for the comparison table.",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    for path, label in [
        (args.original_results,  "--original-results"),
        (args.generated_results, "--generated-results"),
    ]:
        if not path.exists():
            print(f"ERROR: {label} file not found: {path}")
            sys.exit(1)

    print(f"Loading original results  : {args.original_results}")
    print(f"Loading generated results : {args.generated_results}")

    original  = load_original_results(args.original_results)
    generated = load_generated_results(args.generated_results)

    print(f"  Original contracts      : {len(original)}")
    print(f"  Generated contracts     : {len(generated)}")

    # Addresses present in generated results — original is the reference set
    addresses = sorted(generated.keys())

    comparisons = []
    for addr in addresses:
        orig = original.get(addr)
        gen  = generated[addr]

        if orig is None:
            # Contract was generated but never replayed in the original run
            comparisons.append({
                "address":            addr,
                "contract_name":      gen.get("contract_name", addr[:12] + "…"),
                "original_state":     "NOT_RUN",
                "original_pass_rate": None,
                "compiled_samples":   gen.get("compiled_samples", 0),
                "samples_replayed":   gen.get("replayed", 0),
                "mean_gen_pass_rate": gen.get("mean_pass_rate"),
                "best_gen_pass_rate": gen.get("max_pass_rate"),
                "worst_gen_pass_rate": gen.get("min_pass_rate"),
                "delta":              None,
                "pass_at_1":          False,
                "pass_at_k":          False,
                "frac_samples_above_threshold": None,
                "samples":            [],
            })
        else:
            comparisons.append(
                compare_contract(addr, orig, gen, args.pass_threshold)
            )

    # Contracts in original but not in generated (never had samples generated)
    not_generated = sorted(set(original.keys()) - set(generated.keys()))
    if not_generated:
        print(f"\n  NOTE: {len(not_generated)} contract(s) in original results "
              f"have no generated samples and are excluded from comparison.")

    aggregates = compute_aggregates(comparisons, args.pass_threshold)

    # ── Write output ──────────────────────────────────────────────────────
    output = {
        "timestamp":        __import__("datetime").datetime.utcnow().isoformat(),
        "original_results": str(args.original_results),
        "generated_results": str(args.generated_results),
        "pass_threshold":   args.pass_threshold,
        "aggregates":       aggregates,
        "contracts":        comparisons,
    }
    args.output.write_text(json.dumps(output, indent=2))
    print(f"\nComparison written to {args.output}")

    print_comparison_table(comparisons, aggregates)


if __name__ == "__main__":
    main()