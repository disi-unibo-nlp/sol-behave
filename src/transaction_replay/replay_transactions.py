"""
replay_transactions.py
======================

Standalone script for historical transaction replay on smart contracts
already present in the dataset directory.

For each contract folder in the dataset, this script:
  1. Loads the pre-existing source, ABI, metadata, and transactions from disk
     (no Etherscan calls required — all data was collected during crawling).
  2. Compiles the contract from source on a local Hardhat instance.
  3. Deploys a fresh copy (no mainnet fork injection) with constructor
     arguments resolved in priority order:
       a. Etherscan ConstructorArguments field (most reliable)
       b. Deploy transaction input decoding
       c. Deterministic defaults (owner-like params → Hardhat account #0)
  4. Replays the first K transactions (default 1000) against the fresh
     deployment, comparing actual success/revert with the historical outcome.
  5. Writes results to <contract_dir>/replay_result_<K>.json and updates
     <contract_dir>/replay_checkpoint_<K>.json.

Checkpoint behaviour
--------------------
Each contract has its own checkpoint file keyed on K, so different replay
runs with different K values are independent. States:
  NOT_STARTED → COMPILED → DEPLOYED → DONE | FAILED

If a run is interrupted mid-replay, resuming re-starts from COMPILED
(fresh compile + fresh deploy + replay from tx 0). This is correct because
the Hardhat in-memory chain does not survive process restarts.

Directory layout expected
-------------------------
  <dataset_dir>/
    <address>/
      abi.json           ABI array
      metadata.json      compiler version, optimizer settings, etc.
      source.json        Standard JSON input  (OR source.sol for flat files)
      transactions.json  raw Etherscan txlist response
      checkpoint.json    checkpoint from the original crawl (not modified)

Usage
-----
  python replay_transactions.py [options]

  # Replay first 1000 txs for every contract, 50% pass threshold:
  python replay_transactions.py

  # Custom dataset path, 500 txs, 80% threshold, verbose:
  python3 replay_transactions.py \\
      --dataset ../dataset/contracts_dataset_1to20 \\
      --k 1000 \\
      --pass-threshold 0.50 \\
      --tx-timeout 60 \\
      --work-dir /tmp/hardhat_replay \\
      --verbose

  # Replay only specific contracts:
  python replay_transactions.py \\
      --addresses 0xabc...  0xdef... \\
      --k 1000

  # Force re-run even if a DONE checkpoint exists:
  python replay_transactions.py --force

  python3 replay_transactions.py --retry-failed --verbose

  # empty artifacts and cache:

  rm -rf ../dataset/hardhat_workspace_replay/artifacts ../dataset/hardhat_workspace_replay/cache

Environment variables
---------------------
  No Etherscan / Alchemy keys required — all data is read from disk.
"""

import argparse
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path bootstrap: make ../utils/ importable when running from ../transaction_replay/
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR / ".." / "utils"))

from deploy_replay import (
    DeployServer,
    _try_etherscan_args,
    _extract_args_from_tx,
    _default_args,
    synthesize_constructor_args,
    _load_ckpt,
    _save_ckpt,
    setup_workspace,        # writes deploy_server.js in addition to npm install
    SNAPSHOT_EVERY,
)


def _read_view_state(server, view_selectors: list) -> dict:
    """
    Snapshot zero-arg view-function state via the persistent Node.js server,
    using the fast {"snapshot": True} message (the server already holds the
    view selectors passed at deploy time). Returns {} if no view fns or on error.
    """
    if not view_selectors:
        return {}
    try:
        res = server._send({"snapshot": True}, timeout=15)
        return res.get("snapshot", {})
    except Exception:
        return {}
from hardhat_utils import (
    prepare_and_compile,
    load_source_texts,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DATASET = str(_SCRIPT_DIR / ".." / "dataset" / "contracts_dataset_1to20")
DEFAULT_WORK_DIR = str(_SCRIPT_DIR / "hardhat_workspace_replay_comp_failures")
DEFAULT_K = 1000
DEFAULT_PASS_THRESHOLD = 0.50
DEFAULT_TX_TIMEOUT = 60

HARDHAT_OWNER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


# ---------------------------------------------------------------------------
# Checkpoint helpers (K-aware — keyed on the replay size)
# ---------------------------------------------------------------------------

def _ckpt_path_k(contract_dir: Path, k: int) -> Path:
    return contract_dir / f"replay_checkpoint_{k}.json"


def _result_path_k(contract_dir: Path, k: int) -> Path:
    return contract_dir / f"replay_result_{k}.json"


def load_ckpt_k(contract_dir: Path, k: int) -> dict:
    """Load the K-specific checkpoint, or return {'state': 'NOT_STARTED'}."""
    p = _ckpt_path_k(contract_dir, k)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"state": "NOT_STARTED"}


