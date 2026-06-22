#!/usr/bin/env python3
"""
Analizza la distribuzione del numero di funzioni da implementare per contratto.

Usa _find_functions da generate_oneshot.py per garantire che il conteggio
sia identico a quello usato durante la generazione — nessuna divergenza.

Uso:
  python analyze_dataset.py <dataset_dir>
  python3 analyze_dataset.py ./contracts_dataset_pass_08 --save-range 1 20 --output-dir ./contracts_dataset_1to20
  python analyze_dataset.py <dataset_dir> --save-range 1 10 --output-dir ./contracts_dataset_feasible
  python analyze_dataset.py <dataset_dir> --save-feasible   # shortcut per 1-10
"""
import sys, json, re, shutil, statistics, types
from pathlib import Path
from collections import Counter

# ── Importa _find_functions e _main_source_text da generate_oneshot ───────────
# generate_oneshot ha dipendenze pesanti (storage, hardhat_utils, pipeline,
# llm_client). Le stubbizziamo prima dell'import in modo che le sole funzioni
# che ci interessano (_find_functions, _main_source_text) siano disponibili
# senza richiedere l'ambiente completo di addestramento.

# sys.path.append("/workdir/src/source_generation/")
sys.path.append("/workdir/src/utils/")
from source_utils import _find_functions, _main_source_text

# ── Fallback locale (stessa logica di generate_oneshot) ───────────────────────

def _find_matching_brace(source: str, open_pos: int) -> int:
    depth = 0
    for i in range(open_pos, len(source)):
        if source[i] == '{':   depth += 1
        elif source[i] == '}':
            depth -= 1
            if depth == 0: return i
    return -1


def _find_functions_local(source: str) -> list[dict]:
    """
    Stessa logica di generate_oneshot._find_functions.
    Esclude: costruttori, view/pure, funzioni con body vuoto.
    """
    fn_re = re.compile(r'\bfunction\s+(\w+)\s*\([^)]*\)[^;{]*', re.DOTALL)
    results = []
    for m in fn_re.finditer(source):
        fn_name = m.group(1)
        if fn_name.lower() == "constructor":
            continue
        # view/pure check nel modifier region
        modifier_region = source[m.start():m.end() + 30]
        if re.search(r'\b(view|pure)\b', modifier_region):
            continue
        tail  = source[m.end():]
        brace = tail.find('{')
        semi  = tail.find(';')
        if brace == -1: continue
        if semi != -1 and semi < brace: continue
        abs_b = m.end() + brace
        close = _find_matching_brace(source, abs_b)
        if close == -1: continue
        body = source[abs_b+1:close]
        body_s = re.sub(r'//[^\n]*', '', body)
        body_s = re.sub(r'/\*.*?\*/', '', body_s, flags=re.DOTALL)
        if not body_s.strip(): continue
        results.append({
            "fn_sig":     m.group(0).strip(),
            "body_open":  abs_b,
            "body_close": close + 1,
        })
    return results


