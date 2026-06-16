"""
replay_generated.py
===================

Transaction replay for LLM-generated (or repaired) contract samples.

For each contract address found in the generated directory, this script:
  1. Reads result.json to identify which samples compiled successfully.
  2. For each compiled sample_i.(sol|json):
       a. Loads metadata, ABI, and transactions from the ORIGINAL dataset
          directory (ground-truth data collected during crawling).
       b. Compiles the generated sample with Hardhat.
       c. Deploys it fresh (no mainnet fork) with the same constructor
          arguments used for the original replay.
       d. Replays the first K historical transactions and records
          pass/revert outcomes.
       e. Writes replay_sample_{i}_checkpoint.json and
          replay_sample_{i}_result.json inside the generated contract dir.
  3. After all samples for a contract are done, writes a per-contract
     summary to replay_contract_summary.json in the generated contract dir.
  4. Writes an aggregated generated_replay_results.json.

Directory layout
----------------
  <dataset_dir>/                        original crawl data
    <address>/
      abi.json
      metadata.json
      transactions.json
      source.json | source.sol

  <generated_dir>/                      output of repair_source.py
    <address>/
      result.json                       compile results, per-sample
      sample_1.sol | sample_1.json
      ...
      sample_k.sol | sample_k.json
      replay_sample_1_checkpoint.json   written by this script
      replay_sample_1_result.json       written by this script
      replay_contract_summary.json      written by this script

Checkpoint behaviour
--------------------
Each (address, sample_index) pair has its own checkpoint file, keyed on
both the sample index and K.  Re-running the script skips samples whose
checkpoint is already DONE.  Use --force to override.

The original replay checkpoints (replay_checkpoint_<K>.json in the
dataset dir) are never touched by this script.

Usage
-----
  python replay_generated.py [options]

  # Default: repaired_oneshot, dataset contracts_dataset_1to20, K=1000
  python replay_generated.py

  # Custom paths:
  python replay_generated.py \\
      --dataset    ../dataset/contracts_dataset_1to20 \\
      --generated  ../source_generation/repaired_oneshot \\
      --k          1000 \\
      --work-dir   /tmp/hardhat_gen_replay \\
      --verbose

  # Retry only samples whose replay previously failed:
  python replay_generated.py --retry-failed

  # Force re-run everything:
  python replay_generated.py --force
"""

import argparse
import json
import re
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR / ".." / "utils"))

from deploy_replay import (
    DeployServer,
    _try_etherscan_args,
    _default_args,
    setup_workspace,
    SNAPSHOT_EVERY,
)
from hardhat_utils import prepare_and_compile


def _read_view_state(server, view_selectors: list) -> dict:
    """
    Snapshot zero-arg view-function state via the persistent Node.js server,
    using the fast {"snapshot": True} message. Returns {} if no view fns or on error.
    """
    if not view_selectors:
        return {}
    try:
        res = server._send({"snapshot": True}, timeout=15)
        return res.get("snapshot", {})
    except Exception:
        return {}

# Reuse helpers from replay_transactions (same directory as this script)
sys.path.insert(0, str(_SCRIPT_DIR))
from replay_transactions import (
    load_contract_data,
    filter_callable_txs,
    resolve_constructor_args,
    build_view_selectors,
)


# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

DEFAULT_DATASET   = str(_SCRIPT_DIR / ".." / "dataset" / "contracts_dataset_1to20")
DEFAULT_GENERATED = str(_SCRIPT_DIR / ".." / "source_generation" / "repaired_oneshot")
DEFAULT_WORK_DIR  = str(_SCRIPT_DIR / "hardhat_workspace_gen_replay")
DEFAULT_K         = 1000
DEFAULT_TX_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Per-sample checkpoint helpers
# ---------------------------------------------------------------------------

def _sample_ckpt_path(gen_contract_dir: Path, sample_idx: int, k: int) -> Path:
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
    )


# ---------------------------------------------------------------------------
# Sample source path resolution
# ---------------------------------------------------------------------------

def _find_sample_path(gen_contract_dir: Path, idx: int) -> Optional[Path]:
    """Return the path of sample_idx.(json|sol), or None if not found."""
    for ext in (".json", ".sol"):
        p = gen_contract_dir / f"sample_{idx}{ext}"
        if p.exists():
            return p
    return None


