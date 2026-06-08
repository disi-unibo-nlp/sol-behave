"""
filter_dataset.py

Crea una directory con symlink (o copie) dei soli contratti salvati,
ovvero quelli con checkpoint.json in stato DONE e saved=True.

Uso:
    python filter_dataset.py --input-dir ./contracts_dataset --output-dir ./dataset_filtered
    python filter_dataset.py --input-dir ./contracts_dataset --output-dir ./dataset_filtered --copy
    python filter_dataset.py --input-dir ./contracts_dataset --output-dir ./contracts_dataset_pass_08 --min-pass-rate 0.8

Di default usa symlink (veloce, zero spazio disco).
Con --copy fa una copia fisica delle directory.
"""

import argparse
import json
import shutil
from pathlib import Path


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


# python3 filter_dataset.py --input-dir contracts_dataset --output-dir contracts_dataset_pass_08 --min-pass-rate 0.8 --copy

def main():
    p = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--input-dir",      type=Path, required=True,
                   help="Directory sorgente (contracts_dataset).")
    p.add_argument("--output-dir",     type=Path, required=True,
                   help="Directory di output con i soli contratti filtrati.")
    p.add_argument("--min-pass-rate",  type=float, default=0.5,
                   help="Pass rate minimo per includere un contratto.")
    p.add_argument("--copy",           action="store_true",
                   help="Copia le directory invece di creare symlink.")
    p.add_argument("--dry-run",        action="store_true",
                   help="Mostra cosa farebbe senza scrivere nulla.")
    args = p.parse_args()

    if not args.input_dir.exists():
        print(f"✗ Input dir not found: {args.input_dir}")
        return

    if not args.dry_run:
        args.output_dir.mkdir(parents=True, exist_ok=True)

    included, skipped = [], []

    for contract_dir in sorted(args.input_dir.iterdir()):
        if not contract_dir.is_dir():
            continue

        ckpt = _load_json(contract_dir / "checkpoint.json")
        state    = ckpt.get("state")
        saved    = ckpt.get("saved", False)
        pr       = ckpt.get("pass_rate")

        # Contratto utilizzabile: DONE + saved + pass_rate >= soglia
        if state != "DONE" or not saved or pr is None or pr < args.min_pass_rate:
            skipped.append((contract_dir.name, state, pr))
            continue

        dest = args.output_dir / contract_dir.name

        if not args.dry_run:
            if dest.exists() or dest.is_symlink():
                # Già presente — salta silenziosamente
                included.append((contract_dir.name, pr))
                continue

            if args.copy:
                shutil.copytree(contract_dir, dest)
            else:
                dest.symlink_to(contract_dir.resolve())

        included.append((contract_dir.name, pr))

    # Report
    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Dataset filter")
    print(f"  Input:    {args.input_dir}  ({len(list(args.input_dir.iterdir()))} dirs)")
    print(f"  Output:   {args.output_dir}")
    print(f"  Mode:     {'copy' if args.copy else 'symlink'}")
    print(f"  Min pass_rate: {args.min_pass_rate:.0%}")
    print()
    print(f"  Included: {len(included)}")
    print(f"  Skipped:  {len(skipped)}")

    if included:
        prs = [pr for _, pr in included]
        print(f"  pass_rate — mean={sum(prs)/len(prs):.3f}"
              f"  min={min(prs):.3f}  max={max(prs):.3f}")

    if not args.dry_run and not args.copy:
        print(f"\n  ✓ Created {len(included)} symlinks → {args.output_dir}")
    elif not args.dry_run:
        print(f"\n  ✓ Copied {len(included)} directories → {args.output_dir}")


if __name__ == "__main__":
    main()