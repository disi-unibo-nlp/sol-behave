#!/usr/bin/env python3
"""
find_dcr_example.py

Finds candidate contracts for the DCR-graph example figure: contracts whose
source_dcr.json contains all four relation types (condition, response,
exclusion, inclusion) AND at least one event that participates in zero
relations (a disconnected node), so the figure can show every relation type
plus the "no constraint" case in one picture.

Candidates are ranked by total relation count (ascending) so the smallest,
most figure-friendly examples come first. Among ties, fewer events is
preferred (smaller graph to draw).

Usage:
    python3 find_dcr_example.py [DIR] [--min-types 4] [--require-disconnected]
                                 [--top 20] [--json-out PATH]

    DIR defaults to ./generated_dcr_1to20_Qwen3_6-27B-UD-Q4_K_XL_gguf,
    matching dcr_relation_stats.py's default.

Run from /workdir/src/dcr_generation/, e.g.:
    python3 find_dcr_example.py generated_dcr_1to20_gemma-4-31B-it-qat-UD-Q4_K_XL_gguf
"""

import argparse
import glob
import json
import os
import sys

RELATION_TYPES = {"condition", "response", "exclusion", "inclusion"}
DEFAULT_DIR = "generated_dcr_1to20_Qwen3_6-27B-UD-Q4_K_XL_gguf"


def load_dcr(dcr_dir):
    """Yield (address, events, relations) for each contract dir."""
    pattern = os.path.join(dcr_dir, "*", "source_dcr.json")
    for f in sorted(glob.glob(pattern)):
        addr = os.path.basename(os.path.dirname(f))
        try:
            with open(f) as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as e:
            print(f"WARNING: failed to parse {f}: {e}", file=sys.stderr)
            continue
        yield addr, data.get("events", []), data.get("relations", [])


def disconnected_events(events, relations):
    """Return event ids that appear in zero relations (as from or to)."""
    touched = set()
    for r in relations:
        touched.add(r.get("from"))
        touched.add(r.get("to"))
    all_ids = {e["id"] for e in events}
    return sorted(all_ids - touched)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dcr_dir", nargs="?", default=DEFAULT_DIR,
                        help=f"Directory containing <addr>/source_dcr.json "
                             f"subfolders (default: {DEFAULT_DIR})")
    parser.add_argument("--min-types", type=int, default=4,
                        help="Minimum distinct relation types required "
                             "(default: 4, i.e. all of condition/response/"
                             "exclusion/inclusion)")
    parser.add_argument("--require-disconnected", action="store_true",
                        default=True,
                        help="Only keep contracts with >=1 disconnected "
                             "event (default: on)")
    parser.add_argument("--no-require-disconnected", dest="require_disconnected",
                        action="store_false",
                        help="Disable the disconnected-node requirement")
    parser.add_argument("--top", type=int, default=20,
                        help="Number of top candidates to print (default: 20)")
    parser.add_argument("--json-out", default=None,
                        help="Optional path to dump all matching candidates as JSON")
    args = parser.parse_args()

    if not os.path.isdir(args.dcr_dir):
        print(f"ERROR: directory not found: {args.dcr_dir}", file=sys.stderr)
        sys.exit(1)

    candidates = []
    n_scanned = 0

    for addr, events, relations in load_dcr(args.dcr_dir):
        n_scanned += 1
        if not relations or not events:
            continue

        types_here = {r.get("type", "") for r in relations}
        if len(types_here & RELATION_TYPES) < args.min_types:
            continue

        disc = disconnected_events(events, relations)
        if args.require_disconnected and not disc:
            continue

        candidates.append({
            "address": addr,
            "n_events": len(events),
            "n_relations": len(relations),
            "types": sorted(types_here),
            "disconnected_events": disc,
            "n_disconnected": len(disc),
        })

    if n_scanned == 0:
        print(f"ERROR: no source_dcr.json files found under {args.dcr_dir}", file=sys.stderr)
        sys.exit(1)

    # Rank: fewest relations first (cleanest figure), then fewest events,
    # then most disconnected nodes (more clearly shows the "isolated" case).
    candidates.sort(key=lambda c: (c["n_relations"], c["n_events"], -c["n_disconnected"]))

    print(f"Directory scanned: {args.dcr_dir}")
    print(f"Contracts scanned: {n_scanned}")
    print(f"Matching all {args.min_types} relation types"
          f"{' + disconnected node' if args.require_disconnected else ''}: "
          f"{len(candidates)}")
    print()

    if not candidates:
        print("No matching contracts. Try --min-types 3 or "
              "--no-require-disconnected to relax the search.")
        return

    print(f"Top {min(args.top, len(candidates))} candidates "
          f"(sorted by fewest relations, then fewest events):")
    print()
    header = f"{'address':44s} {'events':>6s} {'relations':>9s} {'disc.':>5s}  types"
    print(header)
    print("-" * len(header))
    for c in candidates[:args.top]:
        print(f"{c['address']:44s} {c['n_events']:6d} {c['n_relations']:9d} "
              f"{c['n_disconnected']:5d}  {','.join(c['types'])}")
        print(f"{'':44s} disconnected: {c['disconnected_events']}")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(candidates, fh, indent=2)
        print(f"\nAll {len(candidates)} candidates written to {args.json_out}")


if __name__ == "__main__":
    main()