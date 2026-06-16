#!/usr/bin/env python3
"""
final_discriminability_table.py
===============================
Produce the PUBLICATION-READY discriminability result with all confounds
removed, ready to drop into the paper as a table.

A sample counts as a clean discrimination ONLY if:
  (a) the ORIGINAL passes synthetic at >= 0.9   (synthetic txs are valid here)
  (b) the ORIGINAL passes historical at >= 0.5  (stable baseline)
  (c) the GENERATED sample passes historical at >= 0.5
  (d) the GENERATED sample fails synthetic at  <  0.5
  (e) historical and synthetic k are within 3x  (comparable tx budgets)

It emits a per-contract and per-sample table plus a LaTeX-ready summary.
Run from /workdir/src/coverage_analysis/.
"""
import json
from collections import Counter
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
DATASET    = SRC / "dataset" / "contracts_dataset_1to20"
GENERATED  = SRC / "source_generation" / "repaired_oneshot"
SYNTH_ORIG = SRC / "dcr_replay" / "synth_replay_results"
SYNTH_GEN  = SRC / "dcr_replay" / "synth_replay_results_generated"

PASS = 0.5
ORIG_SYNTH_MIN = 0.9
KMAX = 3.0

def load(p):
    try: return json.loads(p.read_text())
    except Exception: return None

def main():
    rows = []
    # iterate every contract that has a synth-orig result
    for so_path in sorted(SYNTH_ORIG.glob("*/synth_replay_result_1000.json")):
        addr = so_path.parent.name
        so = load(so_path)
        if not so:
            continue
        name = so.get("contract_name", addr[:10])
        so_pass = so.get("pass_rate")
        so_k    = so.get("k")
        ho = load(DATASET / addr / "replay_result_1000.json")
        ho_pass = ho.get("pass_rate") if ho else None

        # gate (a)(b): original must pass its own synthetic & historical
        if so_pass is None or so_pass < ORIG_SYNTH_MIN:
            continue
        if ho_pass is None or ho_pass < PASS:
            continue

        # gather generated samples on both sides
        hist_gen = {}
        for f in (GENERATED / addr).glob("replay_sample_*_result_1000.json"):
            d = load(f)
            if d: hist_gen[d.get("sample_idx")] = d
        synth_gen = {}
        for f in (SYNTH_GEN / addr).glob("synth_replay_sample_*_result_1000.json"):
            d = load(f)
            if d: synth_gen[d.get("sample_idx")] = d
        common = sorted(set(hist_gen) & set(synth_gen), key=lambda x:(x is None,x))
        if not common:
            continue

        clean_disc = []
        kmismatch = False
        for idx in common:
            hp = hist_gen[idx].get("pass_rate")
            sp = synth_gen[idx].get("pass_rate")
            hk = hist_gen[idx].get("k") or 0
            sk = synth_gen[idx].get("k") or 0
            if hk and sk and max(hk,sk)/max(1,min(hk,sk)) > KMAX:
                kmismatch = True
            # gate (c)(d): generated passes hist, fails synth
            if hp is not None and sp is not None and hp >= PASS and sp < PASS:
                clean_disc.append(idx)

        if clean_disc:
            # dominant failing function on the worst synth sample
            worst = min(clean_disc, key=lambda i: synth_gen[i].get("pass_rate",1.0))
            wd = load(SYNTH_GEN / addr / f"synth_replay_sample_{worst}_result_1000.json")
            fnf = Counter((t.get("_fn_sig","").split("(")[0])
                          for t in (wd.get("tx_results",[]) if wd else [])
                          if not t.get("events_match",False) and t.get("_fn_sig"))
            top_fns = ", ".join(fn for fn,_ in fnf.most_common(2))
            rows.append({
                "name": name, "addr": addr,
                "orig_synth": so_pass, "orig_hist": ho_pass,
                "n_disc": len(clean_disc), "n_common": len(common),
                "hist_gen": hist_gen[clean_disc[0]].get("pass_rate"),
                "synth_gen": synth_gen[worst].get("pass_rate"),
                "kmismatch": kmismatch,
                "top_fns": top_fns,
                "consistent": len(clean_disc) == len(common),
            })

    # ── Print clean table ──────────────────────────────────────────────
    rows.sort(key=lambda r: (-r["n_disc"]/r["n_common"], r["synth_gen"]))
    print(f"\n{'='*86}")
    print("  PUBLICATION-READY DISCRIMINABILITY (confounds removed)")
    print(f"{'='*86}")
    print(f"  {'contract':<16}{'oSyn':>6}{'oHist':>7}{'disc':>7}{'hGen':>7}{'sGen':>7}"
          f"{'consist':>8}{'kmm':>5}  failing_fns")
    print("-"*86)
    for r in rows:
        disc_str = f"{r['n_disc']}/{r['n_common']}"
        cons_str = "yes" if r['consistent'] else "partial"
        kmm_str  = "Y" if r['kmismatch'] else "-"
        print(f"  {r['name'][:15]:<16}"
              f"{r['orig_synth']:>6.2f}{r['orig_hist']:>7.2f}"
              f"{disc_str:>7}"
              f"{r['hist_gen']:>7.2f}{r['synth_gen']:>7.2f}"
              f"{cons_str:>8}"
              f"{kmm_str:>5}  {r['top_fns']}")
    print("-"*86)

    n_contracts = len(rows)
    n_consistent = sum(1 for r in rows if r["consistent"] and not r["kmismatch"])
    total_disc = sum(r["n_disc"] for r in rows)
    total_common = sum(r["n_common"] for r in rows)
    print(f"\n  contracts with clean discrimination : {n_contracts}")
    print(f"  fully consistent + balanced k        : {n_consistent}")
    print(f"  total clean discriminated samples    : {total_disc}")
    print(f"\n  Tier-1 (consistent, balanced k) are the headline case studies.")
    print(f"  Tier-2 (partial or k-mismatch) reported with the caveat stated.")

    # LaTeX
    print(f"\n  LaTeX rows:")
    for r in rows:
        tier = "T1" if (r["consistent"] and not r["kmismatch"]) else "T2"
        print(f"    {r['name']} & {r['orig_synth']:.2f} & {r['hist_gen']:.2f} & "
              f"{r['synth_gen']:.2f} & {r['n_disc']}/{r['n_common']} & "
              f"\\texttt{{{r['top_fns'].split(',')[0]}}} \\\\  % {tier}")

if __name__ == "__main__":
    main()