def save_ckpt_k(contract_dir: Path, k: int, data: dict) -> None:
    _ckpt_path_k(contract_dir, k).write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Dataset loading helpers
# ---------------------------------------------------------------------------

def load_contract_data(contract_dir: Path) -> Optional[dict]:
    """
    Load all pre-existing data for a contract from disk.

    Returns a dict with keys:
      address, meta, abi, source_path, source_data, transactions
    Returns None if any required file is missing or unreadable.
    """
    address = contract_dir.name

    # metadata.json
    meta_path = contract_dir / "metadata.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return None

    # abi.json
    abi_path = contract_dir / "abi.json"
    abi: list = []
    if abi_path.exists():
        try:
            abi = json.loads(abi_path.read_text())
        except Exception:
            pass

    # source — prefer source.json (Standard JSON), fall back to source.sol
    source_path = contract_dir / "source.json"
    if not source_path.exists():
        source_path = contract_dir / "source.sol"
    if not source_path.exists():
        return None  # No source available

    # Build a minimal source_data dict compatible with _try_etherscan_args()
    # (that function reads the "ConstructorArguments" key from the Etherscan
    # getsourcecode response, which we stored in metadata.json during crawling)
    source_data = {
        "ConstructorArguments": meta.get("constructor_arguments", ""),
        "ContractName":         meta.get("contract_name", ""),
        "CompilerVersion":      meta.get("compiler_version", ""),
        "OptimizationUsed":     "1" if meta.get("optimization_used") else "0",
        "Runs":                 str(meta.get("runs", 200)),
        "EVMVersion":           meta.get("evm_version", "default"),
        "Proxy":                "1" if meta.get("is_proxy") else "0",
    }

    # transactions.json
    txs_path = contract_dir / "transactions.json"
    if not txs_path.exists():
        return None
    try:
        transactions = json.loads(txs_path.read_text())
    except Exception:
        return None

    return {
        "address":      address,
        "meta":         meta,
        "abi":          abi,
        "source_path":  source_path,
        "source_data":  source_data,
        "transactions": transactions,
        # Optional: historical receipts for log/event comparison.
        # Populated by fetch_historical_receipts.py; None when absent.
        "receipts":     _load_receipts(contract_dir),
    }


def _load_receipts(contract_dir: Path) -> Optional[dict]:
    """Load receipts.json if present; returns None otherwise."""
    p = contract_dir / "receipts.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _keccak256_selector(name: str) -> str:
    """Compute the 4-byte function selector for a zero-argument function."""
    from Crypto.Hash import keccak
    k = keccak.new(digest_bits=256)
    k.update(f"{name}()".encode())
    return k.hexdigest()[:8]


def build_view_selectors(abi: list) -> list[dict]:
    """
    Build a list of zero-argument view/pure functions for state snapshots.

    Returns a list of dicts with keys:
      name         function name
      selector     4-byte hex selector (no 0x prefix)
      outputTypes  list of output type strings for ABI decoding

    Only functions with zero inputs are included — calling a view function
    with arguments would require knowing what to pass, which is not feasible
    in a generic replay context.
    """
    result = []
    seen: set[str] = set()
    for entry in abi:
        if entry.get("type") != "function":
            continue
        if entry.get("stateMutability") not in ("view", "pure"):
            continue
        if entry.get("inputs"):   # skip functions that require arguments
            continue
        name = entry.get("name", "")
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            sel          = _keccak256_selector(name)
            output_types = [o.get("type", "") for o in entry.get("outputs", [])]
            result.append({"name": name, "selector": sel,
                           "outputTypes": output_types})
        except Exception:
            pass
    return result


