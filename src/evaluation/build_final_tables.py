#!/usr/bin/env python3
"""
build_final_tables.py
=====================

Assembles the paper's tables from the artifacts already produced.

  TABLE 0 — REPAIR
    compile@10 before/after the repair pass. Shows the metric is not saturated
    and that repair preserves the ranking (capability, not formatting luck).
    All behavioural evaluation below uses the REPAIRED sources.

  TABLE 1 — ORACLE COMPARISON (matched oracle)
    Historical vs DCR-synthetic replay, per model, where each model's generated
    contracts are judged by its OWN DCR sequences.
    Set difference of the diverged sets: Both / OnlyH / OnlyS.
    OnlyS = defects historical replay certifies as correct but synthetic catches.

  TABLE 1b — ORACLE COMPARISON (fixed oracle: Gemma-4-31B sequences)
    Same columns as Table 1, but every model's generated contracts are judged
    by GEMMA's DCR sequences. Comparing OnlyS between Table 1 and Table 1b
    answers: should we use the best model's sequences for everyone, or each
    model's own?  (ΔOnlyS > 0 favours the fixed oracle.)

  TABLE 2 — FUNCTION COVERAGE
    Historical vs synthetic function coverage + synth-only function pairs.
    NOTE: synthetic coverage is LOWER than historical in aggregate, but reaches
    functions historical never touches — the two oracles cover overlapping but
    NON-NESTED regions. That is the mechanism behind the complementarity in
    Tables 1/1b, not a "synthetic covers more" claim.

Usage:
  python3 build_final_tables.py --eval-root /workdir/src/evaluation \
      --latex tables.tex --output final_tables.json
"""

import argparse
import json
from pathlib import Path
from typing import Optional

# (model_file, short, compile@10 raw, compile@10 repaired)
MODELS = [
    ("gemma-4-31B-it-qat-UD-Q4_K_XL.gguf",                       "Gemma-4-31B",     0.8908, 0.9268),
    ("Qwen3.6-27B-UD-Q4_K_XL.gguf",                              "Qwen3.6-27B",     0.6472, 0.7132),
    ("Qwen.Qwen3-Coder-30B-A3B-Instruct.Q4_K_M.gguf",            "Qwen3-Coder-30B", 0.5316, 0.6175),
    ("gemma-4-12B-it-qat-UD-Q4_K_XL.gguf",                       "Gemma-4-12B",     0.4804, 0.5788),
    ("ornith-1.0-35b-Q4_K_M.gguf",                               "Ornith-35B",      0.4124, 0.5208),
    ("Codestral-22B-v0.1-Q4_K_M.gguf",                           "Codestral-22B",   0.3432, 0.3992),
    ("mistralai.Devstral-Small-2-24B-Instruct-2512.Q4_K_M.gguf", "Devstral-24B",    0.1884, 0.2640),
]

ORACLE_MODEL_SHORT = "Gemma-4-31B"   # the model whose sequences form the fixed oracle


def sanitize(name: str) -> str:
    return name.replace(".", "_").replace(" ", "_")


def _load(p: Path) -> Optional[dict]:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def diverged_set(depth: Optional[dict]) -> set[str]:
    if not depth:
        return set()
    return {c["address"].lower() for c in depth.get("contracts", [])
            if c.get("status_match_rate") is not None
            and c["status_match_rate"] < 1.0}


def names_for(depth: Optional[dict], addrs: set[str]) -> dict[str, str]:
    if not depth:
        return {}
    return {c["address"].lower(): c.get("contract_name", "?")
            for c in depth.get("contracts", [])
            if c["address"].lower() in addrs}


def pct(v, nd=1):
    return f"{v*100:.{nd}f}%" if v is not None else "n/a"