def _compiled_sample_indices(gen_contract_dir: Path) -> list[int]:
    """
    Read result.json in the generated contract directory and return the
    1-based indices of samples that compiled successfully.
    Falls back to scanning for sample files when result.json is missing.
    """
    result_path = gen_contract_dir / "result.json"
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text())
            compiled = [
                s["idx"]
                for s in result.get("samples", [])
                if s.get("compiled", False)
            ]
            if compiled:
                return sorted(compiled)
        except Exception:
            pass

    # Fallback: any sample file that exists is considered a candidate.
    # (Used when result.json is absent or malformed.)
    indices = set()
    for ext in (".sol", ".json"):
        for p in gen_contract_dir.glob(f"sample_*{ext}"):
            m = re.match(r"sample_(\d+)", p.stem)
            if m:
                indices.add(int(m.group(1)))
    return sorted(indices)


# ---------------------------------------------------------------------------
# Single-sample replay
# ---------------------------------------------------------------------------

def replay_sample(
    address:          str,
    sample_idx:       int,
    sample_path:      Path,
    dataset_dir:      Path,
    gen_contract_dir: Path,
    work_dir:         Path,
    k:                int,
    tx_timeout:       int,
    force:            bool = False,
    retry_failed:     bool = False,
    verbose:          bool = False,
) -> dict:
    """
    Compile, deploy, and replay up to K transactions for a single generated
    sample.  Mirrors the logic of replay_transactions.replay_contract() but
    uses the generated source file while pulling metadata/ABI/transactions
    from the original dataset directory.

    Returns a result dict.
    """
    # ── Checkpoint check ──────────────────────────────────────────────────
    ckpt = _load_sample_ckpt(gen_contract_dir, sample_idx, k)
    state = ckpt.get("state")
    if state in ("DONE", "FAILED"):
        if force or (retry_failed and state == "FAILED"):
            pass  # re-run
        else:
            return ckpt

    # ── Load ground-truth data (metadata, ABI, transactions) ──────────────
    original_dir = dataset_dir / address
    data = load_contract_data(original_dir)
    if data is None:
        result = {
            "state":      "FAILED",
            "address":    address,
            "sample_idx": sample_idx,
            "reason":     "original dataset dir missing required files",
        }
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
        return result

    meta          = data["meta"]
    abi           = data["abi"]
    source_data   = data["source_data"]
    transactions  = data["transactions"]
    receipts      = data.get("receipts") or {}
    contract_name = meta.get("contract_name", "Contract")

    if meta.get("is_proxy"):
        result = {
            "state":      "FAILED",
            "address":    address,
            "sample_idx": sample_idx,
            "reason":     "proxy contract — skipped",
        }
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
        return result

    # ── Filter transactions ────────────────────────────────────────────────
    deploy_block  = meta.get("deploy_block")
    callable_txs  = filter_callable_txs(transactions, address, deploy_block)
    txs_to_replay = callable_txs[:k]

    if not txs_to_replay:
        result = {
            "state":      "FAILED",
            "address":    address,
            "sample_idx": sample_idx,
            "reason":     "no callable transactions found",
        }
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
        return result

    has_receipts   = bool(receipts)
    view_selectors = build_view_selectors(abi)

    # Wipe stale Hardhat artifacts and cache before each compile to prevent
    # HH700 errors and stale-bytecode reuse across different sample sources.
    for stale in ("artifacts", "cache"):
        stale_path = work_dir / stale
        if stale_path.exists():
            shutil.rmtree(stale_path)

    # ── Step 1: Compile the generated sample ──────────────────────────────
    print(f"    sample {sample_idx}: compiling ...", end="  ", flush=True)
    ok, err = prepare_and_compile(sample_path, meta, work_dir)
    if not ok:
        print("✗")
        if verbose:
            print(f"      Compiler error:\n{err[-400:]}")
        result = {
            "state":      "FAILED",
            "address":    address,
            "sample_idx": sample_idx,
            "reason":     "compilation failed",
            "stderr":     err[-300:],
        }
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
        return result
    print("✓")
    _save_sample_ckpt(gen_contract_dir, sample_idx, k,
                      {"state": "COMPILED", "address": address,
                       "sample_idx": sample_idx})

    # ── Step 2: Resolve constructor arguments ─────────────────────────────
    constructor_args = resolve_constructor_args(
        meta, source_data, abi, sample_path, verbose=verbose
    )

    # Build FQN for Hardhat's getContractFactory
    if sample_path.suffix == ".sol":
        fqn = f"contracts/{sample_path.name}:{contract_name}"
    else:
        try:
            sources_map = json.loads(sample_path.read_text()).get("sources", {})
            first_file  = next(iter(sources_map), None)
            fqn = f"contracts/{first_file}:{contract_name}" if first_file else contract_name
        except Exception:
            fqn = contract_name

    # ── Step 3: Deploy ────────────────────────────────────────────────────
    server = DeployServer(work_dir, tx_timeout=tx_timeout)
    try:
        print(f"    sample {sample_idx}: deploying {fqn} ...", end="  ", flush=True)
        try:
            deployed_address = server.deploy(fqn, constructor_args,
                                             view_selectors=view_selectors)
        except RuntimeError as e:
            print(f"✗  {e}")
            # One retry with zero defaults
            ctor_entry = next((e for e in abi if e.get("type") == "constructor"), None)
            fallback   = _default_args(ctor_entry.get("inputs", []) if ctor_entry else [])
            if fallback != constructor_args:
                print(f"    sample {sample_idx}: retrying with defaults ...",
                      end="  ", flush=True)
                try:
                    deployed_address = server.deploy(fqn, fallback,
                                                     view_selectors=view_selectors)
                    constructor_args = fallback
                except RuntimeError as e2:
                    print(f"✗  {e2}")
                    server.close()
                    result = {
                        "state":      "FAILED",
                        "address":    address,
                        "sample_idx": sample_idx,
                        "reason":     f"deploy failed: {e2}",
                    }
                    _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
                    return result
            else:
                server.close()
                result = {
                    "state":      "FAILED",
                    "address":    address,
                    "sample_idx": sample_idx,
                    "reason":     f"deploy failed: {e}",
                }
                _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
                return result

        print(f"✓  {deployed_address[:14]}…")
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, {
            "state":            "DEPLOYED",
            "address":          address,
            "sample_idx":       sample_idx,
            "deployed_address": deployed_address,
        })

        # ── Step 4: Replay ─────────────────────────────────────────────────
        tx_results = []
        passed = 0
        failed = 0

        for i, tx in enumerate(txs_to_replay, 1):
            tx_hash         = tx.get("hash", "")
            hist_receipt    = receipts.get(tx_hash)
            historical_logs = hist_receipt.get("logs") if hist_receipt else None

            try:
                res   = server.replay_tx(tx, historical_logs=historical_logs)
                match = res.get("events_match", False)
                # Fast state snapshot every SNAPSHOT_EVERY txs (and at the end),
                # mirroring the synthetic replay scripts for state-divergence analysis.
                if i % SNAPSHOT_EVERY == 0 or i == len(txs_to_replay):
                    res["_view_state"] = _read_view_state(server, view_selectors)
                if match:
                    passed += 1
                else:
                    failed += 1
                    if verbose:
                        reason = (res.get("revert_reason") or res.get("error") or "")[:80]
                        if reason:
                            print(f"      tx {i} mismatch: {reason}")
                tx_results.append(res)
            except TimeoutError:
                failed += 1
                tx_results.append({"hash": tx_hash,
                                   "actual_status": "timeout",
                                   "events_match":  False})
            except Exception as e:
                failed += 1
                tx_results.append({"hash": tx_hash,
                                   "actual_status": "error",
                                   "events_match":  False,
                                   "error":         str(e)})

            if i % 100 == 0 or i == len(txs_to_replay):
                print(f"      [{i:>5}/{len(txs_to_replay)}]  "
                      f"pass={passed}  fail={failed}  rate={passed/i:.1%}")

        # ── Step 5: Save ───────────────────────────────────────────────────
        total     = len(txs_to_replay)
        pass_rate = round(passed / total, 4) if total else None

        # Aggregate new metrics from tx_results
        gas_ratios    = [r["gas_ratio"]       for r in tx_results
                         if r.get("gas_ratio") is not None]
        log_matches   = [r["log_count_match"] for r in tx_results
                         if r.get("log_count_match") is not None]
        topic_matches = [r["topics_match"]    for r in tx_results
                         if r.get("topics_match") is not None]

        result = {
            "state":            "DONE",
            "address":          address,
            "sample_idx":       sample_idx,
            "contract_name":    contract_name,
            "deployed_address": deployed_address,
            "constructor_args": constructor_args,
            "k":                total,
            "passed":           passed,
            "failed":           failed,
            "pass_rate":        pass_rate,
            "has_receipts":     has_receipts,
            "mean_gas_ratio":   round(sum(gas_ratios) / len(gas_ratios), 4)
                                if gas_ratios else None,
            "log_count_match_rate": round(sum(log_matches) / len(log_matches), 4)
                                    if log_matches else None,
            "topics_match_rate":    round(sum(topic_matches) / len(topic_matches), 4)
                                    if topic_matches else None,
        }
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
        _sample_result_path(gen_contract_dir, sample_idx, k).write_text(
            json.dumps({
                **result,
                "timestamp":  datetime.utcnow().isoformat(),
                "tx_results": tx_results,
            }, indent=2)
        )

        rate_str = f"{pass_rate:.1%}" if pass_rate is not None else "n/a"
        print(f"    sample {sample_idx}: pass_rate={rate_str}  ({passed}/{total})")
        return result

    finally:
        server.close()