def filter_callable_txs(txs: list, address: str,
                        deploy_block: Optional[int]) -> list:
    """
    Keep only transactions that are:
      - sent TO the contract (not contract creation)
      - carry calldata (input != "0x" / "")
      - did not error out at the Etherscan level (isError != "1")
      - happened at or after the deploy block (if known)
    """
    addr_lower = address.lower()
    result = []
    for tx in txs:
        if (tx.get("to") or "").lower() != addr_lower:
            continue
        if tx.get("input", "0x") in ("0x", ""):
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
    return result


# ---------------------------------------------------------------------------
# Constructor argument resolution (no LLM)
# ---------------------------------------------------------------------------

def resolve_constructor_args(
    meta: dict,
    source_data: dict,
    abi: list,
    source_path: Path,
    verbose: bool = False,
) -> list:
    """
    Resolve constructor arguments using three strategies in order:

    1. Etherscan ConstructorArguments (ABI-decoded hex from metadata)
    2. Deploy transaction input decoding (reads deploy block from metadata)
    3. Deterministic defaults (owner-like addresses → Hardhat account #0)

    No LLM is involved: this script is designed to be fully reproducible
    without external API calls beyond what was already cached on disk.
    """
    # Strategy 1: Etherscan ConstructorArguments
    args = _try_etherscan_args(source_data, abi)
    if args is not None:
        if verbose:
            print(f"    Constructor args (Etherscan): {args}")
        return args

    # Strategy 2: Deploy transaction input decoding
    deploy_block = meta.get("deploy_block")
    if deploy_block:
        try:
            from etherscan_utils import _get_deploy_tx
            tx = _get_deploy_tx(meta["address"], deploy_block)
            if tx:
                args = _extract_args_from_tx(tx.get("input"), abi)
                if args is not None:
                    if verbose:
                        print(f"    Constructor args (tx decode): {args}")
                    return args
        except Exception as e:
            if verbose:
                print(f"    [warn] deploy tx decode failed: {e}")

    # Strategy 3: Deterministic defaults
    ctor = next((e for e in abi if e.get("type") == "constructor"), None)
    inputs = ctor.get("inputs", []) if ctor else []
    args = _default_args(inputs)
    if verbose and inputs:
        print(f"    Constructor args (defaults): {args}")
    return args

# ---------------------------------------------------------------------------
# Additional helpers to check functional coverage
# ---------------------------------------------------------------------------

def build_fn_selector_map(abi: list) -> dict[str, str]:
    """Map 4-byte hex selector → function name for all state-changing functions."""
    from eth_hash.auto import keccak
    result = {}
    for e in abi:
        if e.get("type") != "function":
            continue
        if e.get("stateMutability") in ("view", "pure"):
            continue
        name   = e.get("name", "")
        inputs = e.get("inputs", [])
        sig    = name + "(" + ",".join(i["type"] for i in inputs) + ")"
        sel    = keccak(sig.encode())[:4].hex()
        result[sel] = sig
    return result


# ---------------------------------------------------------------------------
# Core replay logic for a single contract
# ---------------------------------------------------------------------------


