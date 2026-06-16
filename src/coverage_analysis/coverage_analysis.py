#!/usr/bin/env python3
"""
coverage_analysis.py
====================
Compare functional coverage between:
  - Historical transaction replay  (../transaction_replay/replay_results.json)
  - Synthetic transaction replay   (../dcr_replay/synth_replay_results.json)
  - DCR generation coverage        (../dcr_generation/generated_dcr_1to20/<addr>/coverage.json)

Usage:
  python coverage_analysis.py
  python coverage_analysis.py --no-details
  python coverage_analysis.py --output my_report.json
"""

import argparse
import json
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_SRC        = _SCRIPT_DIR.parent

DEFAULT_HIST_FILE = _SRC / "transaction_replay" / "replay_results.json"
DEFAULT_SYNTH_FILE = _SRC / "dcr_replay"        / "synth_replay_results.json"
DEFAULT_DCR_DIR   = _SRC / "dcr_generation"     / "generated_dcr_1to20"
DEFAULT_OUTPUT    = _SCRIPT_DIR / "coverage_analysis.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt(val, spec=":.1%") -> str:
    """Format a value safely, returning 'n/a' if None."""
    if val is None:
        return "n/a"
    return format(val, spec.lstrip(":"))


def avg(vals: list) -> float | None:
    return round(sum(vals) / len(vals), 4) if vals else None


# ---------------------------------------------------------------------------
# Per-contract analysis
# ---------------------------------------------------------------------------

