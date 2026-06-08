"""
coverage_analysis.py
====================

Measures how much of a contract's callable surface is actually exercised
by the first K historical transactions used in replay evaluation.

For each contract in the dataset the script answers two questions:

  1. How many functions could the model be asked to generate?
     → All non-view / non-pure ABI functions with a body in the source
       ("generatable functions").  These are the functions replaced with
       // TODO during generation.

  2. How many of those functions are actually called in the first K txs?
     → Each transaction carries a 4-byte selector in its `input` field.
       We match selectors to ABI entries and count distinct covered functions.

Coverage = called_functions / generatable_functions

This is important for interpreting replay pass rates: a contract with
90% replay pass rate but only 2/15 functions called is far less
informative than one with 90% and 14/15 functions called.

Outputs
-------
  coverage_results.json     per-contract coverage table + aggregates
  (stdout)                  human-readable summary

Algorithm
---------
  Selector computation:
    For each ABI function entry, build the canonical signature
    "name(type1,type2,...)" (with tuple expansion for structs),
    then take keccak256(signature)[:4].

  Generatable function filter:
    ABI functions whose stateMutability is NOT "view" or "pure", AND
    whose name appears in the source as a function with a non-empty body
    (using _find_functions from source_utils.py).

  Called-function detection:
    For each transaction in callable_txs[:K], read input[:10] (the "0x"
    prefix + 8 hex chars = 4 bytes).  Match against the selector map.
    Only transactions directed to the contract with non-empty input are
    considered (same filter as replay_transactions.py).

Usage
-----
  python coverage_analysis.py [options]

  # Default: dataset contracts_dataset_1to20, K=1000
  python coverage_analysis.py

  # Custom paths:
  python coverage_analysis.py \\
      --dataset ../dataset/contracts_dataset_1to20 \\
      --k 1000 \\
      --output  coverage_results.json \\
      --verbose

  # Only specific contracts:
  python coverage_analysis.py --addresses 0xabc... 0xdef...

Dependencies
------------
  pycryptodome   (pip install pycryptodome)
  source_utils   (../utils/source_utils.py)
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR / ".." / "utils"))

from source_utils import _find_functions, _main_source_text, _load_json


# ---------------------------------------------------------------------------
# Keccak-256 selector computation
# ---------------------------------------------------------------------------

def _keccak256(data: bytes) -> bytes:
    from Crypto.Hash import keccak
    k = keccak.new(digest_bits=256)
    k.update(data)
    return bytes.fromhex(k.hexdigest())


def _canonical_type(t: str, components: Optional[list] = None) -> str:
    """
    Expand a Solidity ABI type to its canonical form.
    Handles tuples/structs: (type1,type2,...) or (...)[]  etc.
    """
    if t == "tuple" or t.startswith("tuple[") or t.startswith("tuple("):
        if not components:
            return "(" + ")"
        inner = ",".join(_canonical_type(c["type"], c.get("components"))
                         for c in components)
        suffix = t[len("tuple"):]   # e.g. "[]" or "[3]" or ""
        return f"({inner}){suffix}"
    return t


def _fn_selector(name: str, inputs: list) -> str:
    """
    Compute the 4-byte selector (hex, no 0x prefix) for an ABI function entry.
    """
    param_types = ",".join(
        _canonical_type(inp["type"], inp.get("components"))
        for inp in inputs
    )
    sig = f"{name}({param_types})"
    return _keccak256(sig.encode())[:4].hex()


def build_selector_map(abi: list) -> dict[str, dict]:
    """
    Build a map  {4-byte-hex-selector: abi_entry}  for all non-fallback,
    non-receive ABI functions.
    """
    selector_map: dict[str, dict] = {}
    for entry in abi:
        if entry.get("type") != "function":
            continue
        name   = entry.get("name", "")
        inputs = entry.get("inputs", [])
        try:
            sel = _fn_selector(name, inputs)
            selector_map[sel] = entry
        except Exception:
            pass
    return selector_map


# ---------------------------------------------------------------------------
# Generatable function identification
# ---------------------------------------------------------------------------

_SKIP_MUTABILITY = {"view", "pure"}


def _generatable_functions(abi: list, source_text: str) -> list[dict]:
    """
    Return the set of functions that the model would be asked to generate,
    using the SAME filter logic as analyze_dataset.py so the denominator
    here is identical to the one used to select contracts into the dataset.

    Primary source of truth: the source text.
      - A function is generatable if _find_functions finds it with a
        non-empty body (same call as analyze_dataset).
      - Constructor and view/pure functions are excluded (analyze_dataset
        excludes them via its local fallback).

    ABI is used ONLY to look up stateMutability and compute the 4-byte
    selector.  If a function is present in source but absent from the ABI
    (e.g. the ABI is sparse or unverified), it is still counted as
    generatable but will have selector=None and cannot be matched to
    transactions.

    Returns a list of dicts:
        name        function name
        inputs      ABI input list ([] if not in ABI)
        selector    4-byte hex selector (None if not in ABI)
        in_abi      True if the function was found in the ABI
    """
    # Build ABI lookup: name -> entry  (last entry wins for overloads)
    abi_by_name: dict[str, dict] = {}
    for entry in abi:
        if entry.get("type") != "function":
            continue
        abi_by_name[entry.get("name", "")] = entry

    # Find all functions with non-empty bodies in the source
    source_fns = _find_functions(source_text)
    result = []
    seen_names: set[str] = set()

    for fn in source_fns:
        m = re.search(r'\bfunction\s+(\w+)\s*\(', fn["fn_sig"])
        if not m:
            continue
        name = m.group(1)

        # Exclude constructors (analyze_dataset local fallback does this)
        if name.lower() == "constructor":
            continue

        # Exclude view/pure via the signature text, mirroring analyze_dataset's
        # modifier_region check.  source_utils._find_functions does NOT filter
        # these out, so we must do it here to stay consistent with the dataset gate.
        if re.search(r'\b(view|pure)\b', fn["fn_sig"]):
            continue

        # Deduplicate by name (overloaded functions share one TODO slot)
        if name in seen_names:
            continue
        seen_names.add(name)

        # Cross-reference with ABI
        abi_entry = abi_by_name.get(name)
        if abi_entry is not None:
            mutability = abi_entry.get("stateMutability", "nonpayable")
            if mutability in _SKIP_MUTABILITY:
                # ABI says view/pure even though source has a body:
                # trust the ABI for mutability, skip it
                continue
            inputs = abi_entry.get("inputs", [])
            try:
                selector = _fn_selector(name, inputs)
            except Exception:
                selector = None
            result.append({
                "name":     name,
                "inputs":   inputs,
                "selector": selector,
                "in_abi":   True,
            })
        else:
            # Function is in source but not in ABI — still generatable,
            # but we cannot match it to transaction selectors
            result.append({
                "name":     name,
                "inputs":   [],
                "selector": None,
                "in_abi":   False,
            })

    return result


# ---------------------------------------------------------------------------
# Transaction filtering (mirrors replay_transactions.filter_callable_txs)
# ---------------------------------------------------------------------------

def _filter_callable_txs(
    txs: list, address: str, deploy_block: Optional[int], k: int
) -> list:
    addr_lower = address.lower()
    result = []
    for tx in txs:
        if (tx.get("to") or "").lower() != addr_lower:
            continue
        inp = tx.get("input", "0x")
        if inp in ("0x", "") or len(inp) < 10:   # need at least 4-byte selector
            continue
        if tx.get("isError", "0") == "1":
            continue
        if deploy_block is not None:
            try:
                if int(tx.get("blockNumber", 0)) < deploy_block:
                    continue
            except (ValueError, TypeError):
                pass
        result.append(tx)
    return result[:k]


# ---------------------------------------------------------------------------
# Per-contract coverage computation
# ---------------------------------------------------------------------------

def analyse_contract(
    contract_dir: Path,
    k:            int,
    verbose:      bool = False,
) -> Optional[dict]:
    """
    Compute function coverage for a single contract.
    Returns a result dict, or None if required files are missing.
    """
    address = contract_dir.name

    # ── Load metadata ─────────────────────────────────────────────────────
    meta_path = contract_dir / "metadata.json"
    if not meta_path.exists():
        return None
    meta = _load_json(meta_path)
    contract_name = meta.get("contract_name", address[:12] + "…")

    # ── Load ABI ──────────────────────────────────────────────────────────
    abi_path = contract_dir / "abi.json"
    if not abi_path.exists():
        return None
    try:
        abi = json.loads(abi_path.read_text())
    except Exception:
        return None

    # ── Load transactions ─────────────────────────────────────────────────
    txs_path = contract_dir / "transactions.json"
    if not txs_path.exists():
        return None
    try:
        txs = json.loads(txs_path.read_text())
    except Exception:
        return None

    # ── Load source text (main file only) ─────────────────────────────────
    source_text, _ = _main_source_text(contract_dir)
    if not source_text:
        return None

    # ── Build selector map for all ABI functions ──────────────────────────
    selector_map = build_selector_map(abi)

    # ── Identify generatable functions ────────────────────────────────────
    gen_fns = _generatable_functions(abi, source_text)

    # n_generatable = ALL functions the model would be asked to implement,
    # including those absent from the ABI (they just can't be selector-matched)
    n_generatable  = len(gen_fns)
    gen_fn_names   = [fn["name"] for fn in gen_fns]

    # Selector set: only fns that have a known selector (in_abi=True)
    gen_selectors: dict[str, str] = {}   # selector -> fn name
    for fn in gen_fns:
        if fn["selector"] is not None:
            gen_selectors[fn["selector"]] = fn["name"]

    # Fns with no ABI entry — cannot be matched to txs
    no_abi_names = [fn["name"] for fn in gen_fns if not fn["in_abi"]]

    # ── Filter transactions ────────────────────────────────────────────────
    deploy_block  = meta.get("deploy_block")
    callable_txs  = _filter_callable_txs(txs, address, deploy_block, k)
    n_txs         = len(callable_txs)

    # ── Count called selectors ────────────────────────────────────────────
    called_selectors:     set[str] = set()   # all selectors seen in txs
    called_gen_selectors: set[str] = set()   # selectors that are generatable
    selector_call_counts: dict[str, int] = defaultdict(int)

    for tx in callable_txs:
        inp = tx.get("input", "0x")
        if len(inp) < 10:
            continue
        sel = inp[2:10].lower()   # 4 bytes, no "0x"
        called_selectors.add(sel)
        selector_call_counts[sel] += 1
        if sel in gen_selectors:
            called_gen_selectors.add(sel)

    n_called_gen = len(called_gen_selectors)
    # Coverage denominator = fns with a known selector only (can't measure
    # coverage of fns absent from the ABI)
    n_matchable  = len(gen_selectors)
    coverage     = round(n_called_gen / n_matchable, 4) if n_matchable else None

    # ── Uncovered generatable functions ───────────────────────────────────
    uncovered_selectors = set(gen_selectors.keys()) - called_gen_selectors
    uncovered_names = [gen_selectors[s] for s in uncovered_selectors]
    # Functions absent from ABI are always "uncovered" (can't be matched)
    uncovered_names += no_abi_names

    # ── Unknown selectors (called but not in ABI) ─────────────────────────
    unknown_selectors = called_selectors - set(selector_map.keys())

    if verbose:
        abi_note = f"  ({len(no_abi_names)} not in ABI)" if no_abi_names else ""
        if coverage is not None:
            print(f"  {contract_name}: generatable={n_generatable}"
                  f"  matchable={n_matchable}  called={n_called_gen}"
                  f"  coverage={coverage:.0%}{abi_note}")
        else:
            print(f"  {contract_name}: no matchable functions{abi_note}")
        if uncovered_names:
            print(f"    uncovered: {', '.join(uncovered_names)}")

    return {
        "address":            address,
        "contract_name":      contract_name,
        "n_txs_analysed":     n_txs,
        # Functions the model would implement (source-based, view/pure excluded)
        "n_generatable":      n_generatable,
        "generatable_names":  gen_fn_names,
        # Subset that can be selector-matched (have an ABI entry)
        "n_matchable":        n_matchable,
        # Functions in source but absent from ABI — counted but unmatchable
        "n_no_abi":           len(no_abi_names),
        "no_abi_names":       no_abi_names,
        # How many matchable functions are called at least once in first K txs
        "n_called":           n_called_gen,
        "called_names":       [gen_selectors[s] for s in called_gen_selectors],
        # Functions never called (includes no-ABI functions)
        "uncovered_names":    uncovered_names,
        # Coverage = called / matchable  (None if no matchable functions)
        "coverage":           coverage,
        # Extra diagnostics
        "n_distinct_selectors_seen": len(called_selectors),
        "n_unknown_selectors":       len(unknown_selectors),
        "selector_call_counts": {
            (selector_map[s]["name"] if s in selector_map else f"0x{s}"): c
            for s, c in selector_call_counts.items()
        },
    }


# ---------------------------------------------------------------------------
# Aggregate statistics
# ---------------------------------------------------------------------------

def compute_aggregates(results: list[dict]) -> dict:
    valid = [r for r in results if r.get("coverage") is not None]
    if not valid:
        return {"error": "no valid contracts"}

    n          = len(valid)
    coverages  = [r["coverage"] for r in valid]
    gen_counts = [r["n_generatable"] for r in valid]
    called_counts = [r["n_called"] for r in valid]
    matchable_counts = [r["n_matchable"] for r in valid]

    # Coverage buckets
    buckets = {"100%": 0, ">=80%": 0, ">=50%": 0, "<50%": 0, "0%": 0}
    for c in coverages:
        if c == 1.0:
            buckets["100%"] += 1
        elif c >= 0.8:
            buckets[">=80%"] += 1
        elif c >= 0.5:
            buckets[">=50%"] += 1
        elif c == 0.0:
            buckets["0%"] += 1
        else:
            buckets["<50%"] += 1

    # Contracts with zero generatable functions (shouldn't happen after the fix,
    # but kept as a sanity check)
    n_no_gen = sum(1 for r in results if r.get("n_generatable", 0) == 0)
    # Contracts where some functions are not in the ABI
    n_partial_abi = sum(1 for r in results if r.get("n_no_abi", 0) > 0)
    total_no_abi  = sum(r.get("n_no_abi", 0) for r in results)

    return {
        "n_contracts":             len(results),
        "n_with_generatable":      n,
        "n_without_generatable":   n_no_gen,
        "n_with_partial_abi":      n_partial_abi,
        "total_no_abi_fns":        total_no_abi,
        "mean_coverage":           round(sum(coverages) / n, 4),
        "median_coverage":         round(sorted(coverages)[n // 2], 4),
        "min_coverage":            round(min(coverages), 4),
        "max_coverage":            round(max(coverages), 4),
        "mean_generatable":        round(sum(gen_counts) / n, 2),
        "mean_matchable":          round(sum(matchable_counts) / n, 2),
        "mean_called":             round(sum(called_counts) / n, 2),
        "total_generatable":       sum(gen_counts),
        "total_matchable":         sum(matchable_counts),
        "total_called":            sum(called_counts),
        "overall_coverage":        round(sum(called_counts) / sum(matchable_counts), 4)
                                   if sum(matchable_counts) else None,
        "coverage_buckets":        buckets,
    }


# ---------------------------------------------------------------------------
# Human-readable output
# ---------------------------------------------------------------------------

def print_summary(results: list[dict], aggregates: dict, k: int) -> None:
    # Sort by coverage ascending so low-coverage contracts stand out at top
    valid = sorted(
        [r for r in results if r.get("coverage") is not None],
        key=lambda r: (r["coverage"], r["n_generatable"]),
    )

    col_name = 26
    col_num  =  6
    col_pct  =  8

    header = (
        f"{'Contract':<{col_name}}  "
        f"{'Genble':>{col_num}}  "
        f"{'Match':>{col_num}}  "
        f"{'Called':>{col_num}}  "
        f"{'Txs':>{col_num}}  "
        f"{'Cover':>{col_pct}}"
    )
    sep = "─" * len(header)

    print()
    print(f"  FUNCTION COVERAGE REPORT  (K={k})")
    print("  " + sep)
    print("  " + header)
    print("  " + sep)

    for r in valid:
        cov = f"{r['coverage']:.0%}" if r["coverage"] is not None else "  n/a"
        print(
            f"  {r['contract_name']:<{col_name}}  "
            f"{r['n_generatable']:>{col_num}}  "
            f"{r['n_matchable']:>{col_num}}  "
            f"{r['n_called']:>{col_num}}  "
            f"{r['n_txs_analysed']:>{col_num}}  "
            f"{cov:>{col_pct}}"
        )

    no_gen = [r for r in results if r.get("n_generatable", 0) == 0]
    if no_gen:
        print(f"  (+ {len(no_gen)} contract(s) with no generatable functions not shown)")

    print("  " + sep)
    print()
    print("  AGGREGATE STATISTICS")
    print("  " + sep)

    if "error" in aggregates:
        print(f"  {aggregates['error']}")
    else:
        agg = aggregates
        print(f"  Contracts analysed        : {agg['n_contracts']}")
        print(f"  With generatable fns      : {agg['n_with_generatable']}"
              #f"  ({agg['n_without_generatable']} have none)")
        )
        if agg.get("n_with_partial_abi", 0) > 0:
            print(f"  With fns missing from ABI : {agg['n_with_partial_abi']}"
                  f"  ({agg['total_no_abi_fns']} fns total — counted but unmatchable)")
        print()
        print(f"  Total generatable fns     : {agg['total_generatable']}")
        print(f"  Total matchable fns       : {agg['total_matchable']}"
              f"  (have ABI entry + selector)")
        print(f"  Total called (≥1 time)    : {agg['total_called']}")
        print(f"  Overall coverage          : "
              f"{agg['overall_coverage']:.1%}" if agg["overall_coverage"] else "  n/a")
        print()
        print(f"  Mean generatable/contract : {agg['mean_generatable']:.1f}")
        print(f"  Mean matchable/contract   : {agg['mean_matchable']:.1f}")
        print(f"  Mean coverage/contract    : {agg['mean_coverage']:.1%}")
        print(f"  Median coverage/contract  : {agg['median_coverage']:.1%}")
        print(f"  Min / Max coverage        : "
              f"{agg['min_coverage']:.1%} / {agg['max_coverage']:.1%}")
        print()
        print(f"  Coverage buckets:")
        b = agg["coverage_buckets"]
        total = agg["n_with_generatable"]
        for label, count in b.items():
            bar = "█" * (count * 30 // max(total, 1))
            print(f"    {label:>5}  {count:>4}  {count/total:>5.1%}  {bar}")

    print("  " + sep)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Analyse function call coverage of historical transactions.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--dataset", type=Path,
        default=_SCRIPT_DIR / ".." / "dataset" / "contracts_dataset_1to20",
        help="Dataset directory containing contract subdirectories.",
    )
    p.add_argument(
        "--k", type=int, default=1000,
        help="Number of transactions to examine per contract.",
    )
    p.add_argument(
        "--addresses", nargs="*", metavar="ADDR",
        help="Analyse only these contract addresses. Default: all.",
    )
    p.add_argument(
        "--from-results", type=Path, dest="from_results", default=None,
        metavar="JSON",
        help="Extract addresses from a replay_results.json or "
             "generated_replay_results.json file and analyse only those "
             "contracts.  For replay_results.json, only DONE contracts are "
             "included.  For generated_replay_results.json, only contracts "
             "with at least one successfully replayed sample are included. "
             "Equivalent to building --addresses by hand from those files.",
    )
    p.add_argument(
        "--from-comparison", type=Path, dest="from_comparison", default=None,
        metavar="JSON",
        help="Extract addresses from a comparison.json file produced by "
             "compare_results.py and analyse only those contracts "
             "(i.e. the ones that appear in both original and generated "
             "replay results — the evaluated set).",
    )
    p.add_argument(
        "--output", type=Path,
        default=_SCRIPT_DIR / "coverage_results.json",
        help="Output path for the JSON results.",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print per-contract details and uncovered function names.",
    )
    return p.parse_args()


def _addresses_from_results(path: Path) -> list[str]:
    """
    Extract contract addresses from a replay_results.json or
    generated_replay_results.json file.

    replay_results.json (from replay_transactions.py):
        top-level "results" list, include entries where state == "DONE".

    generated_replay_results.json (from replay_generated.py):
        top-level "contracts" list, include entries where replayed > 0.
    """
    data = json.loads(path.read_text())
    # replay_results.json uses "results" key
    if "results" in data:
        return [
            r["address"] for r in data["results"]
            if r.get("state") == "DONE" and r.get("address")
        ]
    # generated_replay_results.json uses "contracts" key
    if "contracts" in data:
        return [
            c["address"] for c in data["contracts"]
            if c.get("replayed", 0) > 0 and c.get("address")
        ]
    return []


def _addresses_from_comparison(path: Path) -> list[str]:
    """
    Extract addresses from a comparison.json file (compare_results.py).
    Includes all contracts that have both original and generated pass rates
    (i.e. the fully evaluated set).
    """
    data = json.loads(path.read_text())
    return [
        c["address"] for c in data.get("contracts", [])
        if c.get("original_pass_rate") is not None
        and c.get("mean_gen_pass_rate") is not None
        and c.get("address")
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if not args.dataset.exists():
        print(f"ERROR: dataset not found: {args.dataset}")
        sys.exit(1)

    # ── Resolve address list ──────────────────────────────────────────────
    # Priority: --from-comparison > --from-results > --addresses > all
    extra_addresses: list[str] = []

    if args.from_comparison is not None:
        if not args.from_comparison.exists():
            print(f"ERROR: --from-comparison file not found: {args.from_comparison}")
            sys.exit(1)
        extra_addresses = _addresses_from_comparison(args.from_comparison)
        print(f"  Loaded {len(extra_addresses)} address(es) from comparison file"
              f" (evaluated set).")

    elif args.from_results is not None:
        if not args.from_results.exists():
            print(f"ERROR: --from-results file not found: {args.from_results}")
            sys.exit(1)
        extra_addresses = _addresses_from_results(args.from_results)
        print(f"  Loaded {len(extra_addresses)} address(es) from results file.")

    # Merge with any explicit --addresses
    if extra_addresses:
        if args.addresses:
            combined = {a.lower() for a in args.addresses} | \
                       {a.lower() for a in extra_addresses}
            args.addresses = list(combined)
        else:
            args.addresses = extra_addresses

    # Discover contract directories
    if args.addresses:
        requested = {a.lower() for a in args.addresses}
        contract_dirs = sorted([
            d for d in args.dataset.iterdir()
            if d.is_dir() and d.name.lower() in requested
        ])
        missing = requested - {d.name.lower() for d in contract_dirs}
        if missing:
            print(f"  WARNING: {len(missing)} address(es) not found in dataset.")
    else:
        contract_dirs = sorted([d for d in args.dataset.iterdir() if d.is_dir()])

    if not contract_dirs:
        print("No contract directories found.")
        sys.exit(0)

    subset_label = (
        f"evaluated subset ({len(contract_dirs)} contracts)"
        if args.from_comparison or args.from_results
        else f"all {len(contract_dirs)} contracts"
    )
    print(f"Analysing {subset_label}  (K={args.k}) ...")


    results = []
    skipped = 0
    for i, contract_dir in enumerate(contract_dirs, 1):
        result = analyse_contract(contract_dir, args.k, verbose=args.verbose)
        if result is None:
            skipped += 1
            if args.verbose:
                print(f"  [{i}] {contract_dir.name}: skipped (missing files)")
            continue
        results.append(result)
        if not args.verbose and i % 25 == 0:
            print(f"  [{i}/{len(contract_dirs)}] processed ...")

    if skipped:
        print(f"  Skipped {skipped} contract(s) due to missing files.")

    aggregates = compute_aggregates(results)

    # Write JSON output
    output = {
        "k":         args.k,
        "dataset":   str(args.dataset),
        "aggregates": aggregates,
        "contracts":  results,
    }
    args.output.write_text(json.dumps(output, indent=2))
    print(f"Results written to {args.output}")

    print_summary(results, aggregates, args.k)


if __name__ == "__main__":
    main()