#!/usr/bin/env python3
"""
fix_dataset_hf_prefix.py

One-off fix: the earlier `dataset` push landed files at
    dataset/<address>/...
    dataset/addresses.txt
but they need to be at
    dataset/contracts_dataset_1to20/<address>/...
    dataset/contracts_dataset_1to20/addresses.txt

Uses CommitOperationCopy (server-side, references the existing blob by
hash — does NOT re-upload content) + CommitOperationDelete for the old
path, batched into commits to stay under any payload limits.

Usage:
    python fix_dataset_hf_prefix.py --dry-run
    python fix_dataset_hf_prefix.py
"""

from __future__ import annotations

import argparse

from huggingface_hub import HfApi, CommitOperationCopy, CommitOperationDelete

REPO_ID = "Giovanni-P/soldcr-artifacts"
REPO_TYPE = "dataset"

OLD_PREFIX = "dataset/"
NEW_PREFIX = "dataset/contracts_dataset_1to20/"

BATCH_SIZE = 500  # operations per commit (copy+delete count double this per file)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    api = HfApi()
    files = api.list_repo_files(REPO_ID, repo_type=REPO_TYPE)
    old_paths = [f for f in files if f.startswith(OLD_PREFIX) and not f.startswith(NEW_PREFIX)]

    print(f"Found {len(old_paths)} files under '{OLD_PREFIX}' to move to '{NEW_PREFIX}'")
    if not old_paths:
        print("Nothing to do.")
        return

    print("Sample of planned moves:")
    for p in old_paths[:3]:
        new_p = NEW_PREFIX + p[len(OLD_PREFIX):]
        print(f"  {p}  ->  {new_p}")
    print("  ...")

    if args.dry_run:
        print(f"\nDry run: would run {(len(old_paths) + BATCH_SIZE - 1) // BATCH_SIZE} commit(s), "
              f"{BATCH_SIZE} files (= {BATCH_SIZE*2} ops) per commit.")
        return

    for i in range(0, len(old_paths), BATCH_SIZE):
        batch = old_paths[i : i + BATCH_SIZE]
        ops = []
        for old_p in batch:
            new_p = NEW_PREFIX + old_p[len(OLD_PREFIX):]
            ops.append(CommitOperationCopy(src_path_in_repo=old_p, path_in_repo=new_p))
        for old_p in batch:
            ops.append(CommitOperationDelete(path_in_repo=old_p))

        print(f"Committing batch {i // BATCH_SIZE + 1} ({len(batch)} files, {len(ops)} ops) ...")
        api.create_commit(
            repo_id=REPO_ID,
            repo_type=REPO_TYPE,
            operations=ops,
            commit_message=f"Fix dataset/ prefix -> contracts_dataset_1to20/ (batch {i // BATCH_SIZE + 1})",
        )

    # Verify
    files_after = api.list_repo_files(REPO_ID, repo_type=REPO_TYPE)
    remaining_old = [f for f in files_after if f.startswith(OLD_PREFIX) and not f.startswith(NEW_PREFIX)]
    new_count = len([f for f in files_after if f.startswith(NEW_PREFIX)])
    print(f"\nDone. {new_count} files now under '{NEW_PREFIX}'. "
          f"{len(remaining_old)} still at the old flat path (should be 0).")


if __name__ == "__main__":
    main()