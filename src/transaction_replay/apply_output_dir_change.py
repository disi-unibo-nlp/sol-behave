#!/usr/bin/env python3
"""
apply_output_dir_change.py
==========================

Adds an --output-dir option to replay_generated.py so replay outputs
(checkpoints, per-sample results, per-contract summaries) can be written to a
separate directory instead of next to the samples. Backward-compatible: if
--output-dir is omitted, outputs go where they always did (inside --generated).

Usage:
  python3 apply_output_dir_change.py \
      /workdir/src/transaction_replay/replay_generated.py

  # or with no arg, it defaults to that path:
  python3 apply_output_dir_change.py

A timestamped .bak backup is written next to the file before editing.
The script is idempotent-ish: it refuses to run if it detects it was already
applied (looks for 'out_contract_dir' in the signature).
"""

import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_TARGET = "/workdir/src/transaction_replay/replay_generated.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    """Replace `old` with `new` exactly once; raise if 0 or >1 matches."""
    n = text.count(old)
    if n != 1:
        raise SystemExit(
            f"[FAIL] '{label}': expected exactly 1 match, found {n}. "
            f"File may already be patched or differs from the expected version."
        )
    return text.replace(old, new)


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(DEFAULT_TARGET)
    if not target.exists():
        raise SystemExit(f"[FAIL] target not found: {target}")

    src = target.read_text()

    if "out_contract_dir" in src:
        raise SystemExit(
            "[SKIP] 'out_contract_dir' already present — looks already applied. "
            "Nothing done."
        )

    # ---- backup ------------------------------------------------------------
    bak = target.with_suffix(
        target.suffix + "." + datetime.now().strftime("%Y%m%d_%H%M%S") + ".bak"
    )
    shutil.copy2(target, bak)
    print(f"[ok] backup written: {bak}")

    # ---- 1. path helpers: rename param gen_contract_dir -> out_contract_dir,
    #         and mkdir in _save_sample_ckpt --------------------------------
    src = replace_once(
        src,
        '''def _sample_ckpt_path(gen_contract_dir: Path, sample_idx: int, k: int) -> Path:
    return gen_contract_dir / f"replay_sample_{sample_idx}_checkpoint_{k}.json"


def _sample_result_path(gen_contract_dir: Path, sample_idx: int, k: int) -> Path:
    return gen_contract_dir / f"replay_sample_{sample_idx}_result_{k}.json"


def _load_sample_ckpt(gen_contract_dir: Path, sample_idx: int, k: int) -> dict:
    p = _sample_ckpt_path(gen_contract_dir, sample_idx, k)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"state": "NOT_STARTED"}


def _save_sample_ckpt(gen_contract_dir: Path, sample_idx: int, k: int,
                      data: dict) -> None:
    _sample_ckpt_path(gen_contract_dir, sample_idx, k).write_text(
        json.dumps(data, indent=2)
    )''',
        '''def _sample_ckpt_path(out_contract_dir: Path, sample_idx: int, k: int) -> Path:
    return out_contract_dir / f"replay_sample_{sample_idx}_checkpoint_{k}.json"


def _sample_result_path(out_contract_dir: Path, sample_idx: int, k: int) -> Path:
    return out_contract_dir / f"replay_sample_{sample_idx}_result_{k}.json"


def _load_sample_ckpt(out_contract_dir: Path, sample_idx: int, k: int) -> dict:
    p = _sample_ckpt_path(out_contract_dir, sample_idx, k)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"state": "NOT_STARTED"}


def _save_sample_ckpt(out_contract_dir: Path, sample_idx: int, k: int,
                      data: dict) -> None:
    out_contract_dir.mkdir(parents=True, exist_ok=True)
    _sample_ckpt_path(out_contract_dir, sample_idx, k).write_text(
        json.dumps(data, indent=2)
    )''',
        "path helpers",
    )

    # ---- 2. replay_sample signature: add out_contract_dir ------------------
    src = replace_once(
        src,
        '''    sample_path:      Path,
    dataset_dir:      Path,
    gen_contract_dir: Path,
    work_dir:         Path,''',
        '''    sample_path:      Path,
    dataset_dir:      Path,
    gen_contract_dir: Path,
    out_contract_dir: Path,
    work_dir:         Path,''',
        "replay_sample signature",
    )

    # ---- 3. inside replay_sample: every _save/_load uses out_contract_dir --
    # There are 8 call sites using gen_contract_dir for save/load inside
    # replay_sample. We replace them all at once via targeted unique strings.
    for old, new, label in [
        ("ckpt = _load_sample_ckpt(gen_contract_dir, sample_idx, k)",
         "ckpt = _load_sample_ckpt(out_contract_dir, sample_idx, k)",
         "load ckpt"),
        ('''            "reason":     "original dataset dir missing required files",
        }
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)''',
         '''            "reason":     "original dataset dir missing required files",
        }
        _save_sample_ckpt(out_contract_dir, sample_idx, k, result)''',
         "save: missing files"),
        ('''            "reason":     "proxy contract — skipped",
        }
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)''',
         '''            "reason":     "proxy contract — skipped",
        }
        _save_sample_ckpt(out_contract_dir, sample_idx, k, result)''',
         "save: proxy"),
        ('''            "reason":     "no callable transactions found",
        }
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)''',
         '''            "reason":     "no callable transactions found",
        }
        _save_sample_ckpt(out_contract_dir, sample_idx, k, result)''',
         "save: no txs"),
        ('''            "reason":     "compilation failed",
            "stderr":     err[-300:],
        }
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)''',
         '''            "reason":     "compilation failed",
            "stderr":     err[-300:],
        }
        _save_sample_ckpt(out_contract_dir, sample_idx, k, result)''',
         "save: compile fail"),
        ('''    print("✓")
    _save_sample_ckpt(gen_contract_dir, sample_idx, k,
                      {"state": "COMPILED", "address": address,
                       "sample_idx": sample_idx})''',
         '''    print("✓")
    _save_sample_ckpt(out_contract_dir, sample_idx, k,
                      {"state": "COMPILED", "address": address,
                       "sample_idx": sample_idx})''',
         "save: compiled"),
        ('''                        "reason":     f"deploy failed: {e2}",
                    }
                    _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)''',
         '''                        "reason":     f"deploy failed: {e2}",
                    }
                    _save_sample_ckpt(out_contract_dir, sample_idx, k, result)''',
         "save: deploy fail e2"),
        ('''                    "reason":     f"deploy failed: {e}",
                }
                _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)''',
         '''                    "reason":     f"deploy failed: {e}",
                }
                _save_sample_ckpt(out_contract_dir, sample_idx, k, result)''',
         "save: deploy fail e"),
        ('''        _save_sample_ckpt(gen_contract_dir, sample_idx, k, {
            "state":            "DEPLOYED",''',
         '''        _save_sample_ckpt(out_contract_dir, sample_idx, k, {
            "state":            "DEPLOYED",''',
         "save: deployed"),
        ('''        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
        _sample_result_path(gen_contract_dir, sample_idx, k).write_text(''',
         '''        _save_sample_ckpt(out_contract_dir, sample_idx, k, result)
        _sample_result_path(out_contract_dir, sample_idx, k).write_text(''',
         "save: final result"),
    ]:
        src = replace_once(src, old, new, label)

    # ---- 4. replay_contract_samples signature: add out_dir ----------------
    src = replace_once(
        src,
        '''    address:      str,
    dataset_dir:  Path,
    gen_dir:      Path,
    work_dir:     Path,
    k:            int,
    tx_timeout:   int,''',
        '''    address:      str,
    dataset_dir:  Path,
    gen_dir:      Path,
    out_dir:      Path,
    work_dir:     Path,
    k:            int,
    tx_timeout:   int,''',
        "replay_contract_samples signature",
    )

    # ---- 5. compute out_contract_dir alongside gen_contract_dir -----------
    src = replace_once(
        src,
        "    gen_contract_dir = gen_dir / address\n    compiled_indices = _compiled_sample_indices(gen_contract_dir)",
        "    gen_contract_dir = gen_dir / address\n    out_contract_dir = out_dir / address\n    compiled_indices = _compiled_sample_indices(gen_contract_dir)",
        "compute out_contract_dir",
    )

    # ---- 6. already_done uses out_contract_dir ----------------------------
    src = replace_once(
        src,
        "        if _load_sample_ckpt(gen_contract_dir, i, k).get(\"state\") == \"DONE\"",
        "        if _load_sample_ckpt(out_contract_dir, i, k).get(\"state\") == \"DONE\"",
        "already_done load",
    )

    # ---- 7. pass out_contract_dir into replay_sample ----------------------
    src = replace_once(
        src,
        '''            sample_path      = sample_path,
            dataset_dir      = dataset_dir,
            gen_contract_dir = gen_contract_dir,
            work_dir         = work_dir,''',
        '''            sample_path      = sample_path,
            dataset_dir      = dataset_dir,
            gen_contract_dir = gen_contract_dir,
            out_contract_dir = out_contract_dir,
            work_dir         = work_dir,''',
        "replay_sample call",
    )

    # ---- 8. per-contract summary written to out_contract_dir --------------
    src = replace_once(
        src,
        '''    # Write per-contract summary
    (gen_contract_dir / f"replay_contract_summary_{k}.json").write_text(
        json.dumps(summary, indent=2)
    )''',
        '''    # Write per-contract summary
    out_contract_dir.mkdir(parents=True, exist_ok=True)
    (out_contract_dir / f"replay_contract_summary_{k}.json").write_text(
        json.dumps(summary, indent=2)
    )''',
        "per-contract summary",
    )

    # ---- 9. CLI: add --output-dir -----------------------------------------
    src = replace_once(
        src,
        '''    p.add_argument(
        "--generated", type=Path, default=Path(DEFAULT_GENERATED),
        help="Generated/repaired contracts directory (output of repair_source.py).",
    )''',
        '''    p.add_argument(
        "--generated", type=Path, default=Path(DEFAULT_GENERATED),
        help="Generated/repaired contracts directory (output of repair_source.py).",
    )
    p.add_argument(
        "--output-dir", type=Path, dest="output_dir", default=None,
        help="Directory for replay outputs (<output_dir>/<address>/...). "
             "When omitted, outputs are written next to the samples "
             "(inside --generated), preserving the original behaviour.",
    )''',
        "CLI --output-dir",
    )

    # ---- 10. main(): default output_dir to generated, print it ------------
    src = replace_once(
        src,
        "def main() -> None:\n    args = parse_args()\n",
        "def main() -> None:\n    args = parse_args()\n\n"
        "    # Default: write outputs next to the samples (backward-compatible).\n"
        "    if args.output_dir is None:\n"
        "        args.output_dir = args.generated\n",
        "main output_dir default",
    )

    src = replace_once(
        src,
        "  generated       = {args.generated}\n  work_dir        = {args.work_dir}",
        "  generated       = {args.generated}\n  output_dir      = {args.output_dir}\n  work_dir        = {args.work_dir}",
        "main banner",
    )

    # ---- 11. pass out_dir into replay_contract_samples --------------------
    src = replace_once(
        src,
        '''                    address      = address,
                    dataset_dir  = args.dataset,
                    gen_dir      = args.generated,
                    work_dir     = args.work_dir,''',
        '''                    address      = address,
                    dataset_dir  = args.dataset,
                    gen_dir      = args.generated,
                    out_dir      = args.output_dir,
                    work_dir     = args.work_dir,''',
        "replay_contract_samples call",
    )

    target.write_text(src)
    print(f"[ok] patched: {target}")
    print("[ok] syntax check ...")
    import py_compile
    py_compile.compile(str(target), doraise=True)
    print("[ok] compiles cleanly. Done.")
    print()
    print("Usage now:")
    print("  python3 replay_generated.py --generated <gen> \\")
    print("      --output-dir src/transaction_replay/generated_historical_replay_results_<safe>")


if __name__ == "__main__":
    main()