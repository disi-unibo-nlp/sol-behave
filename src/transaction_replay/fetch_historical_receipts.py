"""
fetch_historical_receipts.py
============================

One-time script that fetches eth_getTransactionReceipt for every callable
transaction in the dataset and stores the logs in a compact receipts.json
file inside each contract's directory.

This data enables event-log comparison during replay: the Node.js server
can check whether the replayed contract emits the same events (same
topic[0] = event signature hash, same count) as the historical execution.

Output per contract
-------------------
  <dataset_dir>/<address>/receipts.json

  {
    "<tx_hash>": {
      "status":  "0x1" | "0x0",
      "gasUsed": "0x...",
      "logs": [
        {
          "topics": ["0x<event-sig-hash>", ...],
          "data":   "0x..."
        },
        ...
      ]
    },
    ...
  }

Only callable transactions (same filter as replay_transactions.py) are
fetched, keeping the file small.  Transactions that are already in the
file are skipped, so the script is safe to re-run after interruptions.

Usage
-----
  python fetch_historical_receipts.py [options]

  # Fetch receipts for all contracts (may take a while — 1 RPC call per tx):
  python fetch_historical_receipts.py

  # Only specific contracts:
  python fetch_historical_receipts.py --addresses 0xabc... 0xdef...

  # Dry run: print what would be fetched without writing anything:
  python fetch_historical_receipts.py --dry-run

  # Use a different RPC endpoint:
  python fetch_historical_receipts.py --rpc-url https://...

Environment variables
---------------------
  ALCHEMY_KEY        Alchemy API key (used if --rpc-url not set)
  ALCHEMY_NETWORK    Alchemy network slug (default: eth-mainnet)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR / ".." / "utils"))

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

DEFAULT_DATASET  = str(_SCRIPT_DIR / ".." / "dataset" / "contracts_dataset_1to20")
DEFAULT_K        = 1000
DEFAULT_DELAY    = 0.05   # seconds between RPC calls to respect rate limits
DEFAULT_BATCH    = 10     # receipts to request before writing a checkpoint


# ---------------------------------------------------------------------------
# RPC helper
# ---------------------------------------------------------------------------

def _rpc(url: str, method: str, params: list, timeout: int = 20) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"RPC {method}: {data['error']}")
    return data.get("result") or {}


def _get_receipt(url: str, tx_hash: str) -> Optional[dict]:
    try:
        result = _rpc(url, "eth_getTransactionReceipt", [tx_hash])
        if not result:
            return None
        return {
            "status":  result.get("status", "0x0"),
            "gasUsed": result.get("gasUsed", "0x0"),
            "logs": [
                {
                    "topics": log.get("topics", []),
                    "data":   log.get("data", "0x"),
                }
                for log in result.get("logs", [])
            ],
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Transaction filter (mirrors replay_transactions.filter_callable_txs)
# ---------------------------------------------------------------------------

def _filter_callable_txs(
    txs: list, address: str, deploy_block: Optional[int], k: int
) -> list:
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
    return result[:k]


# ---------------------------------------------------------------------------
# Per-contract receipt fetching
# ---------------------------------------------------------------------------

def fetch_receipts_for_contract(
    contract_dir: Path,
    rpc_url:      str,
    k:            int,
    delay:        float,
    dry_run:      bool = False,
    verbose:      bool = False,
) -> dict:
    """
    Fetch receipts for a single contract's callable transactions.
    Returns a summary dict.
    """
    address = contract_dir.name

    # Load metadata
    meta_path = contract_dir / "metadata.json"
    if not meta_path.exists():
        return {"address": address, "skipped": True, "reason": "no metadata.json"}
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return {"address": address, "skipped": True, "reason": "bad metadata.json"}

    # Load transactions
    txs_path = contract_dir / "transactions.json"
    if not txs_path.exists():
        return {"address": address, "skipped": True, "reason": "no transactions.json"}
    try:
        txs = json.loads(txs_path.read_text())
    except Exception:
        return {"address": address, "skipped": True, "reason": "bad transactions.json"}

    deploy_block  = meta.get("deploy_block")
    callable_txs  = _filter_callable_txs(txs, address, deploy_block, k)

    if not callable_txs:
        return {"address": address, "skipped": True, "reason": "no callable txs"}

    # Load existing receipts to enable resumption
    receipts_path = contract_dir / "receipts.json"
    if receipts_path.exists():
        try:
            receipts = json.loads(receipts_path.read_text())
        except Exception:
            receipts = {}
    else:
        receipts = {}

    hashes_needed = [
        tx["hash"] for tx in callable_txs
        if tx.get("hash") and tx["hash"] not in receipts
    ]

    if not hashes_needed:
        return {
            "address":  address,
            "fetched":  0,
            "cached":   len(callable_txs),
            "total":    len(callable_txs),
        }

    if dry_run:
        print(f"  [dry-run] {address[:12]}…: would fetch {len(hashes_needed)} receipts")
        return {
            "address":       address,
            "would_fetch":   len(hashes_needed),
            "already_cached": len(callable_txs) - len(hashes_needed),
        }

    fetched   = 0
    failed    = 0
    contract_name = meta.get("contract_name", "")

    for i, tx_hash in enumerate(hashes_needed, 1):
        receipt = _get_receipt(rpc_url, tx_hash)
        if receipt is not None:
            receipts[tx_hash] = receipt
            fetched += 1
        else:
            failed += 1
            if verbose:
                print(f"    {tx_hash}: receipt not found")

        # Write checkpoint every DEFAULT_BATCH receipts and at the end
        if i % DEFAULT_BATCH == 0 or i == len(hashes_needed):
            receipts_path.write_text(json.dumps(receipts, separators=(",", ":")))

        if delay > 0:
            time.sleep(delay)

    return {
        "address":        address,
        "contract_name":  contract_name,
        "fetched":        fetched,
        "failed":         failed,
        "cached":         len(callable_txs) - len(hashes_needed),
        "total":          len(callable_txs),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_rpc_url() -> str:
    key = os.getenv("ALCHEMY_KEY", "")
    net = os.getenv("ALCHEMY_NETWORK", "eth-mainnet")
    if key:
        return f"https://{net}.g.alchemy.com/v2/{key}"
    return ""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch historical transaction receipts (logs) for the dataset.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--dataset", type=Path, default=Path(DEFAULT_DATASET),
        help="Dataset directory containing contract subdirectories.",
    )
    p.add_argument(
        "--k", type=int, default=DEFAULT_K,
        help="Number of callable transactions to cover per contract.",
    )
    p.add_argument(
        "--rpc-url", type=str, default="",
        dest="rpc_url",
        help="Ethereum JSON-RPC endpoint. Defaults to Alchemy via ALCHEMY_KEY env var.",
    )
    p.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY,
        help="Seconds to wait between RPC calls (rate-limit protection).",
    )
    p.add_argument(
        "--addresses", nargs="*", metavar="ADDR",
        help="Process only these contract addresses. Default: all.",
    )
    p.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Print what would be fetched without making any RPC calls.",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print per-transaction details.",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    rpc_url = args.rpc_url or _build_rpc_url()
    if not rpc_url and not args.dry_run:
        print("ERROR: no RPC URL. Set ALCHEMY_KEY or pass --rpc-url.")
        sys.exit(1)

    if not args.dataset.exists():
        print(f"ERROR: dataset not found: {args.dataset}")
        sys.exit(1)

    # Discover contract directories
    if args.addresses:
        requested     = {a.lower() for a in args.addresses}
        contract_dirs = sorted([
            d for d in args.dataset.iterdir()
            if d.is_dir() and d.name.lower() in requested
        ])
    else:
        contract_dirs = sorted([d for d in args.dataset.iterdir() if d.is_dir()])

    print(f"Fetching receipts for {len(contract_dirs)} contract(s)  (K={args.k})")
    if args.dry_run:
        print("  DRY RUN — no files will be written")
    print()

    total_fetched  = 0
    total_cached   = 0
    total_failed   = 0
    total_skipped  = 0

    for i, contract_dir in enumerate(contract_dirs, 1):
        print(f"[{i}/{len(contract_dirs)}] {contract_dir.name}", end="  ")
        try:
            result = fetch_receipts_for_contract(
                contract_dir = contract_dir,
                rpc_url      = rpc_url,
                k            = args.k,
                delay        = args.delay,
                dry_run      = args.dry_run,
                verbose      = args.verbose,
            )
            if result.get("skipped"):
                print(f"skipped ({result.get('reason','')})")
                total_skipped += 1
            elif args.dry_run:
                print(f"would fetch {result.get('would_fetch',0)}")
            else:
                f = result.get("fetched", 0)
                c = result.get("cached",  0)
                e = result.get("failed",  0)
                print(f"fetched={f}  cached={c}  failed={e}")
                total_fetched += f
                total_cached  += c
                total_failed  += e
        except KeyboardInterrupt:
            print("\nInterrupted.")
            break
        except Exception as e:
            print(f"ERROR: {e}")
            total_skipped += 1

    print(f"\nDone.")
    print(f"  Total fetched : {total_fetched}")
    print(f"  Total cached  : {total_cached}")
    print(f"  Total failed  : {total_failed}")
    print(f"  Total skipped : {total_skipped}")


if __name__ == "__main__":
    main()