"""
inspect_dataset.py  —  analizza lo stato dei contratti in output_dir.

Uso:
    python inspect_dataset.py --output-dir ./contracts_dataset
    python inspect_dataset.py --output-dir ./contracts_dataset --verbose
    python inspect_dataset.py --output-dir ./contracts_dataset --saved-only
"""
import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _get_category(ckpt: dict, contract_dir: Path) -> str:
    """
    Legge la categoria dal checkpoint se presente, altrimenti da metadata.json.
    deploy_replay.py scrive il checkpoint senza category; process_candidate
    in crawl.py lo aggiunge dopo, quindi non è sempre presente.
    """
    cat = ckpt.get("category")
    if cat and cat != "unknown":
        return cat
    meta = _load_json(contract_dir / "metadata.json")
    cat = meta.get("category")
    if cat and cat != "unknown":
        return cat
    return "unknown"


def _pass_rate_bucket(pr: float) -> str:
    """
    Bucket con intervalli [a, b) tranne l'ultimo che è [1.0, 1.0].
    Restituisce una stringa chiave ordinabile.
    """
    if pr >= 1.0:
        return "1.00"
    b = int(pr * 10) / 10.0
    return f"{b:.1f}"


def _bucket_label(key: str) -> str:
    if key == "1.00":
        return "[1.00, 1.00]"
    b = float(key)
    return f"[{b:.1f}, {b+0.1:.1f})"


def main():
    p = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--output-dir",  type=Path, default=Path("./contracts_dataset"),
                   help="Directory del dataset.")
    p.add_argument("--verbose",     action="store_true",
                   help="Mostra dettagli per ogni contratto FAILED e saved.")
    p.add_argument("--saved-only",  action="store_true",
                   help="Mostra solo i contratti salvati (pass_rate >= soglia).")
    args = p.parse_args()

    states     = Counter()
    reasons    = Counter()
    categories = defaultdict(Counter)
    pass_rates = []
    saved      = []

    for contract_dir in sorted(args.output_dir.iterdir()):
        if not contract_dir.is_dir():
            continue

        ckpt_file = contract_dir / "checkpoint.json"
        if not ckpt_file.exists():
            states["NO_CHECKPOINT"] += 1
            continue

        ckpt = _load_json(ckpt_file)
        if not ckpt:
            states["CORRUPT"] += 1
            continue

        state    = ckpt.get("state", "UNKNOWN")
        category = _get_category(ckpt, contract_dir)

        states[state] += 1
        categories[state][category] += 1

        if state == "FAILED":
            reason = ckpt.get("reason", "unknown")
            reasons[reason] += 1
            if args.verbose:
                print(f"  FAILED {contract_dir.name[:14]}…"
                      f"  cat={category:<15}  reason={reason}")

        elif state == "DONE":
            pr = ckpt.get("pass_rate")
            if pr is not None:
                pass_rates.append(pr)
            is_saved = ckpt.get("saved", False)
            entry = {
                "address":       contract_dir.name,
                "category":      category,
                "pass_rate":     pr,
                "contract_name": ckpt.get("contract_name", ""),
            }
            if is_saved:
                saved.append(entry)

    # ── Report ────────────────────────────────────────────────────────────────
    total = sum(states.values())
    print(f"\n{'='*62}")
    print(f"  Dataset:  {args.output_dir}")
    print(f"  Dirs:     {total}")
    print(f"{'='*62}\n")

    print("State distribution:")
    for state, count in states.most_common():
        pct     = count / total * 100 if total else 0
        cat_top = dict(categories[state].most_common(5))
        print(f"  {state:<15} {count:>5}  ({pct:5.1f}%)  {cat_top}")

    if reasons:
        print(f"\nFailure reasons:")
        for reason, count in reasons.most_common():
            print(f"  {reason:<40} {count:>5}")

    if pass_rates:
        done_count  = len(pass_rates)
        saved_count = len(saved)
        print(f"\nDONE contracts ({done_count}):")
        print(f"  mean={statistics.mean(pass_rates):.3f}"
              f"  median={statistics.median(pass_rates):.3f}"
              f"  min={min(pass_rates):.3f}"
              f"  max={max(pass_rates):.3f}")
        print(f"  saved (pass_rate >= threshold): {saved_count}/{done_count}"
              f"  ({saved_count/done_count*100:.1f}%)")

        # Histogram con intervalli corretti
        buckets: Counter = Counter()
        for pr in pass_rates:
            buckets[_pass_rate_bucket(pr)] += 1

        bar_scale = max(1, max(buckets.values()) // 40)
        print(f"\n  pass_rate distribution"
              f"  (each █ ≈ {bar_scale} contract{'s' if bar_scale > 1 else ''}):")

        all_keys = [f"{i/10:.1f}" for i in range(10)] + ["1.00"]
        for key in all_keys:
            count = buckets.get(key, 0)
            if count == 0:
                continue
            label = _bucket_label(key)
            bar   = "█" * min(50, max(1, count // bar_scale))
            print(f"    {label}  {count:>5}  {bar}")

    if saved:
        print(f"\nSaved contracts by category ({len(saved)} total):")
        cat_count = Counter(s["category"] for s in saved)
        for cat, count in cat_count.most_common():
            print(f"  {cat:<20} {count:>4}")

        if args.verbose:
            print(f"\nSaved contracts detail:")
            for s in sorted(saved, key=lambda x: x["pass_rate"] or 0, reverse=True):
                print(f"  {s['address'][:14]}…  {s['category']:<20}"
                      f"  pass={s['pass_rate']:.3f}  [{s['contract_name']}]")


if __name__ == "__main__":
    main()