#!/usr/bin/env python3
"""
diagnose_fuzzing_gaps.py

Explains why a differential_fuzzing.py run has fewer contracts than expected
(e.g. 219 instead of 250 or 244), by cross-referencing three sources:

  1. The full dataset (--dataset): all contracts that exist at all.
  2. The generated samples dir (--generated): contracts that would actually
     be discovered by differential_fuzzing.py's main() (i.e. have at least
     one sample_N.sol/.json AND a matching dataset dir).
  3. The diff-dir results (--diff-dir): contracts that have a per-contract
     result.json, split into DONE / FAILED (with reason breakdown).

It also flags a silent-corruption risk: contracts that were fuzzed but
have no replay_checkpoint_<k>.json in the dataset dir, meaning
fuzz_contract() silently used constructor_args=[] instead of the real
mainnet constructor arguments.

Usage:
    python3 diagnose_fuzzing_gaps.py \
        --dataset ../dataset/contracts_dataset_1to20 \
        --generated generated_oneshot_Codestral-22B-v0_1-Q4_K_M_gguf \
        --diff-dir  ../differential_fuzzying/fuzzying_results_Codestral-22B-v0_1-Q4_K_M_gguf \
        --replay-k 1000
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path


def discover_gen_dirs(dataset_dir: Path, generated_dir: Path) -> set[str]:
    """Replicates differential_fuzzing.py's main() gen_dirs discovery."""
    found = set()
    if not generated_dir.exists():
        return found
    for d in generated_dir.iterdir():
        if not d.is_dir():
            continue
        has_sample = any(re.match(r"sample_\d+\.(sol|json)$", f.name) for f in d.iterdir())
        has_dataset_match = (dataset_dir / d.name).exists()
        if has_sample and has_dataset_match:
            found.add(d.name)
    return found


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--generated", type=Path, required=True)
    p.add_argument("--diff-dir", type=Path, required=True)
    p.add_argument("--replay-k", type=int, default=1000)
    args = p.parse_args()

    # 1. Full dataset
    all_contracts = {d.name for d in args.dataset.iterdir()
                      if d.is_dir() and (d / "metadata.json").exists()}
    print(f"Full dataset                         : {len(all_contracts)} contracts")

    # 2. What differential_fuzzing.py would have discovered
    discovered = discover_gen_dirs(args.dataset, args.generated)
    never_discovered = all_contracts - discovered
    print(f"Discovered by differential_fuzzing.py: {len(discovered)} contracts")
    print(f"Never discovered (no compiled sample "
          f"or no matching dataset dir): {len(never_discovered)}")

    # 3. What's actually in diff-dir
    done, failed_reasons, present = [], Counter(), set()
    if args.diff_dir.exists():
        for contract_dir in args.diff_dir.iterdir():
            if not contract_dir.is_dir():
                continue
            result_path = contract_dir / "result.json"
            if not result_path.exists():
                continue
            present.add(contract_dir.name)
            try:
                r = json.loads(result_path.read_text())
            except Exception:
                continue
            if r.get("state") == "DONE":
                done.append(contract_dir.name)
            elif r.get("state") == "FAILED":
                failed_reasons[r.get("reason", "?")] += 1

    discovered_but_no_result = discovered - present
    print(f"\nIn diff-dir with a result.json       : {len(present)}")
    print(f"  DONE                                : {len(done)}")
    print(f"  FAILED (breakdown below)            : {sum(failed_reasons.values())}")
    print(f"Discovered but NO result.json at all "
          f"(likely interrupted run)              : {len(discovered_but_no_result)}")

    if failed_reasons:
        print("\nFAILED reason breakdown:")
        for reason, count in failed_reasons.most_common():
            print(f"  {count:>4}×  {reason}")

    print(f"\n{'='*60}")
    print(f"RECONCILIATION")
    print(f"{'='*60}")
    print(f"  Full dataset                : {len(all_contracts)}")
    print(f"  - never discovered          : {len(never_discovered)}")
    print(f"  - discovered, no result.json: {len(discovered_but_no_result)}")
    print(f"  - discovered, FAILED        : {sum(failed_reasons.values())}")
    print(f"  = discovered, DONE          : {len(done)}")
    check = (len(all_contracts) - len(never_discovered)
             - len(discovered_but_no_result) - sum(failed_reasons.values()))
    print(f"  (sanity check, should match DONE count above: {check})")

    # 4. Constructor-args silent-corruption risk, among DONE contracts
    missing_ckpt = []
    for addr in done:
        ckpt = args.dataset / addr / f"replay_checkpoint_{args.replay_k}.json"
        if not ckpt.exists():
            missing_ckpt.append(addr)
        else:
            try:
                ck = json.loads(ckpt.read_text())
                if not ck.get("constructor_args"):
                    missing_ckpt.append(addr)
            except Exception:
                missing_ckpt.append(addr)

    print(f"\n{'='*60}")
    print(f"CONSTRUCTOR-ARGS RISK (among DONE contracts)")
    print(f"{'='*60}")
    print(f"  DONE contracts with no/empty replay_checkpoint_{args.replay_k}.json:")
    print(f"    {len(missing_ckpt)} / {len(done)}")
    if missing_ckpt:
        print(f"  These were fuzzed with constructor_args=[] — likely wrong")
        print(f"  initial state if the real contract's constructor takes args.")
        print(f"  Affected addresses (first 20):")
        for a in missing_ckpt[:20]:
            print(f"    {a}")

    # 5. Sample of never-discovered contracts, for spot-checking
    if never_discovered:
        print(f"\nSample of never-discovered contracts (first 10, check compile@10):")
        for a in sorted(never_discovered)[:10]:
            print(f"    {a}")


if __name__ == "__main__":
    main()