def count_functions(contract_dir: Path) -> int:
    """
    Conta le funzioni implementabili nel contratto.
    Usa _main_source_text + _find_functions — identico a generate_oneshot.run_oneshot,
    quindi un contratto con n=0 qui sarà sempre skippato anche lì.
    """
    try:
        source, _ = _main_source_text(contract_dir)
    except Exception:
        return 0
    if not source:
        return 0
    try:
        return len(_find_functions(source))
    except Exception:
        return 0


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("dataset_dir", nargs="?", default=".")
    p.add_argument("--save-range",    type=int, nargs=2, metavar=("MIN", "MAX"),
                   help="Copia i contratti con MIN<=fns<=MAX in --output-dir")
    p.add_argument("--output-dir",    type=Path, default=None)
    p.add_argument("--save-feasible", action="store_true",
                   help="Shortcut per --save-range 1 10")
    p.add_argument("--symlink",       action="store_true",
                   help="Usa symlink invece di copiare (più veloce, meno spazio)")
    args = p.parse_args()

    if args.save_feasible and args.save_range is None:
        args.save_range = (1, 10)
        args.output_dir = args.output_dir or Path("contracts_dataset_feasible")

    dataset_dir = Path(args.dataset_dir)
    contracts   = sorted([
        d for d in dataset_dir.iterdir()
        if d.is_dir() and (d / "metadata.json").exists()
    ])
    print(f"Dataset: {dataset_dir}  ({len(contracts)} contracts)\n")

    fn_counts = []   # (n_fns, address, contract_name)
    buckets   = Counter()
    skipped   = []

    for cd in contracts:
        try:
            cname = json.loads((cd / "metadata.json").read_text()).get("contract_name", "?")
        except Exception:
            cname = "?"

        n = count_functions(cd)

        if n == 0 and not (cd / "source.sol").exists() and not (cd / "source.json").exists():
            skipped.append(cd.name[:16])
            continue

        fn_counts.append((n, cd.name, cname))

        if   n == 0:    buckets["0"]     += 1
        elif n <= 5:    buckets["1-5"]   += 1
        elif n <= 10:   buckets["6-10"]  += 1
        elif n <= 20:   buckets["11-20"] += 1
        elif n <= 30:   buckets["21-30"] += 1
        else:           buckets["30+"]   += 1

    ns    = [n for n, _, _ in fn_counts]
    total = len(fn_counts)

    # ── Distribution table ────────────────────────────────────────────────────
    print(f"{'Bucket':<12}  {'Count':>6}  {'%':>6}")
    print("-" * 30)
    for label in ["0", "1-5", "6-10", "11-20", "21-30", "30+"]:
        c = buckets[label]
        print(f"  {label:<10}  {c:>6}  {c/total*100:>5.1f}%")
    print()

    if ns:
        print(f"min={min(ns)}  max={max(ns)}  "
              f"median={statistics.median(ns):.0f}  "
              f"mean={statistics.mean(ns):.1f}  "
              f"p75={sorted(ns)[int(len(ns)*0.75)]}  "
              f"p90={sorted(ns)[int(len(ns)*0.90)]}")
        print()

    for lo, hi in [(1, 5), (1, 10), (1, 20)]:
        subset = [(n, a, c) for n, a, c in fn_counts if lo <= n <= hi]
        print(f"  {lo}-{hi} functions:  {len(subset):>5} / {total}  "
              f"({len(subset)/total*100:.1f}%)")
    print()

    print("Top 10 most complex contracts:")
    for n, addr, cname in sorted(fn_counts, reverse=True)[:10]:
        print(f"  {addr[:18]}…  {cname:<30}  {n} functions")


    # ── Save range ────────────────────────────────────────────────────────────
    if args.save_range:
        lo, hi  = args.save_range
        out_dir = args.output_dir or Path(f"contracts_dataset_{lo}to{hi}")
        # Escludi esplicitamente n==0: nessuna funzione da generare
        subset  = [(n, addr, cn) for n, addr, cn in fn_counts
                   if lo <= n <= hi and n > 0]

        print(f"\n{'─'*60}")
        print(f"Saving {lo}–{hi} function contracts → {out_dir}")
        print(f"  Contracts: {len(subset)}  (excluded 0-fn: "
              f"{sum(1 for n,_,_ in fn_counts if n==0)})")

        out_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for _, addr, _ in subset:
            src_dir = dataset_dir / addr
            dst_dir = out_dir / addr
            if dst_dir.exists(): continue
            if args.symlink:
                dst_dir.symlink_to(src_dir.resolve())
            else:
                shutil.copytree(src_dir, dst_dir)
            copied += 1

        print(f"  Copied: {copied}  (already existed: {len(subset) - copied})")
        print(f"  Done → {out_dir}")

        addr_list = out_dir / "addresses.txt"
        addr_list.write_text("\n".join(addr for _, addr, _ in sorted(subset)))
        print(f"  Address list → {addr_list}")


if __name__ == "__main__":
    main()