def lpct(v, nd=1):
    return f"{v*100:.{nd}f}\\%" if v is not None else "n/a"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-root", type=Path, default=Path("/workdir/src/evaluation"))
    ap.add_argument("--latex", type=Path, default=None)
    ap.add_argument("--output", type=Path, default=None)
    args = ap.parse_args()

    D_HIST = args.eval_root / "depth_historical"
    D_SYN  = args.eval_root / "depth_synthetic"
    D_GEM  = args.eval_root / "depth_synthetic_gemma"
    D_COV  = args.eval_root / "coverage"

    rows = []
    only_synth_union: dict[str, str] = {}

    for model, short, cpre, cpost in MODELS:
        safe = sanitize(model)

        hist = _load(D_HIST / f"replay_depth_HISTORICAL_{safe}.json")
        syn  = _load(D_SYN  / f"replay_depth_SYNTHETIC_{safe}.json")
        gem  = _load(D_GEM  / f"replay_depth_SYNTHETIC_GEMMA_{safe}.json")
        cov  = _load(D_COV  / f"coverage_compare_{safe}.json")

        h_agg = (hist or {}).get("aggregates", {})
        s_agg = (syn  or {}).get("aggregates", {})
        g_agg = (gem  or {}).get("aggregates", {})
        c_agg = (cov  or {}).get("aggregates", {})

        H = diverged_set(hist)
        S = diverged_set(syn)      # matched oracle
        G = diverged_set(gem)      # fixed (gemma) oracle

        # Set difference vs the SAME historical baseline in both cases
        both_m, onlyh_m, onlys_m = H & S, H - S, S - H
        both_g, onlyh_g, onlys_g = H & G, H - G, G - H

        for a, n in names_for(syn, onlys_m).items():
            only_synth_union[a] = n
        for a, n in names_for(gem, onlys_g).items():
            only_synth_union.setdefault(a, n)

        rows.append({
            "model": model, "short": short,
            "compile_raw": cpre, "compile_repaired": cpost,
            # historical
            "h_n": h_agg.get("n_contracts"),
            "h_status": h_agg.get("mean_status_match_rate"),
            "h_div": h_agg.get("n_status_diverged"),
            # synthetic matched
            "s_n": s_agg.get("n_contracts"),
            "s_status": s_agg.get("mean_status_match_rate"),
            "s_div": s_agg.get("n_status_diverged"),
            "m_both": len(both_m), "m_onlyh": len(onlyh_m), "m_onlys": len(onlys_m),
            # synthetic fixed (gemma) oracle
            "g_n": g_agg.get("n_contracts"),
            "g_status": g_agg.get("mean_status_match_rate"),
            "g_div": g_agg.get("n_status_diverged"),
            "g_both": len(both_g), "g_onlyh": len(onlyh_g), "g_onlys": len(onlys_g),
            # which oracle exposes more historical-invisible defects?
            "d_onlys": len(onlys_g) - len(onlys_m),
            # coverage
            "cov_n": c_agg.get("n_contracts"),
            "cov_hist": c_agg.get("mean_hist_coverage"),
            "cov_synth": c_agg.get("mean_synth_coverage"),
            "cov_synth_only": c_agg.get("total_synth_only_fn_pairs"),
        })

    # ══ TABLE 0 — REPAIR ═══════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("  TABLE 0 — COMPILE@10 BEFORE / AFTER REPAIR")
    print("=" * 72)
    print(f"  {'Model':<18}{'Raw':>9}{'Repaired':>11}{'Δ':>9}{'Rel.':>8}")
    print("  " + "-" * 68)
    for r in rows:
        d = r["compile_repaired"] - r["compile_raw"]
        rel = d / r["compile_raw"] if r["compile_raw"] else None
        print(f"  {r['short']:<18}{pct(r['compile_raw']):>9}"
              f"{pct(r['compile_repaired']):>11}{'+' + pct(d):>9}"
              f"{('+' + f'{rel*100:.0f}%') if rel else 'n/a':>8}")
    print("  " + "-" * 68)
    raws  = [r["compile_raw"] for r in rows]
    reps  = [r["compile_repaired"] for r in rows]
    ranking_kept = ([r["short"] for r in sorted(rows, key=lambda x: -x["compile_raw"])]
                    == [r["short"] for r in sorted(rows, key=lambda x: -x["compile_repaired"])])
    print(f"  Range raw {pct(min(raws))}–{pct(max(raws))}   "
          f"repaired {pct(min(reps))}–{pct(max(reps))}   "
          f"ranking preserved: {'YES' if ranking_kept else 'NO'}")
    print("  Repair fixes shallow syntax only; weak models stay weak.")
    print("  All behavioural evaluation below uses the REPAIRED sources.")
    print("=" * 72)

    # ══ TABLE 1 — matched oracle ═══════════════════════════════════════════
    print()
    print("=" * 106)
    print("  TABLE 1 — ORACLE COMPARISON (matched: each model judged by its OWN DCR sequences)")
    print("=" * 106)
    print(f"  {'Model':<18}{'cmp@10':>8}"
          f"{'H:N':>6}{'H:Stat':>8}{'H:Div':>7}"
          f"{'S:N':>6}{'S:Stat':>8}{'S:Div':>7}"
          f"{'Both':>7}{'OnlyH':>7}{'OnlyS':>7}")
    print("  " + "-" * 102)
    for r in rows:
        print(f"  {r['short']:<18}{pct(r['compile_repaired'],0):>8}"
              f"{str(r['h_n'] or '-'):>6}{pct(r['h_status']):>8}"
              f"{str(r['h_div'] if r['h_div'] is not None else '-'):>7}"
              f"{str(r['s_n'] or '-'):>6}{pct(r['s_status']):>8}"
              f"{str(r['s_div'] if r['s_div'] is not None else '-'):>7}"
              f"{r['m_both']:>7}{r['m_onlyh']:>7}{r['m_onlys']:>7}")
    print("  " + "-" * 102)
    print("  cmp@10 = compile@10 AFTER repair (the source actually under test).")
    print("  OnlyS = divergent under synthetic but NOT historical  ← the paper's claim.")
    print("=" * 106)

    # ══ TABLE 1b — fixed oracle, SAME columns ══════════════════════════════
    print()
    print("=" * 118)
    print(f"  TABLE 1b — ORACLE COMPARISON (fixed: every model judged by {ORACLE_MODEL_SHORT}'s DCR sequences)")
    print("=" * 118)
    print(f"  {'Model':<18}{'cmp@10':>8}"
          f"{'H:N':>6}{'H:Stat':>8}{'H:Div':>7}"
          f"{'G:N':>6}{'G:Stat':>8}{'G:Div':>7}"
          f"{'Both':>7}{'OnlyH':>7}{'OnlyS':>7}{'ΔOnlyS':>9}")
    print("  " + "-" * 114)
    for r in rows:
        if r["g_n"] is None:
            continue
        d = r["d_onlys"]
        dstr = f"{d:+d}"
        print(f"  {r['short']:<18}{pct(r['compile_repaired'],0):>8}"
              f"{str(r['h_n'] or '-'):>6}{pct(r['h_status']):>8}"
              f"{str(r['h_div'] if r['h_div'] is not None else '-'):>7}"
              f"{r['g_n']:>6}{pct(r['g_status']):>8}{r['g_div']:>7}"
              f"{r['g_both']:>7}{r['g_onlyh']:>7}{r['g_onlys']:>7}{dstr:>9}")
    print("  " + "-" * 114)
    print(f"  G = synthetic oracle using {ORACLE_MODEL_SHORT}'s sequences for ALL models.")
    print("  ΔOnlyS = OnlyS(fixed) − OnlyS(matched).  Positive ⇒ the fixed oracle exposes")
    print("  MORE historical-invisible defects ⇒ prefer the best model's sequences for everyone.")
    print("  Negative ⇒ each model's own sequences are the better oracle.")
    # verdict
    deltas = [r["d_onlys"] for r in rows if r["g_n"] is not None]
    if deltas:
        pos = sum(1 for d in deltas if d > 0)
        neg = sum(1 for d in deltas if d < 0)
        tot = sum(deltas)
        print(f"  → fixed better on {pos}/{len(deltas)} models, matched better on {neg}; "
              f"net ΔOnlyS = {tot:+d}")
    print("=" * 118)

    # ══ TABLE 2 — coverage ═════════════════════════════════════════════════
    print()
    print("=" * 90)
    print("  TABLE 2 — FUNCTION COVERAGE")
    print("=" * 90)
    print(f"  {'DCR source model':<20}{'N':>6}{'Hist':>9}{'Synth':>9}{'Δ':>9}{'Synth-only fns':>18}")
    print("  " + "-" * 86)
    for r in rows:
        if r["cov_n"] is None:
            continue
        gain = (r["cov_synth"] - r["cov_hist"]) if (
            r["cov_synth"] is not None and r["cov_hist"] is not None) else None
        gstr = f"{gain*100:+.1f}%" if gain is not None else "n/a"
        print(f"  {r['short']:<20}{r['cov_n']:>6}{pct(r['cov_hist']):>9}"
              f"{pct(r['cov_synth']):>9}{gstr:>9}{str(r['cov_synth_only']):>18}")
    print("  " + "-" * 86)
    print("  Historical coverage is identical across rows (same on-chain transactions).")
    print("  Synthetic coverage is LOWER in aggregate, yet reaches functions historical")
    print("  NEVER exercises (Synth-only): the two oracles cover overlapping but")
    print("  NON-NESTED regions of the call space — the mechanism behind Tables 1/1b.")
    print("=" * 90)

    # ══ case-study candidates ══════════════════════════════════════════════
    if only_synth_union:
        print()
        print("  CASE-STUDY CANDIDATES — divergent under synthetic, clean under historical")
        print("  " + "-" * 80)
        for addr, name in sorted(only_synth_union.items(), key=lambda x: x[1]):
            print(f"    {name:<26} {addr}")
        print("  " + "-" * 80)

    if args.output:
        args.output.write_text(json.dumps(
            {"rows": rows, "only_synth_union": only_synth_union}, indent=2))
        print(f"\nJSON written to {args.output}")

    # ══ LaTeX ══════════════════════════════════════════════════════════════
    if args.latex:
        L = []

        # Table 0 — repair
        L.append(r"% ---------- Table 0: repair ----------")
        L.append(r"\begin{table}[t]\centering\small")
        L.append(r"\caption{\textsc{compile@10} before and after the repair pass. "
                 r"Repair fixes shallow syntactic defects only; it lifts every model "
                 r"but preserves the ranking, indicating that \textsc{compile@k} "
                 r"reflects capability rather than formatting artefacts. All "
                 r"behavioural evaluation uses the repaired sources.}")
        L.append(r"\label{tab:repair}")
        L.append(r"\begin{tabular}{l r r r}")
        L.append(r"\toprule")
        L.append(r"Model & Raw & Repaired & $\Delta$ \\")
        L.append(r"\midrule")
        for r in rows:
            d = r["compile_repaired"] - r["compile_raw"]
            L.append(f"{r['short']} & {lpct(r['compile_raw'])} & "
                     f"\\textbf{{{lpct(r['compile_repaired'])}}} & $+${lpct(d)} \\\\")
        L.append(r"\bottomrule\end{tabular}\end{table}")
        L.append("")

        # Table 1 — matched
        L.append(r"% ---------- Table 1: matched oracle ----------")
        L.append(r"\begin{table*}[t]\centering\small")
        L.append(r"\caption{Oracle comparison with the \emph{matched} synthetic oracle: "
                 r"each model's generated contracts are replayed against DCR sequences "
                 r"extracted by that same model. \emph{Only S} counts contracts flagged "
                 r"divergent by the synthetic oracle but certified correct by historical "
                 r"replay.}")
        L.append(r"\label{tab:oracle_matched}")
        L.append(r"\begin{tabular}{l r r r r r r r r r r}")
        L.append(r"\toprule")
        L.append(r"Model & compile@10 & \multicolumn{3}{c}{Historical} & "
                 r"\multicolumn{3}{c}{Synthetic (own DCR)} & \multicolumn{3}{c}{Overlap} \\")
        L.append(r"\cmidrule(lr){3-5}\cmidrule(lr){6-8}\cmidrule(lr){9-11}")
        L.append(r" & & $N$ & Status & Div. & $N$ & Status & Div. & Both & Only H & Only S \\")
        L.append(r"\midrule")
        for r in rows:
            L.append(
                f"{r['short']} & {lpct(r['compile_repaired'])} & "
                f"{r['h_n'] or '--'} & {lpct(r['h_status'])} & {r['h_div'] if r['h_div'] is not None else '--'} & "
                f"{r['s_n'] or '--'} & {lpct(r['s_status'])} & {r['s_div'] if r['s_div'] is not None else '--'} & "
                f"{r['m_both']} & {r['m_onlyh']} & \\textbf{{{r['m_onlys']}}} \\\\")
        L.append(r"\bottomrule\end{tabular}\end{table*}")
        L.append("")

        # Table 1b — fixed
        L.append(r"% ---------- Table 1b: fixed oracle ----------")
        L.append(r"\begin{table*}[t]\centering\small")
        L.append(r"\caption{Oracle comparison with a \emph{fixed} synthetic oracle: every "
                 r"model's generated contracts are replayed against DCR sequences extracted "
                 r"by " + ORACLE_MODEL_SHORT + r" (the highest \textsc{compile@10}). "
                 r"$\Delta$\,Only\,S is the change relative to the matched oracle "
                 r"(Table~\ref{tab:oracle_matched}); positive values indicate the fixed "
                 r"oracle exposes more historically-invisible defects.}")
        L.append(r"\label{tab:oracle_fixed}")
        L.append(r"\begin{tabular}{l r r r r r r r r r r r}")
        L.append(r"\toprule")
        L.append(r"Model & compile@10 & \multicolumn{3}{c}{Historical} & "
                 r"\multicolumn{3}{c}{Synthetic (fixed DCR)} & "
                 r"\multicolumn{3}{c}{Overlap} & $\Delta$\,Only\,S \\")
        L.append(r"\cmidrule(lr){3-5}\cmidrule(lr){6-8}\cmidrule(lr){9-11}")
        L.append(r" & & $N$ & Status & Div. & $N$ & Status & Div. & Both & Only H & Only S & \\")
        L.append(r"\midrule")
        for r in rows:
            if r["g_n"] is None:
                continue
            L.append(
                f"{r['short']} & {lpct(r['compile_repaired'])} & "
                f"{r['h_n'] or '--'} & {lpct(r['h_status'])} & {r['h_div'] if r['h_div'] is not None else '--'} & "
                f"{r['g_n']} & {lpct(r['g_status'])} & {r['g_div']} & "
                f"{r['g_both']} & {r['g_onlyh']} & \\textbf{{{r['g_onlys']}}} & ${r['d_onlys']:+d}$ \\\\")
        L.append(r"\bottomrule\end{tabular}\end{table*}")
        L.append("")

        # Table 2 — coverage
        L.append(r"% ---------- Table 2: coverage ----------")
        L.append(r"\begin{table}[t]\centering\small")
        L.append(r"\caption{Functional coverage. Historical coverage is identical across "
                 r"rows (the same on-chain transactions); only the model extracting the DCR "
                 r"graph varies. Synthetic coverage is lower in aggregate, yet reaches "
                 r"\emph{Synth-only} (contract,\,function) pairs that historical traffic "
                 r"never exercises: the two oracles cover overlapping but non-nested "
                 r"regions of the call space.}")
        L.append(r"\label{tab:coverage}")
        L.append(r"\begin{tabular}{l r r r r}")
        L.append(r"\toprule")
        L.append(r"DCR source & $N$ & Historical & Synthetic & Synth-only \\")
        L.append(r"\midrule")
        for r in rows:
            if r["cov_n"] is None:
                continue
            L.append(f"{r['short']} & {r['cov_n']} & {lpct(r['cov_hist'])} & "
                     f"{lpct(r['cov_synth'])} & {r['cov_synth_only']} \\\\")
        L.append(r"\bottomrule\end{tabular}\end{table}")

        args.latex.write_text("\n".join(L) + "\n")
        print(f"LaTeX written to {args.latex}")


if __name__ == "__main__":
    main()