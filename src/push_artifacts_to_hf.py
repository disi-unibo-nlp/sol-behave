#!/usr/bin/env python3
"""
push_artifacts_to_hf.py

Manifest-driven upload of local artifact folders/files to the HF dataset
repo. Designed to extend to dcr_replay/, evaluation/, coverage_analysis/,
etc. by adding entries to ARTIFACT_MANIFEST below — no per-folder scripts.

Usage:
    pip install huggingface_hub
    hf auth login   # needs WRITE scope on Giovanni-P/soldcr-artifacts

    python push_artifacts_to_hf.py                  # push everything in manifest
    python push_artifacts_to_hf.py --only dcr_generation
    python push_artifacts_to_hf.py --dry-run
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
from pathlib import Path

from huggingface_hub import HfApi, upload_file, upload_folder

REPO_ID = "Giovanni-P/soldcr-artifacts"
REPO_TYPE = "dataset"

# Token resolution order: --token flag > HF_TOKEN env var > cached `hf auth login`.
# A 401 (not 403) on create_repo means no token was found at all -- distinct
# from a 403, which would mean a token was found but lacks write scope / repo access.

# Skipped everywhere, regardless of manifest entry.
GLOBAL_IGNORE = ["__pycache__/*", "*.pyc", "*.log", ".DS_Store"]

# ---------------------------------------------------------------------------
# Manifest: one entry per local folder/file -> HF path prefix.
# `kind` is "folder" or "file". `skip` is an explicit local-name blocklist
# for folders (in addition to GLOBAL_IGNORE), e.g. a superseded example dir.
# ---------------------------------------------------------------------------

ARTIFACT_MANIFEST = {
    "dcr_generation": {
        "local_root": "/workdir/src/dcr_generation",
        "entries": [
            {"kind": "folder", "name": "generated_dcr_1to20_Codestral-22B-v0_1-Q4_K_M_gguf"},
            {"kind": "folder", "name": "generated_dcr_1to20_Qwen_Qwen3-Coder-30B-A3B-Instruct_Q4_K_M_gguf"},
            {"kind": "folder", "name": "generated_dcr_1to20_Qwen3_6-27B-UD-Q4_K_XL_gguf"},
            {"kind": "folder", "name": "generated_dcr_1to20_gemma-4-12B-it-qat-UD-Q4_K_XL_gguf"},
            {"kind": "folder", "name": "generated_dcr_1to20_gemma-4-31B-it-qat-UD-Q4_K_XL_gguf"},
            {"kind": "folder", "name": "generated_dcr_1to20_mistralai_Devstral-Small-2-24B-Instruct-2512_Q4_K_M_gguf"},
            {"kind": "folder", "name": "generated_dcr_1to20_ornith-1_0-35b-Q4_K_M_gguf"},
            {"kind": "folder", "name": "dcr_example"},
            {"kind": "file", "name": "relation_stats_gemma.json"},
            {"kind": "file", "name": "relation_stats_Qwen3_6-27B.json"},
            {"kind": "file", "name": "drain_stats_gemma.json"},
            # running_example_OHMI intentionally excluded — skip, per instruction.
        ],
    },
    "dcr_replay": {
        "local_root": "/workdir/src/dcr_replay",
        # Push everything under this folder as-is (raw replay data, no
        # curation yet). Canonical-vs-superseded question for the
        # `_orig_`/`original_` naming families (pre-repair vs Jul 13
        # repaired-source run) is UNRESOLVED — revisit before treating
        # this as the public/curated release. See handoff notes.
        "whole_folder": True,
        "extra_ignore": [
            "grep_log.temp",
            "*.temp",
            # Case-study dirs excluded per instruction — not part of the
            # artifact push (Trusty / RelayReceiver here; OHMI elsewhere).
            "0x0007039b77d22042afc1a9c3b3da11837b730000/*",
            "0xa5f565650890fba1824ee0f21ebbbf660a179934/*",
        ],
    },
    "dataset": {
        "local_root": "/workdir/src/dataset/contracts_dataset_1to20",
        # Only the 250-contract benchmark subset (matches the paper's
        # claim: 250 contract dirs + addresses.txt = 251 entries).
        # contracts_dataset_pass_08 is the full/extended set, already
        # uploaded separately on its own HF repo — not duplicated here.
        # hardhat_workspace_crawl/replay are scratch dirs (npm deps etc.),
        # not dataset content — excluded by only pointing local_root here.
        #
        # hf_prefix nests under contracts_dataset_1to20/ to match the
        # naming already used in the external full-dataset HF repo — a
        # first push landed these flat at "dataset/*" and had to be
        # server-side moved after the fact (see fix_dataset_hf_prefix.py).
        "hf_prefix": "dataset/contracts_dataset_1to20",
        "whole_folder": True,
        "extra_ignore": [
            "*.py",
            "*.sh",
            "*.bak",
        ],
    },
    "differential_fuzzying": {
        "local_root": "/workdir/src/differential_fuzzying",
        "entries": [
            {"kind": "file", "name": "fuzzing_analysis_Codestral-22B-v0_1-Q4_K_M_gguf.json"},
            {"kind": "file", "name": "fuzzing_analysis_Qwen3_6-27B-UD-Q4_K_XL_gguf.json"},
            {"kind": "file", "name": "fuzzing_analysis_Qwen_Qwen3-Coder-30B-A3B-Instruct_Q4_K_M_gguf.json"},
            {"kind": "file", "name": "fuzzing_analysis_gemma-4-12B-it-qat-UD-Q4_K_XL_gguf.json"},
            {"kind": "file", "name": "fuzzing_analysis_gemma-4-31B-it-qat-UD-Q4_K_XL_gguf.json"},
            {"kind": "file", "name": "fuzzing_analysis_mistralai_Devstral-Small-2-24B-Instruct-2512_Q4_K_M_gguf.json"},
            {"kind": "file", "name": "fuzzing_analysis_ornith-1_0-35b-Q4_K_M_gguf.json"},
            # fuzzing_analysis.json (no suffix) excluded — Jun 24, predates
            # the Jul 7 per-model versions, same superseded-prototype
            # pattern as dcr_replay's unsuffixed root files.
            {"kind": "file", "name": "fuzzying_results_Codestral-22B-v0_1-Q4_K_M_gguf.tar.gz"},
            {"kind": "file", "name": "fuzzying_results_Qwen3_6-27B-UD-Q4_K_XL_gguf.tar.gz"},
            {"kind": "file", "name": "fuzzying_results_Qwen_Qwen3-Coder-30B-A3B-Instruct_Q4_K_M_gguf.tar.gz"},
            {"kind": "file", "name": "fuzzying_results_gemma-4-12B-it-qat-UD-Q4_K_XL_gguf.tar.gz"},
            {"kind": "file", "name": "fuzzying_results_gemma-4-31B-it-qat-UD-Q4_K_XL_gguf.tar.gz"},
            {"kind": "file", "name": "fuzzying_results_mistralai_Devstral-Small-2-24B-Instruct-2512_Q4_K_M_gguf.tar.gz"},
            {"kind": "file", "name": "fuzzying_results_ornith-1_0-35b-Q4_K_M_gguf.tar.gz"},
        ],
    },
    "transaction_replay": {
        "local_root": "/workdir/src/transaction_replay",
        "whole_folder": True,
        "extra_ignore": [
            "*.py",
            "*.sh",
            "*.bak",
            "*.patch",
            # Superseded pre-repair / prototype runs — same pattern
            # confirmed in dcr_replay (original_synth_*) and
            # differential_fuzzying (unsuffixed fuzzing_analysis.json).
            "original_historical_replay_results/*",
            "generated_replay_results.json",
            # coverage_results.json/coverage.log dated Jun 22-24, matching
            # the confirmed-legacy coverage_analysis/ folder timeframe —
            # superseded by evaluation/coverage/ (Jul 13-14).
            "coverage_results.json",
            "coverage.log",
            # Hardhat scratch workspaces — contain node_modules (confirmed).
            "hardhat_workspace_gen_replay/*",
            "hardhat_workspace_replay/*",
            "hardhat_workspace_replay_comp_failures/*",
        ],
    },
    "source_generation": {
        "local_root": "/workdir/src/source_generation",
        "whole_folder": True,
        "extra_ignore": [
            "*.py",
            "*.sh",
            "*.bak",
            # Granite variants — explored but abandoned before the repair
            # stage (no matching repaired_oneshot_granite-*/, incomplete
            # sample counts on 2 of 3, not in any reported table/figure).
            # Not part of the paper's 7-model set — exclude to avoid
            # implying 10 models were evaluated.
            "generated_oneshot_granite-20b-code-instruct_Q4_0_gguf/*",
            "generated_oneshot_granite-34b-code-instruct-Q4_K_M_gguf/*",
            "generated_oneshot_granite-4_1-8b-Q4_K_M_gguf/*",
            # Hardhat scratch workspace — contains node_modules (confirmed).
            "hardhat_workspace/*",
            # Logs, not wanted in the public artifact set.
            "generation_logs/*",
        ],
    },
    "human_eval": {
        "local_root": "/workdir/src/human_eval",
        "entries": [
            {"kind": "file", "name": "HumanEval_clean.xlsx"},
            # Original HumanEval.xlsx excluded — superseded by the
            # metadata-stripped HumanEval_clean.xlsx (creator/lastModifiedBy
            # scrubbed of personal email).
            {"kind": "file", "name": "metrics_summary.csv"},
            {"kind": "folder", "name": "dcr_annotation"},
            # build_dcr_annotation_sample.py, compute_metrics.py excluded —
            # code, not artifacts (consistent with every other folder).
        ],
    },
    # Add more folders here once confirmed.
}


def push_entry(api: HfApi, local_root: Path, hf_prefix: str, entry: dict, dry_run: bool) -> None:
    name = entry["name"]
    local_path = local_root / name
    hf_path = f"{hf_prefix}/{name}"

    if not local_path.exists():
        print(f"  [SKIP] {local_path} does not exist")
        return

    if entry["kind"] == "folder":
        print(f"  [{'DRY-RUN' if dry_run else 'UPLOAD'}] {local_path}/ -> {hf_path}/")
        if not dry_run:
            upload_folder(
                repo_id=REPO_ID,
                repo_type=REPO_TYPE,
                folder_path=str(local_path),
                path_in_repo=hf_path,
                ignore_patterns=GLOBAL_IGNORE,
            )
    else:
        print(f"  [{'DRY-RUN' if dry_run else 'UPLOAD'}] {local_path} -> {hf_path}")
        if not dry_run:
            upload_file(
                repo_id=REPO_ID,
                repo_type=REPO_TYPE,
                path_or_fileobj=str(local_path),
                path_in_repo=hf_path,
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Push only this manifest key (e.g. dcr_generation)")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without uploading")
    parser.add_argument("--workers", type=int, default=2,
                         help="Concurrent workers for upload_large_folder (default: 2, "
                              "down from the library default of 4 — fewer concurrent request "
                              "streams means fewer simultaneous 429 collisions against HF's "
                              "1000-req/5min quota; the tradeoff is somewhat slower throughput)")
    args = parser.parse_args()

    api = HfApi()
    if not args.dry_run:
        api.create_repo(repo_id=REPO_ID, repo_type=REPO_TYPE, exist_ok=True)

    keys = [args.only] if args.only else list(ARTIFACT_MANIFEST.keys())

    for key in keys:
        if key not in ARTIFACT_MANIFEST:
            print(f"Unknown manifest key: {key}")
            continue
        spec = ARTIFACT_MANIFEST[key]
        local_root = Path(spec["local_root"])
        print(f"\n== {key} ({local_root}) ==")

        if spec.get("whole_folder"):
            if not local_root.exists():
                print(f"  [SKIP] {local_root} does not exist")
                continue
            ignore = GLOBAL_IGNORE + spec.get("extra_ignore", [])
            hf_prefix = spec.get("hf_prefix", key)

            if args.dry_run:
                print(f"  [DRY-RUN] {local_root}/ (whole folder) -> {hf_prefix}/")
                print(f"    ignore patterns: {ignore}")
                included, excluded = [], []
                for item in sorted(local_root.iterdir()):
                    rel = item.name + ("/" if item.is_dir() else "")
                    # Match both the bare name and name/* (for dir-content patterns)
                    is_ignored = any(
                        fnmatch.fnmatch(item.name, pat)
                        or fnmatch.fnmatch(rel, pat)
                        or fnmatch.fnmatch(f"{item.name}/dummy", pat)
                        for pat in ignore
                    )
                    (excluded if is_ignored else included).append(rel)
                print(f"    would upload {len(included)} top-level entries:")
                for r in included:
                    print(f"      + {r}")
                if excluded:
                    print(f"    excluding {len(excluded)} top-level entries:")
                    for r in excluded:
                        print(f"      - {r}")
                continue

            # upload_large_folder has NO path_in_repo param — it mirrors
            # folder_path's *contents* straight to the repo root. To land
            # under "<key>/..." instead of the repo root, stage the data
            # under a real directory named <key>.
            #
            # NOTE: a symlink here does NOT work — upload_large_folder's
            # walk does not follow symlinked directories (confirmed: it
            # reported 0/0 files against a symlinked staging dir). Use
            # `cp -al` (hardlinks, not a data copy) instead. Staging lives
            # as a sibling of local_root so hardlinks stay on the same
            # filesystem, and the path is fixed (not a random tempdir) so
            # a second run resumes from upload_large_folder's own cache
            # instead of starting over.
            staging_root = local_root.parent / ".hf_stage"
            staging_root.mkdir(exist_ok=True)
            staged = staging_root / hf_prefix
            staged.parent.mkdir(parents=True, exist_ok=True)
            if not staged.exists():
                print(f"    staging via hardlinks: {local_root} -> {staged}")
                subprocess.run(
                    ["cp", "-al", str(local_root), str(staged)],
                    check=True,
                )
            else:
                print(f"    staging dir already exists, reusing: {staged}")

            # Physically prune anything matching a top-level "<dirname>/*"
            # ignore pattern from the STAGED copy (cheap — hardlinks only,
            # doesn't touch local_root). This matters beyond just skipping
            # unwanted uploads: if upload_large_folder's internal walk
            # enumerates the full tree before applying ignore_patterns, a
            # stray node_modules/ left in place can make the walk itself
            # take forever (or look hung) on folders that embed Hardhat/npm
            # scratch workspaces. Pruning first guarantees the walker never
            # sees those subtrees at all.
            for pat in spec.get("extra_ignore", []):
                if pat.endswith("/*"):
                    dirname = pat[:-2]
                    target = staged / dirname
                    if target.exists():
                        print(f"    pruning from staged copy (not local_root): {target}")
                        subprocess.run(["rm", "-rf", str(target)], check=True)

            # Patterns matched fnmatch-style against the path relative to
            # folder_path (= staging_root), so prefix with "<hf_prefix>/".
            staged_ignore = [f"{hf_prefix}/{p}" for p in ignore]

            print(f"  [UPLOAD] {local_root}/ (whole folder, large-folder mode, resumable) -> {hf_prefix}/")
            api.upload_large_folder(
                repo_id=REPO_ID,
                repo_type=REPO_TYPE,
                folder_path=str(staging_root),
                allow_patterns=[f"{hf_prefix}/**"],
                ignore_patterns=staged_ignore,
                num_workers=args.workers,
            )
            continue

        for entry in spec["entries"]:
            push_entry(api, local_root, hf_prefix=key, entry=entry, dry_run=args.dry_run)

    print("\nDone." if not args.dry_run else "\nDry run complete — no files uploaded.")


if __name__ == "__main__":
    main()