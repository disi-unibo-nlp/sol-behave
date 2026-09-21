#!/usr/bin/env python3
"""
reconstruct_fuzzing_results.py

Rebuilds the top-level fuzzing_results.json for a differential_fuzzing.py
--diff-dir when it's missing (e.g. because --results-file wasn't passed and
the argparse default silently pointed at a different model's directory).

Safe by construction: fuzz_contract() writes each contract's exact result
dict to <diff_dir>/<address>/result.json, which is the same dict main()
would have appended to all_results and wrapped in fuzzing_results.json.
This script just re-collects those per-contract files; it re-runs nothing.

Usage:
    python3 reconstruct_fuzzying_results.py --diff-dir fuzzying_results_Codestral-22B-v0_1-Q4_K_M_gguf
    python3 reconstruct_fuzzying_results.py --diff-dir fuzzying_results_Codestral-22B-v0_1-Q4_K_M_gguf --output custom_name.json
    python3 reconstruct_fuzzying_results.py --diff-dir fuzzying_results_Qwen3_6-27B-UD-Q4_K_XL_gguf
"""

import argparse
import json
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--diff-dir", type=Path, required=True,
                    help="The differential_fuzzing.py --diff-dir to reconstruct.")
    p.add_argument("--output", type=Path, default=None,
                    help="Output path (default: <diff-dir>/fuzzing_results.json)")
    args = p.parse_args()

    if not args.diff_dir.exists():
        print(f"ERROR: diff-dir not found: {args.diff_dir}")
        return 1

    output = args.output or (args.diff_dir / "fuzzing_results.json")

    all_results = []
    n_done = n_failed = n_skipped = 0
    for contract_dir in sorted(args.diff_dir.iterdir()):
        if not contract_dir.is_dir():
            continue
        result_path = contract_dir / "result.json"
        if not result_path.exists():
            n_skipped += 1
            continue
        try:
            result = json.loads(result_path.read_text())
        except Exception as e:
            print(f"  WARNING: could not parse {result_path}: {e}")
            n_skipped += 1
            continue
        all_results.append(result)
        if result.get("state") == "DONE":
            n_done += 1
        elif result.get("state") == "FAILED":
            n_failed += 1

    if not all_results:
        print(f"ERROR: no per-contract result.json files found under {args.diff_dir}")
        return 1

    # config/timestamp/duration are not read by fuzzing_analysis.py (it only
    # reads data["results"]), so we don't try to recover them precisely.
    # If you want them, they can be read from any <diff_dir>/<addr>/sample_*/*/echidna.yaml.
    wrapper = {
        "timestamp":        None,
        "reconstructed":    True,
        "reconstructed_note": "Rebuilt from per-contract result.json files; "
                               "original fuzzing_results.json was missing "
                               "(argparse --results-file default bug).",
        "config": {
            "test_limit":  None,
            "seq_len":     None,
            "timeout":     None,
            "n_contracts": len(all_results),
        },
        "duration_seconds": None,
        "results": all_results,
    }

    output.write_text(json.dumps(wrapper, indent=2))
    print(f"Reconstructed {len(all_results)} contracts "
          f"(done={n_done}, failed={n_failed}, skipped={n_skipped})")
    print(f"Written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())