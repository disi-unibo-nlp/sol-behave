#!/usr/bin/env python3
"""
cleanup_differential_fuzzying_leftovers.py

The earlier `--only differential_fuzzying` push (before we switched to
tar.gz archives) used upload_large_folder, which commits incrementally.
It was killed mid-run due to rate limiting, but whatever had already
landed is still in the repo as leftover raw per-contract files —
now redundant with the tar.gz archives.

This finds anything under differential_fuzzying/ that is NOT one of the
14 expected flat files (7 fuzzing_analysis_*.json + 7 *.tar.gz) and
deletes it in batched commits.

Usage:
    python cleanup_differential_fuzzying_leftovers.py --dry-run
    python cleanup_differential_fuzzying_leftovers.py
"""

from __future__ import annotations

import argparse
import time

from huggingface_hub import HfApi, CommitOperationDelete

REPO_ID = "Giovanni-P/soldcr-artifacts"
REPO_TYPE = "dataset"
PREFIX = "differential_fuzzying/"

EXPECTED = {
    PREFIX + n for n in [
        "fuzzing_analysis_Codestral-22B-v0_1-Q4_K_M_gguf.json",
        "fuzzing_analysis_Qwen3_6-27B-UD-Q4_K_XL_gguf.json",
        "fuzzing_analysis_Qwen_Qwen3-Coder-30B-A3B-Instruct_Q4_K_M_gguf.json",
        "fuzzing_analysis_gemma-4-12B-it-qat-UD-Q4_K_XL_gguf.json",
        "fuzzing_analysis_gemma-4-31B-it-qat-UD-Q4_K_XL_gguf.json",
        "fuzzing_analysis_mistralai_Devstral-Small-2-24B-Instruct-2512_Q4_K_M_gguf.json",
        "fuzzing_analysis_ornith-1_0-35b-Q4_K_M_gguf.json",
        "fuzzying_results_Codestral-22B-v0_1-Q4_K_M_gguf.tar.gz",
        "fuzzying_results_Qwen3_6-27B-UD-Q4_K_XL_gguf.tar.gz",
        "fuzzying_results_Qwen_Qwen3-Coder-30B-A3B-Instruct_Q4_K_M_gguf.tar.gz",
        "fuzzying_results_gemma-4-12B-it-qat-UD-Q4_K_XL_gguf.tar.gz",
        "fuzzying_results_gemma-4-31B-it-qat-UD-Q4_K_XL_gguf.tar.gz",
        "fuzzying_results_mistralai_Devstral-Small-2-24B-Instruct-2512_Q4_K_M_gguf.tar.gz",
        "fuzzying_results_ornith-1_0-35b-Q4_K_M_gguf.tar.gz",
    ]
}

BATCH_SIZE = 300  # deletes per commit — conservative given the earlier 429s
SLEEP_BETWEEN_BATCHES = 3  # seconds, extra safety margin against rate limits


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    api = HfApi()
    print("Listing repo files (may take a while if there are many leftovers) ...")
    files = api.list_repo_files(REPO_ID, repo_type=REPO_TYPE)
    under_prefix = [f for f in files if f.startswith(PREFIX)]
    leftovers = [f for f in under_prefix if f not in EXPECTED]
    kept = [f for f in under_prefix if f in EXPECTED]

    print(f"{len(under_prefix)} files under '{PREFIX}'")
    print(f"  {len(kept)} match the expected 14 flat files (kept)")
    print(f"  {len(leftovers)} are leftovers to delete")

    missing_expected = EXPECTED - set(kept)
    if missing_expected:
        print(f"\nWARNING: {len(missing_expected)} expected files are NOT present yet:")
        for m in sorted(missing_expected):
            print(f"  {m}")
        print("(Fine if the real push just hasn't run/finished yet — otherwise investigate.)")

    if not leftovers:
        print("\nNothing to clean up.")
        return

    print("\nSample leftovers:")
    for p in leftovers[:5]:
        print(f"  {p}")
    print("  ...")

    n_batches = (len(leftovers) + BATCH_SIZE - 1) // BATCH_SIZE
    if args.dry_run:
        print(f"\nDry run: would delete {len(leftovers)} files in {n_batches} commit(s) "
              f"of up to {BATCH_SIZE} each, {SLEEP_BETWEEN_BATCHES}s between batches.")
        return

    for i in range(0, len(leftovers), BATCH_SIZE):
        batch = leftovers[i : i + BATCH_SIZE]
        ops = [CommitOperationDelete(path_in_repo=p) for p in batch]
        batch_num = i // BATCH_SIZE + 1
        print(f"Deleting batch {batch_num}/{n_batches} ({len(batch)} files) ...")
        api.create_commit(
            repo_id=REPO_ID,
            repo_type=REPO_TYPE,
            operations=ops,
            commit_message=f"Remove leftover partial-upload files from differential_fuzzying/ (batch {batch_num})",
        )
        if i + BATCH_SIZE < len(leftovers):
            time.sleep(SLEEP_BETWEEN_BATCHES)

    files_after = api.list_repo_files(REPO_ID, repo_type=REPO_TYPE)
    remaining = [f for f in files_after if f.startswith(PREFIX)]
    print(f"\nDone. {len(remaining)} files now under '{PREFIX}' (expected: {len(EXPECTED)} once the real push lands).")


if __name__ == "__main__":
    main()