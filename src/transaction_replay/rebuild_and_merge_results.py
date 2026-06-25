#!/usr/bin/env python3
"""
USE THIS SCRIPT IF SOME CONTRACTS FAILED TO COMPILE USING replay_transactions.py
Why? Well, sometimes dependencies may be broken and you simply need a different hardhat workspace

rebuild_replay_results.py

Two jobs:
  1. REBUILD an aggregated replay_results.json from the per-contract
     checkpoints (replay_checkpoint_<k>.json), so a clobbered aggregate
     can be reconstructed without re-running anything.
  2. Provide a reusable merge_results() helper that loads an existing
     aggregate, updates entries by address, and writes the union — the
     behaviour replay_transactions.py / replay_generated.py should use so
     re-running a subset ADDS to the file instead of overwriting it.

Usage:
  # Rebuild the historical aggregate from checkpoints:
  python rebuild_replay_results.py \
      --dataset ../dataset/contracts_dataset_1to20 \
      --k 1000 \
      --out replay_results.json

  # Rebuild a per-model generated aggregate:
  python rebuild_replay_results.py \
      --dataset ../dataset/contracts_dataset_1to20 \
      --k 1000 \
      --checkpoint-name "replay_checkpoint_{k}.json" \
      --out generated_replay_results_Qwen3_6-27B-UD-Q4_K_XL_gguf.json
"""
import argparse, json, sys
from datetime import datetime, timezone
from pathlib import Path


def load_existing(path: Path) -> dict:
    """Load an existing aggregate, or return an empty skeleton."""
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"results": []}


def merge_results(existing: dict, new_results: list[dict]) -> dict:
    """
    Merge new per-contract results into an existing aggregate by address.
    New entries replace old ones for the same address; everything else is kept.
    Returns the merged dict (caller writes it).
    """
    by_addr = {}
    # existing first (so new ones overwrite)
    for r in existing.get("results", []):
        a = (r.get("address") or "").lower()
        if a:
            by_addr[a] = r
    for r in new_results:
        a = (r.get("address") or "").lower()
        if a:
            by_addr[a] = r

    merged = list(by_addr.values())
    done   = sum(1 for r in merged if r.get("state") == "DONE")
    failed = sum(1 for r in merged if r.get("state") == "FAILED")
    passed = sum(1 for r in merged if r.get("saved"))

    out = dict(existing)  # preserve config/timestamp keys if present
    out["results"]   = merged
    out["total"]     = len(merged)
    out["done"]      = done
    out["failed"]    = failed
    out["passed"]    = passed
    out["timestamp"] = datetime.now(timezone.utc).isoformat()
    return out


def rebuild_from_checkpoints(dataset: Path, k: int, ckpt_tmpl: str) -> list[dict]:
    """Walk each contract dir and collect its checkpoint result."""
    results = []
    ckpt_name = ckpt_tmpl.format(k=k)
    for cdir in sorted(p for p in dataset.iterdir() if p.is_dir()):
        ckpt = cdir / ckpt_name
        if not ckpt.exists():
            continue
        try:
            data = json.loads(ckpt.read_text())
            data.setdefault("address", cdir.name)
            results.append(data)
        except Exception as e:
            print(f"  [warn] {cdir.name}: {e}", file=sys.stderr)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--k", type=int, default=1000)
    ap.add_argument("--checkpoint-name", default="replay_checkpoint_{k}.json",
                    help="Checkpoint filename template; {k} is substituted.")
    ap.add_argument("--out", type=Path, required=True,
                    help="Aggregate file to write (merged with existing if present).")
    ap.add_argument("--no-merge", action="store_true",
                    help="Overwrite instead of merging with existing --out.")
    args = ap.parse_args()

    rebuilt = rebuild_from_checkpoints(args.dataset, args.k, args.checkpoint_name)
    print(f"Collected {len(rebuilt)} results from checkpoints.")

    existing = {"results": []} if args.no_merge else load_existing(args.out)
    merged = merge_results(existing, rebuilt)
    args.out.write_text(json.dumps(merged, indent=2))
    print(f"Wrote {merged['total']} results -> {args.out}  "
          f"(done={merged['done']} failed={merged['failed']} passed={merged['passed']})")


if __name__ == "__main__":
    main()