# ---------------------------------------------------------------------------
# Per-contract orchestration
# ---------------------------------------------------------------------------

def replay_contract_samples(
    address:      str,
    dataset_dir:  Path,
    gen_dir:      Path,
    work_dir:     Path,
    k:            int,
    tx_timeout:   int,
    force:        bool = False,
    retry_failed: bool = False,
    verbose:      bool = False,
) -> dict:
    """
    Run replay for all compiled samples of one contract.
    Returns a summary dict with per-sample results and aggregate stats.
    """
    gen_contract_dir = gen_dir / address
    compiled_indices = _compiled_sample_indices(gen_contract_dir)

    if not compiled_indices:
        return {
            "address":  address,
            "skipped":  True,
            "reason":   "no compiled samples found in result.json",
            "samples":  [],
        }

    # Check how many samples were already done (for progress display)
    already_done = sum(
        1 for i in compiled_indices
        if _load_sample_ckpt(gen_contract_dir, i, k).get("state") == "DONE"
    )
    n = len(compiled_indices)

    meta_path = dataset_dir / address / "metadata.json"
    contract_name = address[:12] + "…"
    if meta_path.exists():
        try:
            contract_name = json.loads(meta_path.read_text()).get(
                "contract_name", contract_name
            )
        except Exception:
            pass

    print(f"  {contract_name} ({address[:12]}…)  "
          f"compiled_samples={n}  already_done={already_done}")

    sample_results = []
    for sample_idx in compiled_indices:
        sample_path = _find_sample_path(gen_contract_dir, sample_idx)
        if sample_path is None:
            print(f"    sample {sample_idx}: file not found — skipping")
            sample_results.append({
                "sample_idx": sample_idx,
                "state":      "FAILED",
                "reason":     "sample file not found on disk",
            })
            continue

        result = replay_sample(
            address          = address,
            sample_idx       = sample_idx,
            sample_path      = sample_path,
            dataset_dir      = dataset_dir,
            gen_contract_dir = gen_contract_dir,
            work_dir         = work_dir,
            k                = k,
            tx_timeout       = tx_timeout,
            force            = force,
            retry_failed     = retry_failed,
            verbose          = verbose,
        )
        sample_results.append(result)

    # Aggregate stats across samples
    done_results  = [r for r in sample_results if r.get("state") == "DONE"]
    rates         = [r["pass_rate"] for r in done_results
                     if r.get("pass_rate") is not None]

    def _mean(vals): return round(sum(vals) / len(vals), 4) if vals else None

    gas_ratios    = [r["mean_gas_ratio"]        for r in done_results
                     if r.get("mean_gas_ratio") is not None]
    log_rates     = [r["log_count_match_rate"]  for r in done_results
                     if r.get("log_count_match_rate") is not None]
    topic_rates   = [r["topics_match_rate"]     for r in done_results
                     if r.get("topics_match_rate") is not None]

    summary = {
        "address":              address,
        "contract_name":        contract_name,
        "compiled_samples":     n,
        "replayed":             len(done_results),
        "failed_replay":        len(sample_results) - len(done_results),
        "pass_rates":           rates,
        "mean_pass_rate":       _mean(rates),
        "min_pass_rate":        round(min(rates), 4) if rates else None,
        "max_pass_rate":        round(max(rates), 4) if rates else None,
        "mean_gas_ratio":       _mean(gas_ratios),
        "log_count_match_rate": _mean(log_rates),
        "topics_match_rate":    _mean(topic_rates),
        "has_receipts":         any(r.get("has_receipts") for r in done_results),
        "samples":              sample_results,
    }

    # Write per-contract summary
    (gen_contract_dir / f"replay_contract_summary_{k}.json").write_text(
        json.dumps(summary, indent=2)
    )
    return summary