def analyse_contract(
    addr: str,
    hist_by_addr: dict,
    synth_by_addr: dict,
    dcr_dir: Path,
) -> dict | None:

    hist  = hist_by_addr.get(addr)
    synth = synth_by_addr.get(addr)

    # skip .cache and other non-address entries
    if not addr.startswith("0x"):
        return None
    # skip if no data at all
    if hist is None and synth is None:
        return None

    r = {"address": addr}

    # ── Historical ────────────────────────────────────────────────────────
    if hist and hist.get("state") == "DONE":
        r["contract_name"]  = hist.get("contract_name", "?")
        r["hist_pass_rate"] = hist.get("pass_rate")
        r["hist_k"]         = hist.get("k", 0)
        r["hist_fns"]       = hist.get("fn_pass_rates", {})
    else:
        r["contract_name"]  = (synth or {}).get("contract_name", addr[:16])
        r["hist_pass_rate"] = None
        r["hist_k"]         = 0
        r["hist_fns"]       = {}
    r["hist_fn_count"] = len(r["hist_fns"])

    # ── Synthetic ─────────────────────────────────────────────────────────
    if synth and synth.get("state") == "DONE":
        r.setdefault("contract_name", synth.get("contract_name", "?"))
        r["synth_pass_rate"]  = synth.get("pass_rate")
        r["synth_k"]          = synth.get("k", 0)
        r["synth_fns"]        = synth.get("fn_pass_rates", {})
        r["n_events_covered"] = synth.get("n_events_covered", 0)
    else:
        r["synth_pass_rate"]  = None
        r["synth_k"]          = 0
        r["synth_fns"]        = {}
        r["n_events_covered"] = 0
    r["synth_fn_count"] = len(r["synth_fns"])

    # ── DCR generation coverage ───────────────────────────────────────────
    dcr_path = dcr_dir / addr / "coverage.json"
    if dcr_path.exists():
        cov = json.loads(dcr_path.read_text())
        fc  = cov.get("function_coverage", {})
        r["dcr_fn_coverage_ratio"] = fc.get("coverage_ratio")
        r["dcr_successful_fns"]    = fc.get("successful_functions", [])
        r["dcr_total_abi_fns"]     = fc.get("total_abi", 0)
        r["dcr_event_coverage"]    = cov.get("dcr_event_coverage", {}).get("coverage_ratio")
    else:
        r["dcr_fn_coverage_ratio"] = None
        r["dcr_successful_fns"]    = []
        r["dcr_total_abi_fns"]     = 0
        r["dcr_event_coverage"]    = None

    # ── Set overlap ───────────────────────────────────────────────────────
    hist_set  = set(r["hist_fns"].keys())
    synth_set = set(r["synth_fns"].keys())
    dcr_set   = set(r["dcr_successful_fns"])

    r["only_historical"] = sorted(hist_set  - synth_set - dcr_set)
    r["only_synthetic"]  = sorted(synth_set - hist_set  - dcr_set)
    r["only_dcr"]        = sorted(dcr_set   - hist_set  - synth_set)
    r["hist_not_synth"]  = sorted(hist_set  - synth_set)
    r["synth_not_hist"]  = sorted(synth_set - hist_set)
    r["union_fn_count"]  = len(hist_set | synth_set | dcr_set)

    # Per-function comparison for functions in both
    fn_comparison = {}
    for fn in hist_set & synth_set:
        fn_comparison[fn] = {
            "hist_rate":  r["hist_fns"][fn],
            "synth_rate": r["synth_fns"][fn],
            "delta":      round(r["synth_fns"][fn] - r["hist_fns"][fn], 4),
        }
    r["fn_comparison"] = fn_comparison

    return r


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def aggregate(records: list[dict]) -> dict:
    has_hist  = [r for r in records if r.get("hist_pass_rate")  is not None]
    has_synth = [r for r in records if r.get("synth_pass_rate") is not None]
    has_both  = [r for r in records if r.get("hist_pass_rate")  is not None
                                    and r.get("synth_pass_rate") is not None]
    dcr_ratios = [r["dcr_fn_coverage_ratio"] for r in records
                  if r.get("dcr_fn_coverage_ratio") is not None]

    return {
        "n_contracts":             len(records),
        "n_with_historical":       len(has_hist),
        "n_with_synthetic":        len(has_synth),
        "n_with_both":             len(has_both),
        "hist_avg_pass_rate":      avg([r["hist_pass_rate"]  for r in has_hist]),
        "synth_avg_pass_rate":     avg([r["synth_pass_rate"] for r in has_synth]),
        "hist_avg_fn_count":       avg([r["hist_fn_count"]   for r in has_hist]),
        "synth_avg_fn_count":      avg([r["synth_fn_count"]  for r in has_synth]),
        "dcr_avg_fn_coverage":     avg(dcr_ratios),
        "avg_gap_hist_not_synth":  avg([len(r["hist_not_synth"]) for r in has_both]),
        "avg_gain_synth_not_hist": avg([len(r["synth_not_hist"]) for r in has_both]),
        "synth_broader_count":     sum(1 for r in has_both if r["synth_fn_count"] > r["hist_fn_count"]),
        "hist_broader_count":      sum(1 for r in has_both if r["hist_fn_count"]  > r["synth_fn_count"]),
        "equal_coverage_count":    sum(1 for r in has_both if r["hist_fn_count"]  == r["synth_fn_count"]),
    }


# ---------------------------------------------------------------------------
# Print
# ---------------------------------------------------------------------------

