"""
build_table.py

Fills the Table~\ref{tab:fuzzing} O1 differential-fuzzing table from the
already-computed fuzzing_analysis_<model>.json files (output of
fuzzing_analysis.py). No re-run of echidna or differential_fuzzing.py needed.

N              = aggregates["n_contracts"]           (exact match to caption)
Contracts      = aggregates["pass_at_k_pct"]          (exact match to caption)
Functions      = POOLED (function, sample) pair ratio, NOT the same as
                 aggregates["mean_fn_pass_rate_all"], which equal-weights
                 each function regardless of its contract's k / n_fns.
                 Recomputed here from each contract's raw k + fn_pass_rates.

Usage:
    python3 build_table.py fuzzing_analysis_*.json
"""
import json, sys
from pathlib import Path

MODEL_LABELS = {
    "gemma-4-31B-it-qat-UD-Q4_K_XL_gguf":                       "Gemma-4-31B",
    "Qwen3_6-27B-UD-Q4_K_XL_gguf":                               "Qwen3.6-27B",
    "Qwen_Qwen3-Coder-30B-A3B-Instruct_Q4_K_M_gguf":             "Qwen3-Coder-30B",
    "gemma-4-12B-it-qat-UD-Q4_K_XL_gguf":                        "Gemma-4-12B",
    "ornith-1_0-35b-Q4_K_M_gguf":                                "Ornith-35B",
    "Codestral-22B-v0_1-Q4_K_M_gguf":                            "Codestral-22B",
    "mistralai_Devstral-Small-2-24B-Instruct-2512_Q4_K_M_gguf":  "Devstral-24B",
}

def pooled_function_ratio(contracts: list[dict]) -> tuple[int, int]:
    """Return (equivalent_pairs, total_pairs) pooled across all done contracts."""
    eq_total, pair_total = 0, 0
    for c in contracts:
        if c.get("status") != "done":
            continue
        k = c.get("k", 0)
        fn_rates = c.get("fn_pass_rates", {})
        if not k or not fn_rates:
            continue
        pair_total += k * len(fn_rates)
        eq_total   += sum(round(rate * k) for rate in fn_rates.values())
    return eq_total, pair_total

def main():
    paths = sys.argv[1:]
    if not paths:
        print("Usage: python3 build_table.py fuzzing_analysis_*.json")
        return 1

    print(f"{'Model':<20} {'N':>5} {'Functions':>10} {'Contracts':>10}")
    print("-" * 48)
    for p in paths:
        data = json.loads(Path(p).read_text())
        agg = data["aggregates"]
        contracts = data["contracts"]

        # derive the model key from the filename
        stem = Path(p).stem.replace("fuzzing_analysis_", "")
        label = MODEL_LABELS.get(stem, stem)

        n = agg["n_contracts"]
        eq, tot = pooled_function_ratio(contracts)
        functions_pct = eq / tot if tot else float("nan")
        contracts_pct = agg["pass_at_k_pct"]

        # sanity check against the (slightly different) unweighted field
        unweighted = agg.get("mean_fn_pass_rate_all")
        flag = ""
        if unweighted is not None and abs(unweighted - functions_pct) > 0.01:
            flag = f"  (unweighted mean_fn_pass_rate_all={unweighted:.4f}, differs)"

        print(f"{label:<20} {n:>5} {functions_pct:>10.2%} {contracts_pct:>10.2%}{flag}")

if __name__ == "__main__":
    raise SystemExit(main())