def replay_contract(
    contract_dir: Path,
    work_dir: Path,
    k: int,
    pass_threshold: float,
    tx_timeout: int,
    force: bool = False,
    retry_failed: bool = False,
    verbose: bool = False,
) -> dict:
    """
    Compile, deploy, and replay up to K transactions for a single contract.

    Returns a result dict with keys:
      state, address, contract_name, k, passed, failed, pass_rate, saved, ...
    """
    address = contract_dir.name

    # ── Check existing checkpoint ─────────────────────────────────────────
    ckpt = load_ckpt_k(contract_dir, k)
    state = ckpt.get("state")
    if state in ("DONE", "FAILED"):
        # --force        : re-run everything
        # --retry-failed : re-run only FAILED, skip DONE
        # (default)      : skip both
        if force or (retry_failed and state == "FAILED"):
            pass  # fall through and re-run
        else:
            if verbose:
                print(f"  Already {state} (checkpoint found) — skipping")
            return ckpt

    # ── Load data from disk ───────────────────────────────────────────────
    data = load_contract_data(contract_dir)
    if data is None:
        result = {
            "state":   "FAILED",
            "address": address,
            "reason":  "missing required files (metadata/source/transactions)",
        }
        save_ckpt_k(contract_dir, k, result)
        return result

    meta          = data["meta"]
    abi           = data["abi"]
    source_path   = data["source_path"]
    source_data   = data["source_data"]
    transactions  = data["transactions"]
    receipts      = data.get("receipts") or {}   # may be None if file absent
    contract_name = meta.get("contract_name", "Contract")

    # Skip proxies — they require the implementation contract to be deployed
    # separately, which is outside the scope of this standalone script.
    if meta.get("is_proxy"):
        result = {
            "state":   "FAILED",
            "address": address,
            "reason":  "proxy contract — skipped",
        }
        save_ckpt_k(contract_dir, k, result)
        return result

    # ── Filter callable transactions ──────────────────────────────────────
    deploy_block = meta.get("deploy_block")
    callable_txs = filter_callable_txs(transactions, address, deploy_block)
    txs_to_replay = callable_txs[:k]

    if not txs_to_replay:
        result = {
            "state":   "FAILED",
            "address": address,
            "reason":  "no callable transactions found",
        }
        save_ckpt_k(contract_dir, k, result)
        return result

    has_receipts   = bool(receipts)
    view_selectors = build_view_selectors(abi)
    fn_selector_map = build_fn_selector_map(abi)

    print(f"  {contract_name} ({address[:12]}…)  |  "
          f"callable={len(callable_txs)}  replaying={len(txs_to_replay)}"
          f"  receipts={'yes' if has_receipts else 'no'}"
          f"  view_fns={len(view_selectors)}")

    # ── Step 1: Compile ───────────────────────────────────────────────────
    print(f"  Compiling ...", end="  ", flush=True)
    ok, err = prepare_and_compile(source_path, meta, work_dir)
    if not ok:
        print("✗")
        if verbose:
            #print(f"    Compiler error:\n{err[-600:]}")
            print(f"    Compiler error:\n{err[:1500]}")
        result = {
            "state":   "FAILED",
            "address": address,
            "reason":  "compilation failed",
            "stderr":  err[-500:],
        }
        save_ckpt_k(contract_dir, k, result)
        return result
    print("✓")
    save_ckpt_k(contract_dir, k, {"state": "COMPILED", "address": address})

    # ── Step 2: Resolve constructor arguments ─────────────────────────────
    constructor_args = resolve_constructor_args(
        meta, source_data, abi, source_path, verbose=verbose
    )
    if not constructor_args:
        print(f"  Constructor: no args")

    # Build the fully-qualified contract name for Hardhat's getContractFactory
    if source_path.suffix == ".sol":
        fqn = f"contracts/{source_path.name}:{contract_name}"
    else:
        sources_map = json.loads(source_path.read_text()).get("sources", {})
        first_file = next(iter(sources_map), None)
        fqn = f"contracts/{first_file}:{contract_name}" if first_file else contract_name

    # ── Step 3: Deploy ────────────────────────────────────────────────────
    server = DeployServer(work_dir, tx_timeout=tx_timeout)
    try:
        print(f"  Deploying {fqn} ...", end="  ", flush=True)
        try:
            deployed_address = server.deploy(fqn, constructor_args,
                                             view_selectors=view_selectors)
        except RuntimeError as e:
            print(f"✗  {e}")
            # One retry with zero defaults
            ctor_entry = next((e for e in abi if e.get("type") == "constructor"), None)
            fallback = _default_args(ctor_entry.get("inputs", []) if ctor_entry else [])
            if fallback != constructor_args:
                print(f"  Retrying with zero defaults ...", end="  ", flush=True)
                try:
                    deployed_address = server.deploy(fqn, fallback,
                                                     view_selectors=view_selectors)
                    constructor_args = fallback
                except RuntimeError as e2:
                    print(f"✗  {e2}")
                    server.close()
                    result = {
                        "state":   "FAILED",
                        "address": address,
                        "reason":  f"deploy failed: {e2}",
                    }
                    save_ckpt_k(contract_dir, k, result)
                    return result
            else:
                server.close()
                result = {
                    "state":   "FAILED",
                    "address": address,
                    "reason":  f"deploy failed: {e}",
                }
                save_ckpt_k(contract_dir, k, result)
                return result

        print(f"✓  {deployed_address[:14]}…")
        save_ckpt_k(contract_dir, k, {
            "state":            "DEPLOYED",
            "address":          address,
            "deployed_address": deployed_address,
            "constructor_args": constructor_args,
        })

        # ── Step 4: Replay transactions ───────────────────────────────────
        print(f"  Replaying {len(txs_to_replay)} transactions ...")
        tx_results = []
        passed = 0
        failed = 0

        for i, tx in enumerate(txs_to_replay, 1):
            # Look up historical logs for this tx from receipts.json
            tx_hash = tx.get("hash", "")
            historical_receipt = receipts.get(tx_hash)
            historical_logs    = historical_receipt.get("logs") if historical_receipt else None

            try:
                res = server.replay_tx(tx, historical_logs=historical_logs)
                res["_fn_sig"] = fn_selector_map.get(
                    (tx.get("input") or "0x")[2:10].lower(), ""
                )
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
                        reason = (res.get("revert_reason") or res.get("error") or "")[:100]
                        if reason:
                            print(f"    tx {i} mismatch: {reason}")
                tx_results.append(res)
            except TimeoutError:
                failed += 1
                tx_results.append({
                    "hash":          tx_hash,
                    "actual_status": "timeout",
                    "events_match":  False,
                })
            except Exception as e:
                failed += 1
                tx_results.append({
                    "hash":          tx_hash,
                    "actual_status": "error",
                    "events_match":  False,
                    "error":         str(e),
                })

            # Progress line every 50 transactions and at the end
            if i % 50 == 0 or i == len(txs_to_replay):
                rate = passed / i
                print(f"    [{i:>5}/{len(txs_to_replay)}]  "
                      f"pass={passed}  fail={failed}  rate={rate:.1%}")

        # ── Step 5: Summarise and save results ────────────────────────────
        total     = len(txs_to_replay)
        pass_rate = round(passed / total, 4) if total else None
        saved     = pass_rate is not None and pass_rate >= pass_threshold

        # Per-function pass rates (keyed on function name, not full sig)
        fn_pass: dict[str, list[bool]] = {}
        for r in tx_results:
            fn_sig = r.get("_fn_sig", "")
            fn_name = fn_sig.split("(")[0] if fn_sig else ""
            if fn_name:
                fn_pass.setdefault(fn_name, []).append(r.get("events_match", False))
        fn_pass_rates = {
            fn: round(sum(v) / len(v), 4)
            for fn, v in fn_pass.items()
        }

        # Aggregate the new metrics across all tx_results
        gas_ratios    = [r["gas_ratio"]  for r in tx_results
                         if r.get("gas_ratio") is not None]
        log_matches   = [r["log_count_match"] for r in tx_results
                         if r.get("log_count_match") is not None]
        topic_matches = [r["topics_match"]    for r in tx_results
                         if r.get("topics_match") is not None]

        result = {
            "state":            "DONE",
            "address":          address,
            "contract_name":    contract_name,
            "deployed_address": deployed_address,
            "constructor_args": constructor_args,
            "k":                total,
            "passed":           passed,
            "failed":           failed,
            "pass_rate":        pass_rate,
            "saved":            saved,
            # Gas metrics (available for all txs via Etherscan txlist)
            "mean_gas_ratio":   round(sum(gas_ratios) / len(gas_ratios), 4)
                                if gas_ratios else None,
            # Log metrics (only when receipts.json is present)
            "has_receipts":     has_receipts,
            "log_count_match_rate":  round(sum(log_matches) / len(log_matches), 4)
                                     if log_matches else None,
            "topics_match_rate":     round(sum(topic_matches) / len(topic_matches), 4)
                                     if topic_matches else None,
            "n_view_fns":       len(view_selectors),
            "fn_pass_rates":    fn_pass_rates,
        }
        save_ckpt_k(contract_dir, k, result)

        # Write the detailed per-transaction results to a separate file to
        # avoid bloating the checkpoint with potentially thousands of entries.
        _result_path_k(contract_dir, k).write_text(json.dumps({
            "address":               address,
            "contract_name":         contract_name,
            "deployed_address":      deployed_address,
            "k":                     total,
            "passed":                passed,
            "failed":                failed,
            "pass_rate":             pass_rate,
            "mean_gas_ratio":        result["mean_gas_ratio"],
            "log_count_match_rate":  result["log_count_match_rate"],
            "topics_match_rate":     result["topics_match_rate"],
            "has_receipts":          has_receipts,
            "n_view_fns":            len(view_selectors),
            "fn_pass_rates":         fn_pass_rates,
            "timestamp":             datetime.utcnow().isoformat(),
            "tx_results":            tx_results,
        }, indent=2))

        icon = "✓ PASS" if saved else "✗ FAIL"
        rate_str = f"{pass_rate:.1%}" if pass_rate is not None else "n/a"
        print(f"  → {icon}  pass_rate={rate_str}  ({passed}/{total})\n")
        return result

    finally:
        server.close()


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

