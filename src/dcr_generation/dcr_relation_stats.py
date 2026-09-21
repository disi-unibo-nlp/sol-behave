#!/usr/bin/env python3
"""
dcr_relation_stats.py

Reports statistics on DCR relation-type usage across a directory of
generated source_dcr.json files: presence (how many contracts contain
>=1 instance of each type), volume (total instances per type), the
distribution of type-combinations per contract, and relations-per-contract
stats.

Usage:
    python3 dcr_relation_stats.py [DIR] [--functions-csv PATH] [--json-out PATH]

    DIR defaults to ./generated_dcr_1to20_Qwen3_6-27B-UD-Q4_K_XL_gguf
    (relative to the current working directory), matching the layout under
    /workdir/src/dcr_generation/.

Run from /workdir/src/dcr_generation/, e.g.:
    python3 dcr_relation_stats.py
    python3 dcr_relation_stats.py generated_dcr_1to20_Qwen3_6-27B-UD-Q4_K_XL_gguf

    python3 dcr_relation_stats.py generated_dcr_1to20_gemma-4-31B-it-qat-UD-Q4_K_XL_gguf

    python3 dcr_relation_stats.py generated_dcr_1to20_gemma-4-31B-it-qat-UD-Q4_K_XL_gguf/ --json-out relation_stats_gemma.json
"""

import argparse
import glob
import json
import os
import statistics as st
import sys
from collections import Counter


DEFAULT_DIR = "generated_dcr_1to20_Qwen3_6-27B-UD-Q4_K_XL_gguf"


def load_relations(dcr_dir):
    """Yield (address, relations_list, num_branch_functions) for each contract dir."""
    pattern = os.path.join(dcr_dir, "*", "source_dcr.json")
    for f in sorted(glob.glob(pattern)):
        addr = os.path.basename(os.path.dirname(f))
        try:
            with open(f) as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as e:
            print(f"WARNING: failed to parse {f}: {e}", file=sys.stderr)
            continue
        relations = data.get("relations", [])
        branches = data.get("branches", [])
        distinct_functions = len({b.get("function", "") for b in branches})
        yield addr, relations, distinct_functions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dcr_dir",
        nargs="?",
        default=DEFAULT_DIR,
        help=f"Directory containing <addr>/source_dcr.json subfolders "
             f"(default: {DEFAULT_DIR})",
    )
    parser.add_argument(
        "--min-types",
        type=int,
        default=4,
        help="Report contracts with at least this many distinct relation "
             "types (default: 4, i.e. all-four-relation-types candidates)",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Optional path to dump the full stats as JSON",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.dcr_dir):
        print(f"ERROR: directory not found: {args.dcr_dir}", file=sys.stderr)
        sys.exit(1)

    presence = Counter()
    volume = Counter()
    combo = Counter()
    n_contracts = 0
    n_no_relations = 0
    per_contract_relation_count = []
    all_four_candidates = []  # (addr, types, n_functions)

    for addr, relations, n_functions in load_relations(args.dcr_dir):
        n_contracts += 1
        per_contract_relation_count.append(len(relations))
        if not relations:
            n_no_relations += 1
            continue
        types_here = Counter(r.get("type", "") for r in relations)
        for t, c in types_here.items():
            presence[t] += 1
            volume[t] += c
        combo_str = ",".join(sorted(types_here.keys()))
        combo[combo_str] += 1
        if len(types_here) >= args.min_types:
            all_four_candidates.append((addr, combo_str, n_functions))

    if n_contracts == 0:
        print(f"ERROR: no source_dcr.json files found under {args.dcr_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Directory scanned: {args.dcr_dir}")
    print(f"Contracts scanned: {n_contracts}")
    print(f"Contracts with zero relations: {n_no_relations}")
    print()

    print("Presence (contracts containing >=1 instance of type):")
    for t, c in presence.most_common():
        print(f"  {t:12s} {c:4d}/{n_contracts}  ({100 * c / n_contracts:.1f}%)")
    print()

    print("Volume (total relation instances across all contracts):")
    total_vol = sum(volume.values())
    for t, c in volume.most_common():
        pct = (100 * c / total_vol) if total_vol else 0.0
        print(f"  {t:12s} {c:5d}  ({pct:.1f}% of all relation instances)")
    print()

    print("Type-combination distribution:")
    for combo_str, c in combo.most_common():
        print(f"  {c:4d}  {combo_str}")
    print()

    print(f"Relations per contract: "
          f"mean={st.mean(per_contract_relation_count):.2f}, "
          f"median={st.median(per_contract_relation_count):.1f}, "
          f"max={max(per_contract_relation_count)}")
    print()

    print(f"Contracts with >= {args.min_types} distinct relation types "
          f"({len(all_four_candidates)}/{n_contracts}):")
    for addr, combo_str, n_functions in all_four_candidates:
        print(f"  {addr} : {combo_str}  (distinct functions: {n_functions})")

    if args.json_out:
        out = {
            "dcr_dir": args.dcr_dir,
            "n_contracts": n_contracts,
            "n_no_relations": n_no_relations,
            "presence": dict(presence),
            "volume": dict(volume),
            "combo_distribution": dict(combo),
            "relations_per_contract": {
                "mean": st.mean(per_contract_relation_count),
                "median": st.median(per_contract_relation_count),
                "max": max(per_contract_relation_count),
            },
            "min_types_candidates": [
                {"address": a, "types": t, "distinct_functions": n}
                for a, t, n in all_four_candidates
            ],
        }
        with open(args.json_out, "w") as fh:
            json.dump(out, fh, indent=2)
        print(f"\nFull stats written to {args.json_out}")


if __name__ == "__main__":
    main()