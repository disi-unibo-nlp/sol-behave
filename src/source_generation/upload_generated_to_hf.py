#!/usr/bin/env python3
"""
upload_generated_to_hf.py
=========================

Pack the four models' generated contracts into one HuggingFace dataset.

Layout:
  /workdir/src/source_generation/
    generated_oneshot_<MODEL>/
      <contract_id>/
        sample_1.sol | sample_1.json ... (the 10 generations, loaded AS-IS)
        <result>.json                   (per-contract result, loaded AS-IS)

One row per (model, contract, sample). Files are stored as raw text — no
parsing, no extraction. The result file's raw text is attached to every
sample row of that contract.

Usage:
  pip install datasets huggingface_hub tqdm --break-system-packages
  hf auth login                         # token with WRITE scope (repo is private)
  python3 upload_generated_to_hf.py --repo Giovanni-P/GeneratedContracts --dry-run
  python3 upload_generated_to_hf.py --repo Giovanni-P/GeneratedContracts
"""

import argparse
import json
import re
from pathlib import Path

from tqdm import tqdm

GEN_ROOT = Path("/workdir/src/source_generation")

MODEL_FOLDERS = {
    "generated_oneshot_gemma-4-31B-it-qat-UD-Q4_K_XL_gguf":
        "gemma-4-31B-it-qat",
    "generated_oneshot_mistralai_Devstral-Small-2-24B-Instruct-2512_Q4_K_M_gguf":
        "Devstral-Small-2-24B-Instruct-2512",
    "generated_oneshot_Qwen_Qwen3-Coder-30B-A3B-Instruct_Q4_K_M_gguf":
        "Qwen3-Coder-30B-A3B-Instruct",
    "generated_oneshot_Qwen3_6-27B-UD-Q4_K_XL_gguf":
        "Qwen3.6-27B",
}


def _read_text(p: Path) -> str:
    """Read a file as raw text. No JSON parsing — load as-is."""
    return p.read_text(errors="replace")


def _is_sample(name: str) -> bool:
    # a generation file: sample_*.sol / sample_*.json (adjust here if named differently)
    return bool(re.match(r"sample_\d+\.(sol|json)$", name))


def _is_result(name: str) -> bool:
    # the per-contract result file: any .json that isn't a sample_*.json
    return name.endswith(".json") and not _is_sample(name)


def collect_rows() -> list[dict]:
    rows = []
    for folder, label in MODEL_FOLDERS.items():
        model_dir = GEN_ROOT / folder
        if not model_dir.exists():
            print(f"  [skip] missing folder: {model_dir}")
            continue

        contract_dirs = sorted(d for d in model_dir.iterdir() if d.is_dir())

        for cdir in tqdm(contract_dirs, desc=label, unit="contract"):
            files = list(cdir.iterdir())

            # result file: first .json that is not a sample
            result_text = None
            for f in files:
                if f.is_file() and _is_result(f.name):
                    result_text = _read_text(f)
                    break

            # sample generations, loaded verbatim
            sample_files = sorted(
                f for f in files if f.is_file() and _is_sample(f.name)
            )
            for sp in sample_files:
                m = re.search(r"sample_(\d+)", sp.name)
                rows.append({
                    "model":       label,
                    "contract_id": cdir.name,
                    "sample_id":   int(m.group(1)) if m else -1,
                    "filename":    sp.name,
                    "source":      _read_text(sp),   # raw .sol/.json text
                    "result":      result_text,      # raw result .json text
                })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True,
                    help="HF dataset repo id, e.g. Giovanni-P/GeneratedContracts")
    ap.add_argument("--private", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--save-local", type=Path, default=None)
    args = ap.parse_args()

    print("Collecting rows ...")
    rows = collect_rows()
    print(f"\nTotal rows: {len(rows)}")
    if not rows:
        print("Nothing collected — check folder names / sample patterns.")
        return

    from collections import Counter
    for mdl, n in Counter(r["model"] for r in rows).items():
        print(f"  {mdl:<40} {n} samples")

    from datasets import Dataset
    ds = Dataset.from_list(rows)

    if args.save_local:
        ds.save_to_disk(str(args.save_local))
        print(f"Saved locally to {args.save_local}")

    if args.dry_run:
        ex = dict(ds[0])
        ex["source"] = (ex["source"] or "")[:200] + " ...[trunc]"
        if ex.get("result"):
            ex["result"] = ex["result"][:200] + " ...[trunc]"
        print("\n[dry-run] example row:")
        print(json.dumps(ex, indent=2))
        return

    print(f"\nPushing to {args.repo} ...")
    ds.push_to_hub(args.repo, private=args.private)
    print("Done.")


if __name__ == "__main__":
    main()