def _format_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def print_summary(results: list[dict], k: int, pass_threshold: float) -> None:
    total   = len(results)
    done    = [r for r in results if r.get("state") == "DONE"]
    failed  = [r for r in results if r.get("state") == "FAILED"]
    passed  = [r for r in done    if r.get("saved")]
    skipped = total - len(done) - len(failed)

    print("\n" + "═" * 60)
    print(f"  REPLAY SUMMARY  (K={k}, threshold={pass_threshold:.0%})")
    print("═" * 60)
    print(f"  Contracts processed : {total}")
    print(f"  Completed (DONE)    : {len(done)}")
    print(f"  Failed              : {len(failed)}")
    print(f"  Passed threshold    : {len(passed)}")

    if done:
        rates = [r["pass_rate"] for r in done if r.get("pass_rate") is not None]
        if rates:
            avg = sum(rates) / len(rates)
            print(f"  Avg pass rate       : {avg:.1%}")
            print(f"  Min / Max pass rate : {min(rates):.1%} / {max(rates):.1%}")

    if failed:
        from collections import Counter
        reasons = Counter(r.get("reason", "unknown")[:50] for r in failed)
        print(f"\n  Failure breakdown:")
        for reason, count in reasons.most_common(8):
            print(f"    {count:>4}× {reason}")
    print("═" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Replay historical transactions against freshly deployed "
            "Solidity contracts from a pre-crawled dataset."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--dataset", type=Path, default=Path(DEFAULT_DATASET),
        help="Root directory of the contracts dataset.",
    )
    p.add_argument(
        "--work-dir", type=Path, default=Path(DEFAULT_WORK_DIR),
        help="Hardhat workspace directory (created automatically).",
    )
    p.add_argument(
        "--k", type=int, default=DEFAULT_K,
        help="Number of transactions to replay per contract.",
    )
    p.add_argument(
        "--pass-threshold", type=float, default=DEFAULT_PASS_THRESHOLD,
        help="Minimum pass rate to mark a contract as saved (0.0–1.0).",
    )
    p.add_argument(
        "--tx-timeout", type=int, default=DEFAULT_TX_TIMEOUT,
        help="Seconds to wait for a single transaction replay before timeout.",
    )
    p.add_argument(
        "--addresses", nargs="*", metavar="ADDR",
        help="Replay only these contract addresses (space-separated). "
             "Default: all contracts in the dataset.",
    )
    p.add_argument(
        "--from-results", type=Path, dest="from_results", default=None,
        metavar="RESULTS_JSON",
        help="Load failed contract addresses from a previous replay_results.json "
             "and retry only those. Equivalent to passing their addresses via "
             "--addresses combined with --retry-failed, but without having to "
             "copy-paste addresses manually. Implies --retry-failed.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Re-run even if a DONE or FAILED checkpoint already exists.",
    )
    p.add_argument(
        "--retry-failed", action="store_true", dest="retry_failed",
        help="Re-run only contracts whose checkpoint is FAILED; skip DONE ones.",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print constructor args, per-tx mismatch details, and compiler errors.",
    )
    p.add_argument(
        "--results-file", type=Path,
        default=_SCRIPT_DIR / "replay_results.json",
        help="Path to write the aggregated JSON results file.",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    print(f"""
replay_transactions.py
══════════════════════════════════════════════════════════
  dataset         = {args.dataset}
  work_dir        = {args.work_dir}
  k               = {args.k}
  pass_threshold  = {args.pass_threshold:.0%}
  tx_timeout      = {args.tx_timeout}s
  force           = {args.force}
  retry_failed    = {args.retry_failed}
  verbose         = {args.verbose}
  results_file    = {args.results_file}
══════════════════════════════════════════════════════════
""")

    if not args.dataset.exists():
        print(f"ERROR: dataset directory not found: {args.dataset}")
        sys.exit(1)

    # ── Resolve addresses from --from-results ─────────────────────────────
    # If the user points at a previous results file, extract all FAILED
    # addresses from it automatically and imply --retry-failed.
    if args.from_results is not None:
        if not args.from_results.exists():
            print(f"ERROR: results file not found: {args.from_results}")
            sys.exit(1)
        try:
            prev = json.loads(args.from_results.read_text())
        except Exception as e:
            print(f"ERROR: could not parse results file: {e}")
            sys.exit(1)
        failed_addrs = [
            r["address"] for r in prev.get("results", [])
            if r.get("state") == "FAILED" and r.get("address")
        ]
        if not failed_addrs:
            print("No FAILED contracts found in the results file. Nothing to do.")
            sys.exit(0)
        print(f"  Loaded {len(failed_addrs)} FAILED address(es) from {args.from_results}")
        # Merge with any explicit --addresses the user also provided
        if args.addresses:
            combined = {a.lower() for a in args.addresses} | {a.lower() for a in failed_addrs}
            args.addresses = list(combined)
        else:
            args.addresses = failed_addrs
        # --from-results implies --retry-failed (don't skip FAILED checkpoints)
        args.retry_failed = True

    # ── Discover contract directories ─────────────────────────────────────
    if args.addresses:
        # Normalise to lowercase for case-insensitive matching
        requested = {a.lower() for a in args.addresses}
        contract_dirs = sorted([
            d for d in args.dataset.iterdir()
            if d.is_dir() and d.name.lower() in requested
        ])
        missing = requested - {d.name.lower() for d in contract_dirs}
        if missing:
            print(f"WARNING: {len(missing)} address(es) not found in dataset:")
            for m in sorted(missing):
                print(f"  {m}")
    else:
        contract_dirs = sorted([d for d in args.dataset.iterdir() if d.is_dir()])

    if not contract_dirs:
        print("No contract directories found. Exiting.")
        sys.exit(0)

    print(f"Found {len(contract_dirs)} contract(s) to process.\n")

    # ── Setup shared Hardhat workspace ────────────────────────────────────
    setup_workspace(args.work_dir)

    # ── Replay loop ───────────────────────────────────────────────────────
    all_results: list[dict] = []
    start_time = time.time()

    try:
        for i, contract_dir in enumerate(contract_dirs, 1):
            print(f"[{i}/{len(contract_dirs)}] {contract_dir.name}")
            try:
                result = replay_contract(
                    contract_dir   = contract_dir,
                    work_dir       = args.work_dir,
                    k              = args.k,
                    pass_threshold = args.pass_threshold,
                    tx_timeout     = args.tx_timeout,
                    force          = args.force,
                    retry_failed   = args.retry_failed,
                    verbose        = args.verbose,
                )
                result.setdefault("category", "")
                all_results.append(result)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  Unexpected error: {e}")
                all_results.append({
                    "state":   "FAILED",
                    "address": contract_dir.name,
                    "reason":  f"unexpected error: {e}",
                })

    except KeyboardInterrupt:
        print(f"\nInterrupted — saving partial results ...")

    # ── Write aggregated results ──────────────────────────────────────────
    elapsed = time.time() - start_time
    summary = {
        "timestamp":       datetime.utcnow().isoformat(),
        "duration_seconds": round(elapsed, 1),
        "config": {
            "k":              args.k,
            "pass_threshold": args.pass_threshold,
            "tx_timeout":     args.tx_timeout,
            "dataset":        str(args.dataset),
        },
        "total":   len(all_results),
        "done":    sum(1 for r in all_results if r.get("state") == "DONE"),
        "failed":  sum(1 for r in all_results if r.get("state") == "FAILED"),
        "passed":  sum(1 for r in all_results if r.get("saved")),
        "results": all_results,
    }
    args.results_file.write_text(json.dumps(summary, indent=2))
    print(f"\nResults written to {args.results_file}")

    # ── Print summary ─────────────────────────────────────────────────────
    print_summary(all_results, args.k, args.pass_threshold)
    print(f"\n  Total time: {_format_elapsed(elapsed)}")


if __name__ == "__main__":
    main()