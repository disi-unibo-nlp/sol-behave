#!/usr/bin/env python3
"""
overlap_table.py

Fills Table~\\ref{tab:overlap} (divergent contracts by combination of oracles
that flag them), per model.

Oracles:
  O1  differential fuzzing  -- fuzzing_analysis_<model>.json
      flagged if ANY fn_pass_rate < 1.0 for a 'done' contract
      (matches: "at least one fuzzable function is non-equivalent in at
      least one compiling sample")
  O2  historical replay     -- depth_historical/replay_depth_HISTORICAL_<model>.json
      flagged if status_match_rate < 1.0   (same rule as build_final_tables.py's
      diverged_set())
  O3  synthetic replay, REFERENCE-EXTRACTOR (fixed Gemma-4-31B) oracle --
      depth_synthetic_gemma/replay_depth_SYNTHETIC_GEMMA_<model>.json
      same flagging rule as O2

N = contracts evaluated by ALL THREE oracles simultaneously (intersection
of each oracle's own evaluated population), NOT any single oracle's N.
Expect N to be well below O1's 244, since O2/O3's own N per model
(h_n / g_n in final_tables.json) is already smaller.

Usage:
    python3 overlap_table.py \\
        --fuzz-dir  /workdir/src/differential_fuzzying \\
        --eval-root /workdir/src/evaluation
"""
import argparse
import json
from pathlib import Path

MODELS = [
    ("gemma-4-31B-it-qat-UD-Q4_K_XL.gguf",                       "Gemma-4-31B"),
    ("Qwen3.6-27B-UD-Q4_K_XL.gguf",                              "Qwen3.6-27B"),
    ("Qwen.Qwen3-Coder-30B-A3B-Instruct.Q4_K_M.gguf",            "Qwen3-Coder-30B"),
    ("gemma-4-12B-it-qat-UD-Q4_K_XL.gguf",                       "Gemma-4-12B"),
    ("ornith-1.0-35b-Q4_K_M.gguf",                               "Ornith-35B"),
    ("Codestral-22B-v0.1-Q4_K_M.gguf",                           "Codestral-22B"),
    ("mistralai.Devstral-Small-2-24B-Instruct-2512.Q4_K_M.gguf", "Devstral-24B"),
]


def sanitize(name: str) -> str:
    return name.replace(".", "_").replace(" ", "_")


def _load(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def o1_sets(fuzz_json: dict) -> tuple[set[str], set[str]]:
    """Return (evaluated, flagged) address sets from fuzzing_analysis_<model>.json."""
    evaluated, flagged = set(), set()
    for c in fuzz_json.get("contracts", []):
        if c.get("status") != "done":
            continue
        addr = c["address"].lower()
        evaluated.add(addr)
        fn_rates = c.get("fn_pass_rates", {})
        if any(rate < 1.0 for rate in fn_rates.values()):
            flagged.add(addr)
    return evaluated, flagged


def depth_sets(depth_json: dict) -> tuple[set[str], set[str]]:
    """Return (evaluated, flagged) address sets from a depth_*.json file."""
    if not depth_json:
        return set(), set()
    evaluated, flagged = set(), set()
    for c in depth_json.get("contracts", []):
        if c.get("status_match_rate") is None:
            continue
        addr = c["address"].lower()
        evaluated.add(addr)
        if c["status_match_rate"] < 1.0:
            flagged.add(addr)
    return evaluated, flagged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fuzz-dir", type=Path, required=True)
    ap.add_argument("--eval-root", type=Path, required=True)
    ap.add_argument("--output", type=Path, default=None,
                     help="optional JSON dump of full per-model breakdown")
    args = ap.parse_args()

    D_HIST = args.eval_root / "depth_historical"
    D_GEM  = args.eval_root / "depth_synthetic_gemma"

    header = (f"{'Model':<18}{'N':>5}  "
              f"{'O1':>4}{'O2':>4}{'O3':>4}  "
              f"{'O1O2':>5}{'O1O3':>5}{'O2O3':>5}  {'all':>4}")
    print(header)
    print("-" * len(header))

    all_rows = []
    for model, short in MODELS:
        safe = sanitize(model)

        fuzz = _load(args.fuzz_dir / f"fuzzing_analysis_{safe}.json")
        hist = _load(D_HIST / f"replay_depth_HISTORICAL_{safe}.json")
        gem  = _load(D_GEM  / f"replay_depth_SYNTHETIC_GEMMA_{safe}.json")

        if not fuzz or not hist or not gem:
            print(f"{short:<18}  MISSING INPUT (fuzz={bool(fuzz)} hist={bool(hist)} gem={bool(gem)})")
            continue

        o1_eval, o1_flag = o1_sets(fuzz)
        o2_eval, o2_flag = depth_sets(hist)
        o3_eval, o3_flag = depth_sets(gem)

        N = o1_eval & o2_eval & o3_eval

        s1 = o1_flag & N
        s2 = o2_flag & N
        s3 = o3_flag & N

        only_o1 = s1 - s2 - s3
        only_o2 = s2 - s1 - s3
        only_o3 = s3 - s1 - s2
        o1o2    = (s1 & s2) - s3
        o1o3    = (s1 & s3) - s2
        o2o3    = (s2 & s3) - s1
        all3    = s1 & s2 & s3

        row = {
            "model": short, "N": len(N),
            "O1": len(only_o1), "O2": len(only_o2), "O3": len(only_o3),
            "O1O2": len(o1o2), "O1O3": len(o1o3), "O2O3": len(o2o3),
            "all": len(all3),
            "_addrs": {  # for --output only
                "only_o1": sorted(only_o1), "only_o2": sorted(only_o2), "only_o3": sorted(only_o3),
                "o1o2": sorted(o1o2), "o1o3": sorted(o1o3), "o2o3": sorted(o2o3),
                "all": sorted(all3),
            },
        }
        all_rows.append(row)

        print(f"{short:<18}{len(N):>5}  "
              f"{len(only_o1):>4}{len(only_o2):>4}{len(only_o3):>4}  "
              f"{len(o1o2):>5}{len(o1o3):>5}{len(o2o3):>5}  {len(all3):>4}")

    if args.output:
        args.output.write_text(json.dumps(all_rows, indent=2))
        print(f"\nFull breakdown (incl. addresses) written to {args.output}")


if __name__ == "__main__":
    main()