# ---------------------------------------------------------------------------
# Summary / printing
# ---------------------------------------------------------------------------

def _format_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def print_summary(all_summaries: list[dict], k: int) -> None:
    replayed    = [s for s in all_summaries if s.get("replayed", 0) > 0]
    skipped     = [s for s in all_summaries if s.get("skipped")]
    all_rates   = [r for s in replayed for r in s.get("pass_rates", [])]
    mean_rates  = [s["mean_pass_rate"] for s in replayed
                   if s.get("mean_pass_rate") is not None]

    print("\n" + "═" * 60)
    print(f"  GENERATED REPLAY SUMMARY  (K={k})")
    print("═" * 60)
    print(f"  Contracts processed     : {len(all_summaries)}")
    print(f"  Contracts with replays  : {len(replayed)}")
    print(f"  Contracts skipped       : {len(skipped)}")
    total_samples  = sum(s.get("compiled_samples", 0) for s in all_summaries)
    total_replayed = sum(s.get("replayed", 0) for s in all_summaries)
    total_failed   = sum(s.get("failed_replay", 0) for s in all_summaries)
    print(f"  Total compiled samples  : {total_samples}")
    print(f"  Successfully replayed   : {total_replayed}")
    print(f"  Replay failed           : {total_failed}")
    if all_rates:
        print(f"  Avg pass rate (samples) : {sum(all_rates)/len(all_rates):.1%}")
        print(f"  Min / Max (samples)     : {min(all_rates):.1%} / {max(all_rates):.1%}")
    if mean_rates:
        print(f"  Avg mean pass rate      : {sum(mean_rates)/len(mean_rates):.1%}")
    print("═" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Replay historical transactions against freshly deployed "
            "LLM-generated contract samples."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--dataset", type=Path, default=Path(DEFAULT_DATASET),
        help="Original dataset directory (metadata, ABI, transactions).",
    )
    p.add_argument(
        "--generated", type=Path, default=Path(DEFAULT_GENERATED),
        help="Generated/repaired contracts directory (output of repair_source.py).",
    )
    p.add_argument(
        "--work-dir", type=Path, default=Path(DEFAULT_WORK_DIR),
        help="Hardhat workspace (created automatically).",
    )
    p.add_argument(
        "--k", type=int, default=DEFAULT_K,
        help="Number of transactions to replay per sample.",
    )
    p.add_argument(
        "--tx-timeout", type=int, default=DEFAULT_TX_TIMEOUT,
        help="Seconds to wait for a single transaction before timeout.",
    )
    p.add_argument(
        "--addresses", nargs="*", metavar="ADDR",
        help="Process only these contract addresses. Default: all.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Re-run even if a DONE checkpoint exists.",
    )
    p.add_argument(
        "--retry-failed", action="store_true", dest="retry_failed",
        help="Re-run only samples whose checkpoint is FAILED; skip DONE ones.",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print compiler errors, per-tx mismatch reasons, and constructor args.",
    )
    p.add_argument(
        "--results-file", type=Path,
        default=_SCRIPT_DIR / "generated_replay_results.json",
        help="Path for the aggregated output JSON.",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    print(f"""
replay_generated.py
══════════════════════════════════════════════════════════
  dataset         = {args.dataset}
  generated       = {args.generated}
  work_dir        = {args.work_dir}
  k               = {args.k}
  tx_timeout      = {args.tx_timeout}s
  force           = {args.force}
  retry_failed    = {args.retry_failed}
  verbose         = {args.verbose}
  results_file    = {args.results_file}
══════════════════════════════════════════════════════════
""")

    for d, label in [(args.dataset, "--dataset"), (args.generated, "--generated")]:
        if not d.exists():
            print(f"ERROR: {label} directory not found: {d}")
            sys.exit(1)

    # ── Discover contract directories in the generated folder ─────────────
    if args.addresses:
        requested    = {a.lower() for a in args.addresses}
        gen_dirs     = sorted([
            d for d in args.generated.iterdir()
            if d.is_dir() and d.name.lower() in requested
        ])
        missing = requested - {d.name.lower() for d in gen_dirs}
        if missing:
            print(f"WARNING: {len(missing)} address(es) not found in generated dir:")
            for m in sorted(missing):
                print(f"  {m}")
    else:
        # Only directories that have at least one sample file
        gen_dirs = sorted([
            d for d in args.generated.iterdir()
            if d.is_dir() and any(
                re.match(r"sample_\d+\.(sol|json)$", f.name)
                for f in d.iterdir()
            )
        ])

    if not gen_dirs:
        print("No generated contract directories found. Exiting.")
        sys.exit(0)

    print(f"Found {len(gen_dirs)} contract(s) with generated samples.\n")

    # ── Setup Hardhat workspace ───────────────────────────────────────────
    setup_workspace(args.work_dir)

    # ── Main loop ─────────────────────────────────────────────────────────
    all_summaries: list[dict] = []
    start_time = time.time()

    try:
        for i, gen_contract_dir in enumerate(gen_dirs, 1):
            address = gen_contract_dir.name
            print(f"\n[{i}/{len(gen_dirs)}] {address}")

            # Skip if the original dataset entry doesn't exist
            if not (args.dataset / address).exists():
                print(f"  WARNING: no entry in dataset for {address} — skipping")
                all_summaries.append({
                    "address": address,
                    "skipped": True,
                    "reason":  "address not found in dataset directory",
                    "samples": [],
                })
                continue

            try:
                summary = replay_contract_samples(
                    address      = address,
                    dataset_dir  = args.dataset,
                    gen_dir      = args.generated,
                    work_dir     = args.work_dir,
                    k            = args.k,
                    tx_timeout   = args.tx_timeout,
                    force        = args.force,
                    retry_failed = args.retry_failed,
                    verbose      = args.verbose,
                )
                all_summaries.append(summary)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  Unexpected error: {e}")
                all_summaries.append({
                    "address": address,
                    "skipped": True,
                    "reason":  f"unexpected error: {e}",
                    "samples": [],
                })

    except KeyboardInterrupt:
        print("\nInterrupted — saving partial results ...")

    # ── Write aggregated results ──────────────────────────────────────────
    elapsed = time.time() - start_time
    output = {
        "timestamp":        datetime.utcnow().isoformat(),
        "duration_seconds": round(elapsed, 1),
        "config": {
            "k":           args.k,
            "tx_timeout":  args.tx_timeout,
            "dataset":     str(args.dataset),
            "generated":   str(args.generated),
        },
        "total_contracts":   len(all_summaries),
        "contracts_replayed": sum(1 for s in all_summaries
                                  if s.get("replayed", 0) > 0),
        "contracts_skipped":  sum(1 for s in all_summaries if s.get("skipped")),
        "total_samples_replayed": sum(s.get("replayed", 0) for s in all_summaries),
        "contracts": all_summaries,
    }
    args.results_file.write_text(json.dumps(output, indent=2))
    print(f"\nResults written to {args.results_file}")

    print_summary(all_summaries, args.k)
    print(f"\n  Total time: {_format_elapsed(elapsed)}")


if __name__ == "__main__":
    main()