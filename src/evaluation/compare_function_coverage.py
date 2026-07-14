#!/usr/bin/env python3
"""
compare_function_coverage.py
============================

Compares FUNCTIONAL COVERAGE of the two transaction sources:

  HISTORICAL : distinct state-changing ABI functions exercised by the
               contract's real mainnet transactions (transactions.json),
               computed here by decoding each tx's 4-byte selector.

  SYNTHETIC  : distinct state-changing ABI functions successfully executed by
               the DCR-guided sequences, read from the coverage.json that
               make_sequences_hardhat.py already wrote per contract.

Why this matters for the paper: coverage is the *mechanism* behind the
discriminability result. If DCR sequences exercise functions that historical
traffic never touches, then defects in those functions are invisible to
historical replay by construction — which is exactly the access-control
over-restriction case (guards on functions only ever called by the owner
historically, or never called at all).

Outputs
-------
  <out>.json   per-contract + aggregate coverage comparison
  (stdout)     table + aggregates + the "synthetic-only functions" list

Usage
-----
  python3 compare_function_coverage.py \
      --dataset /workdir/src/dataset/contracts_dataset_1to20 \
      --dcr-dir /workdir/src/dcr_generation/generated_dcr_1to20_<safe> \
      --label   Qwen3.6-27B \
      --output  coverage_compare_<safe>.json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

try:
    from eth_hash.auto import keccak
except ImportError:
    print("ERROR: eth_hash not available. pip install eth-hash[pycryptodome] --break-system-packages")
    sys.exit(1)


def _mean(v):
    return round(sum(v) / len(v), 4) if v else None


def _median(v):
    if not v:
        return None
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


# ---------------------------------------------------------------------------
# ABI helpers
# ---------------------------------------------------------------------------

def state_changing_functions(abi: list) -> dict[str, str]:
    """
    Map 4-byte selector (hex, no 0x) -> function name, for state-changing
    (non view/pure) functions only. These are the functions an oracle can
    actually exercise.
    """
    out = {}
    for e in abi:
        if e.get("type") != "function":
            continue
        if e.get("stateMutability", "nonpayable") in ("view", "pure"):
            continue
        name = e.get("name", "")
        if not name:
            continue
        inputs = e.get("inputs", [])
        sig = name + "(" + ",".join(i["type"] for i in inputs) + ")"
        sel = keccak(sig.encode())[:4].hex()
        out[sel] = name
    return out


# ---------------------------------------------------------------------------
# Historical coverage: decode selectors from transactions.json
# ---------------------------------------------------------------------------

def historical_coverage(txs_path: Path, address: str,
                        sel_map: dict[str, str]) -> Optional[set[str]]:
    """Distinct state-changing functions actually called by historical txs."""
    if not txs_path.exists():
        return None
    try:
        txs = json.loads(txs_path.read_text())
    except Exception:
        return None
    if isinstance(txs, dict):
        txs = txs.get("result", txs.get("transactions", []))
    if not isinstance(txs, list):
        return None

    addr_lower = address.lower()
    hit: set[str] = set()
    for tx in txs:
        # only calls TO the contract, with calldata, that didn't error
        if (tx.get("to") or "").lower() != addr_lower:
            continue
        data = tx.get("input") or tx.get("data") or ""
        if not data or len(data) < 10:
            continue
        if tx.get("isError", "0") == "1":
            continue
        sel = data[2:10].lower()
        fn = sel_map.get(sel)
        if fn:
            hit.add(fn)
    return hit


# ---------------------------------------------------------------------------
# Synthetic coverage: read coverage.json from make_sequences_hardhat.py
# ---------------------------------------------------------------------------

def synthetic_coverage(cov_path: Path) -> Optional[set[str]]:
    """Distinct functions SUCCESSFULLY executed by the DCR synthetic sequence."""
    if not cov_path.exists():
        return None
    try:
        cov = json.loads(cov_path.read_text())
    except Exception:
        return None
    fc = cov.get("function_coverage", {})
    fns = fc.get("successful_functions")
    if fns is None:
        return None
    return set(fns)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Compare functional coverage: historical txs vs DCR synthetic sequences."
    )
    ap.add_argument("--dataset", type=Path, required=True,
                    help="Dataset dir (contains <addr>/abi.json, transactions.json).")
    ap.add_argument("--dcr-dir", type=Path, required=True, dest="dcr_dir",
                    help="DCR dir for one model (contains <addr>/coverage.json).")
    ap.add_argument("--label", default="model",
                    help="Model label for the printed title.")
    ap.add_argument("--output", type=Path, default=Path("coverage_compare.json"))
    args = ap.parse_args()

    for d, lbl in [(args.dataset, "--dataset"), (args.dcr_dir, "--dcr-dir")]:
        if not d.exists():
            print(f"ERROR: {lbl} not found: {d}")
            sys.exit(1)

    rows = []
    skipped = {"no_abi": 0, "no_hist": 0, "no_synth": 0, "no_fns": 0}

    for cdir in sorted(d for d in args.dataset.iterdir() if d.is_dir()):
        address = cdir.name

        abi_path = cdir / "abi.json"
        if not abi_path.exists():
            skipped["no_abi"] += 1
            continue
        try:
            abi = json.loads(abi_path.read_text())
        except Exception:
            skipped["no_abi"] += 1
            continue

        sel_map = state_changing_functions(abi)
        total_fns = set(sel_map.values())
        if not total_fns:
            skipped["no_fns"] += 1
            continue

        hist = historical_coverage(cdir / "transactions.json", address, sel_map)
        if hist is None:
            skipped["no_hist"] += 1
            continue

        synth = synthetic_coverage(args.dcr_dir / address / "coverage.json")
        if synth is None:
            skipped["no_synth"] += 1
            continue

        # Restrict synthetic to functions that are actually in the ABI's
        # state-changing set, so both sides use the same denominator.
        synth = synth & total_fns

        name = "?"
        meta_path = cdir / "metadata.json"
        if meta_path.exists():
            try:
                name = json.loads(meta_path.read_text()).get("contract_name", "?")
            except Exception:
                pass

        rows.append({
            "address":        address,
            "contract_name":  name,
            "total_fns":      len(total_fns),
            "hist_covered":   len(hist),
            "synth_covered":  len(synth),
            "hist_ratio":     round(len(hist) / len(total_fns), 4),
            "synth_ratio":    round(len(synth) / len(total_fns), 4),
            # The paper's key set: functions ONLY the synthetic oracle reaches
            "synth_only_fns": sorted(synth - hist),
            "hist_only_fns":  sorted(hist - synth),
            "n_synth_only":   len(synth - hist),
            "n_hist_only":    len(hist - synth),
        })

    if not rows:
        print("No comparable contracts. Skipped:", skipped)
        sys.exit(0)

    # ── Aggregates ──────────────────────────────────────────────────────────
    h_ratios = [r["hist_ratio"] for r in rows]
    s_ratios = [r["synth_ratio"] for r in rows]

    # Contracts where synthetic strictly beats historical
    synth_better = [r for r in rows if r["synth_ratio"] > r["hist_ratio"]]
    hist_better  = [r for r in rows if r["hist_ratio"] > r["synth_ratio"]]
    tied         = [r for r in rows if r["hist_ratio"] == r["synth_ratio"]]

    # Total distinct (contract, function) pairs reachable only by synthetic
    total_synth_only = sum(r["n_synth_only"] for r in rows)
    total_hist_only  = sum(r["n_hist_only"] for r in rows)

    agg = {
        "label":                    args.label,
        "n_contracts":              len(rows),
        "mean_hist_coverage":       _mean(h_ratios),
        "mean_synth_coverage":      _mean(s_ratios),
        "median_hist_coverage":     round(_median(h_ratios), 4),
        "median_synth_coverage":    round(_median(s_ratios), 4),
        "n_synth_better":           len(synth_better),
        "n_hist_better":            len(hist_better),
        "n_tied":                   len(tied),
        "total_synth_only_fn_pairs": total_synth_only,
        "total_hist_only_fn_pairs":  total_hist_only,
        "skipped":                  skipped,
    }

    args.output.write_text(json.dumps(
        {"aggregates": agg, "contracts": rows}, indent=2))

    # ── Print ───────────────────────────────────────────────────────────────
    def pct(v): return f"{v:.1%}" if v is not None else " n/a "

    print(f"\n  FUNCTION COVERAGE — historical vs DCR synthetic  [{args.label}]")
    sep = "─" * 88
    print("  " + sep)
    print(f"  {'Contract':<26}{'TotalFns':>9}{'Hist':>7}{'Synth':>7}"
          f"{'Hist%':>8}{'Synth%':>8}{'SynthOnly':>11}{'HistOnly':>10}")
    print("  " + sep)
    # sort by biggest synthetic advantage
    for r in sorted(rows, key=lambda x: x["synth_ratio"] - x["hist_ratio"],
                    reverse=True)[:40]:
        print(f"  {r['contract_name'][:25]:<26}{r['total_fns']:>9}"
              f"{r['hist_covered']:>7}{r['synth_covered']:>7}"
              f"{pct(r['hist_ratio']):>8}{pct(r['synth_ratio']):>8}"
              f"{r['n_synth_only']:>11}{r['n_hist_only']:>10}")
    if len(rows) > 40:
        print(f"  ... ({len(rows)-40} more rows in {args.output})")
    print("  " + sep)

    print(f"\n  AGGREGATE  [{args.label}]")
    print("  " + sep)
    print(f"  Contracts compared              : {agg['n_contracts']}")
    print(f"  Mean coverage — HISTORICAL      : {pct(agg['mean_hist_coverage'])}")
    print(f"  Mean coverage — SYNTHETIC (DCR) : {pct(agg['mean_synth_coverage'])}")
    print(f"  Median coverage — HISTORICAL    : {pct(agg['median_hist_coverage'])}")
    print(f"  Median coverage — SYNTHETIC     : {pct(agg['median_synth_coverage'])}")
    print()
    print(f"  Contracts where SYNTH > HIST    : {agg['n_synth_better']}")
    print(f"  Contracts where HIST  > SYNTH   : {agg['n_hist_better']}")
    print(f"  Tied                            : {agg['n_tied']}")
    print()
    print(f"  (contract,fn) pairs ONLY synth  : {agg['total_synth_only_fn_pairs']}"
          f"   ← functions historical replay never exercises")
    print(f"  (contract,fn) pairs ONLY hist   : {agg['total_hist_only_fn_pairs']}")
    print(f"  Skipped: {agg['skipped']}")
    print("  " + sep)

    # The money list: contracts with the largest synthetic-only function sets
    top = sorted(rows, key=lambda r: r["n_synth_only"], reverse=True)[:15]
    top = [r for r in top if r["n_synth_only"] > 0]
    if top:
        print(f"\n  Contracts with functions ONLY reachable via DCR synthetic:")
        for r in top:
            fns = ", ".join(r["synth_only_fns"][:6])
            more = f" (+{len(r['synth_only_fns'])-6})" if len(r["synth_only_fns"]) > 6 else ""
            print(f"    {r['contract_name'][:22]:<24} +{r['n_synth_only']:<3} {fns}{more}")

    print(f"\nWritten to {args.output}")


if __name__ == "__main__":
    main()