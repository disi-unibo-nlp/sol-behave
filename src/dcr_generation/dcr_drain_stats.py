#!/usr/bin/env python3
"""
dcr_drain_stats.py

Reports statistics on privilege-drop ("drain") events across a directory of
generated source_dcr.json files: per-contract event count, how many events
per contract have a non-zero drain_priority (i.e. are deferred to the drain
phase), and the fraction of contracts that defer nothing at all.

Mirrors the structure of dcr_relation_stats.py but operates on the "events"
list (field: drain_priority) rather than "relations".

Usage:
    python3 dcr_drain_stats.py [DIR] [--json-out PATH]

    DIR defaults to ./generated_dcr_1to20_Qwen3_6-27B-UD-Q4_K_XL_gguf
    (relative to the current working directory), matching the layout under
    /workdir/src/dcr_generation/.

Run from /workdir/src/dcr_generation/, e.g.:
    python3 dcr_drain_stats.py generated_dcr_1to20_gemma-4-31B-it-qat-UD-Q4_K_XL_gguf
    python3 dcr_drain_stats.py generated_dcr_1to20_gemma-4-31B-it-qat-UD-Q4_K_XL_gguf --json-out drain_stats_gemma.json
"""

import argparse
import glob
import json
import os
import statistics as st
import sys
from collections import Counter


DEFAULT_DIR = "generated_dcr_1to20_Qwen3_6-27B-UD-Q4_K_XL_gguf"


def load_events(dcr_dir):
    """Yield (address, events_list) for each contract dir."""
    pattern = os.path.join(dcr_dir, "*", "source_dcr.json")
    for f in sorted(glob.glob(pattern)):
        addr = os.path.basename(os.path.dirname(f))
        try:
            with open(f) as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as e:
            print(f"WARNING: failed to parse {f}: {e}", file=sys.stderr)
            continue
        yield addr, data.get("events", [])


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
        "--json-out",
        default=None,
        help="Optional path to dump the full stats as JSON",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.dcr_dir):
        print(f"ERROR: directory not found: {args.dcr_dir}", file=sys.stderr)
        sys.exit(1)

    n_contracts = 0
    events_per_contract = []          # total event count per contract
    deferred_per_contract = []        # events with drain_priority != 0, per contract
    n_contracts_zero_deferred = 0
    priority_counter = Counter()      # distribution of nonzero drain_priority values
    deferred_examples = []            # (addr, event_id, drain_priority)

    for addr, events in load_events(args.dcr_dir):
        n_contracts += 1
        events_per_contract.append(len(events))

        deferred = [e for e in events if e.get("drain_priority", 0) != 0]
        deferred_per_contract.append(len(deferred))

        if not deferred:
            n_contracts_zero_deferred += 1
        else:
            for e in deferred:
                priority_counter[e.get("drain_priority", 0)] += 1
                deferred_examples.append(
                    (addr, e.get("id", "?"), e.get("drain_priority", 0))
                )

    if n_contracts == 0:
        print(f"ERROR: no source_dcr.json files found under {args.dcr_dir}", file=sys.stderr)
        sys.exit(1)

    total_events = sum(events_per_contract)
    total_deferred = sum(deferred_per_contract)
    mean_events = st.mean(events_per_contract)
    mean_deferred = st.mean(deferred_per_contract)
    pct_of_graph = (100 * mean_deferred / mean_events) if mean_events else 0.0
    pct_contracts_zero = 100 * n_contracts_zero_deferred / n_contracts

    print(f"Directory scanned: {args.dcr_dir}")
    print(f"Contracts scanned: {n_contracts}")
    print()

    print(f"Mean events per contract:   {mean_events:.2f}")
    print(f"Mean deferred per contract: {mean_deferred:.2f}  "
          f"({pct_of_graph:.1f}% of a typical graph)")
    print(f"Contracts deferring nothing: {n_contracts_zero_deferred}/{n_contracts} "
          f"({pct_contracts_zero:.1f}%)")
    print()

    print("Drain-priority value distribution (nonzero only):")
    for prio, c in priority_counter.most_common():
        print(f"  priority {prio:<4d} {c:4d} instances")
    print()

    print(f"Total events: {total_events}   Total deferred: {total_deferred} "
          f"({100 * total_deferred / total_events:.1f}% pooled)")

    if args.json_out:
        out = {
            "dcr_dir": args.dcr_dir,
            "n_contracts": n_contracts,
            "mean_events_per_contract": mean_events,
            "mean_deferred_per_contract": mean_deferred,
            "pct_of_typical_graph": pct_of_graph,
            "n_contracts_zero_deferred": n_contracts_zero_deferred,
            "pct_contracts_zero_deferred": pct_contracts_zero,
            "total_events": total_events,
            "total_deferred": total_deferred,
            "pct_deferred_pooled": 100 * total_deferred / total_events if total_events else 0.0,
            "drain_priority_distribution": dict(priority_counter),
            "deferred_examples": [
                {"address": a, "event_id": e, "drain_priority": p}
                for a, e, p in deferred_examples
            ],
        }
        with open(args.json_out, "w") as fh:
            json.dump(out, fh, indent=2)
        print(f"\nFull stats written to {args.json_out}")


if __name__ == "__main__":
    main()