def print_report(records: list[dict], agg: dict, no_details: bool = False) -> None:
    SEP = "═" * 80
    print(f"\n{SEP}")
    print("  FUNCTIONAL COVERAGE COMPARISON")
    print("  historical replay  vs  synthetic (DCR) replay")
    print(SEP)
    print(f"  Contracts with historical data : {agg['n_with_historical']}")
    print(f"  Contracts with synthetic data  : {agg['n_with_synthetic']}")
    print(f"  Contracts with both            : {agg['n_with_both']}")
    print()
    print(f"  Historical avg pass rate       : {fmt(agg['hist_avg_pass_rate'])}")
    print(f"  Synthetic  avg pass rate       : {fmt(agg['synth_avg_pass_rate'])}")
    print()
    print(f"  Historical avg functions hit   : {fmt(agg['hist_avg_fn_count'], ':.1f')}")
    print(f"  Synthetic  avg functions hit   : {fmt(agg['synth_avg_fn_count'], ':.1f')}")
    print(f"  DCR avg fn coverage ratio      : {fmt(agg['dcr_avg_fn_coverage'])}")
    print()
    print(f"  Synth broader fn coverage      : {agg['synth_broader_count']} contracts")
    print(f"  Hist  broader fn coverage      : {agg['hist_broader_count']} contracts")
    print(f"  Equal fn coverage              : {agg['equal_coverage_count']} contracts")
    print(f"  Avg fns only in historical     : {fmt(agg['avg_gap_hist_not_synth'], ':.2f')}")
    print(f"  Avg fns only in synthetic      : {fmt(agg['avg_gain_synth_not_hist'], ':.2f')}")
    print(SEP)

    if no_details:
        return

    print(f"\n{'contract':<22} {'hist_rate':>9} {'synth_rate':>10} "
          f"{'hist_fns':>8} {'synth_fns':>9} {'dcr_cov':>7}")
    print("-" * 70)

    for r in sorted(records, key=lambda x: x.get("contract_name", "")):
        name = r.get("contract_name", r["address"][:16])[:20]
        print(f"{name:<22} {fmt(r['hist_pass_rate']):>9} {fmt(r['synth_pass_rate']):>10} "
              f"{r['hist_fn_count']:>8} {r['synth_fn_count']:>9} "
              f"{fmt(r['dcr_fn_coverage_ratio']):>7}")
        if r["hist_not_synth"]:
            print(f"  {'↑ hist only':<18}: {r['hist_not_synth']}")
        if r["synth_not_hist"]:
            print(f"  {'↑ synth only':<18}: {r['synth_not_hist']}")
        for fn, cmp in r.get("fn_comparison", {}).items():
            if abs(cmp["delta"]) >= 0.1:
                arrow = "▲" if cmp["delta"] > 0 else "▼"
                print(f"  {fn:<20} hist={cmp['hist_rate']:.0%}  "
                      f"synth={cmp['synth_rate']:.0%}  {arrow}{abs(cmp['delta']):.0%}")

    print(SEP)


# ---------------------------------------------------------------------------
# CLI + main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Compare historical vs synthetic functional coverage.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--hist",    type=Path, default=DEFAULT_HIST_FILE,
                   help="replay_results.json from replay_transactions.py")
    p.add_argument("--synth",   type=Path, default=DEFAULT_SYNTH_FILE,
                   help="synth_replay_results.json from dcr_replay")
    p.add_argument("--dcr",     type=Path, default=DEFAULT_DCR_DIR,
                   help="generated_dcr_1to20 directory")
    p.add_argument("--output",  type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--no-details", action="store_true",
                   help="Print summary only.")
    return p.parse_args()


def main():
    args = parse_args()

    print(f"hist  : {args.hist}")
    print(f"synth : {args.synth}")
    print(f"dcr   : {args.dcr}")
    for label, path in [("hist", args.hist), ("synth", args.synth), ("dcr", args.dcr)]:
        if not path.exists():
            print(f"  [warn] {label} path not found: {path}")

    # Load both summary files
    hist_by_addr: dict[str, dict] = {}
    if args.hist.exists():
        data = json.loads(args.hist.read_text())
        for r in data.get("results", []):
            addr = r.get("address", "")
            if addr.startswith("0x"):
                hist_by_addr[addr] = r

    synth_by_addr: dict[str, dict] = {}
    if args.synth.exists():
        data = json.loads(args.synth.read_text())
        for r in data.get("results", []):
            addr = r.get("address", "")
            if addr.startswith("0x"):
                synth_by_addr[addr] = r

    # Union of all known addresses
    all_addrs = set(hist_by_addr.keys()) | set(synth_by_addr.keys())
    if args.dcr.exists():
        all_addrs |= {d.name for d in args.dcr.iterdir()
                      if d.is_dir() and d.name.startswith("0x")}

    print(f"\nAddresses: hist={len(hist_by_addr)}  synth={len(synth_by_addr)}  "
          f"union={len(all_addrs)}\n")

    records = []
    for addr in sorted(all_addrs):
        r = analyse_contract(addr, hist_by_addr, synth_by_addr, args.dcr)
        if r:
            records.append(r)

    agg = aggregate(records)
    print_report(records, agg, no_details=args.no_details)

    args.output.write_text(json.dumps({"aggregate": agg, "contracts": records}, indent=2))
    print(f"\nFull report saved to {args.output}")


if __name__ == "__main__":
    main()