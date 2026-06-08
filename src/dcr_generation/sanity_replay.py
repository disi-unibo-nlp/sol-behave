# script name: sanity_replay.py
"""
Sanity check for the deploy+replay pipeline.

Calls run_fresh_deploy_replay() from deploy_replay.py directly -- exactly the
same function used by crawl.py -- so results are guaranteed to be identical.

Usage:
  python sanity_replay.py
  python sanity_replay.py 0xADDRESS1 0xADDRESS2
  DATASET_DIR=../dataset/contracts_dataset_1to20 K=50 python sanity_replay.py
"""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path


# 0xfb272d3acfcbf3e0f92bd01ffbcde92575c371f9
# DONE         90.0%    9.1%   200  JeromePowell 
# cat ../dataset/contracts_dataset_1to20/0xfb272d3acfcbf3e0f92bd01ffbcde92575c371f9/source.json > source_file.log
# cat ../dataset/contracts_dataset_1to20/0xfb272d3acfcbf3e0f92bd01ffbcde92575c371f9/source.sol > source_file.log

sys.path.append("/workdir/src/utils/")

from deploy_replay import run_fresh_deploy_replay, setup_workspace

REPLAY_OUTPUT_DIR = Path(os.getenv("REPLAY_OUTPUT_DIR", "./sanity_replay_output"))
DATASET_DIR       = Path(os.getenv("DATASET_DIR",    "../dataset/contracts_dataset_1to20"))
WORK_DIR          = Path(os.getenv("WORK_DIR",       "../dataset/hardhat_workspace_crawl"))
K                 = int(os.getenv("K",               "200"))
PASS_THRESHOLD    = float(os.getenv("PASS_THRESHOLD", "0.80"))
TX_TIMEOUT        = int(os.getenv("TX_TIMEOUT",      "60"))
LLM_API_URL       = os.getenv("LLM_API_URL", "")
LLM_MODEL         = os.getenv("LLM_MODEL",   "claude-sonnet-4-20250514")


def _make_llm_client():
    if not LLM_API_URL:
        return None
    try:
        import openai
        return openai.OpenAI(base_url=LLM_API_URL,
                             api_key=os.getenv("LLM_API_KEY", "EMPTY"))
    except ImportError:
        return None


def _callable_fns(abi):
    return {
        e["name"] for e in abi
        if e.get("type") == "function"
        and e.get("stateMutability") not in ("view", "pure")
    }


def _fn_coverage(abi, tx_results, tx_by_hash):
    """
    Compute function coverage from replay tx_results.

    tx_results: list of dicts from replay_result.json
                fields: hash, actual_status, events_match, ...
                NOTE: no functionName or input — must join with tx_by_hash

    tx_by_hash: dict mapping hash -> original tx from transactions.json
                fields include: functionName, input, methodId, ...
    """
    from eth_hash.auto import keccak

    abi_index = {e["name"]: e for e in abi if e.get("type") == "function"}
    target    = _callable_fns(abi)

    # selector -> function name map
    sel_map = {}
    for e in abi:
        if e.get("type") != "function":
            continue
        inputs = e.get("inputs", [])
        sig    = e["name"] + "(" + ",".join(i["type"] for i in inputs) + ")"
        sel_map[keccak(sig.encode())[:4].hex()] = e["name"]

    called_ok  = set()
    called_any = set()

    for res in tx_results:
        h = res.get("hash", "")

        # Join with original tx to get functionName / input
        orig = tx_by_hash.get(h, {})

        # Try functionName first (Etherscan field, most reliable)
        fn = orig.get("functionName") or orig.get("fn_name") or ""
        fn = fn.split("(")[0].strip() if fn else ""

        # Fallback: selector lookup from input calldata
        if not fn:
            calldata = orig.get("input") or orig.get("data") or ""
            if len(calldata) >= 10:
                fn = sel_map.get(calldata[2:10].lower(), "")

        if not fn:
            continue

        called_any.add(fn)
        if res.get("events_match") or res.get("actual_status") == "success":
            called_ok.add(fn)

    denom   = len(target) if target else len(abi_index)
    covered = called_ok & (target if target else set(abi_index))
    return {
        "fn_coverage":    round(len(covered) / max(1, denom), 4),
        "called_fns_ok":  sorted(called_ok),
        "called_fns_any": sorted(called_any),
        "total_abi_fns":  denom,
        "uncovered_fns":  sorted((target if target else set(abi_index)) - called_ok),
    }


def process_contract(address, llm_client):
    contract_dir = DATASET_DIR / address
    abi_path  = contract_dir / "abi.json"
    meta_path = contract_dir / "metadata.json"
    tx_path   = contract_dir / "transactions.json"

    for p, name in [(abi_path, "abi.json"), (meta_path, "metadata.json"),
                    (tx_path, "transactions.json")]:
        if not p.exists():
            return {"address": address, "state": "SKIP", "reason": "missing " + name}

    source_path = None
    for name in ("source.sol", "source.json"):
        p = contract_dir / name
        if p.exists():
            source_path = p
            break
    if not source_path:
        return {"address": address, "state": "SKIP", "reason": "missing source"}

    abi  = json.loads(abi_path.read_text())
    meta = json.loads(meta_path.read_text())
    source_data = {"ConstructorArguments": meta.get("constructor_arguments", "")}

    raw_txs = json.loads(tx_path.read_text())
    if isinstance(raw_txs, dict):
        raw_txs = raw_txs.get("result", raw_txs.get("transactions", []))
    callable_txs = [
        tx for tx in raw_txs
        if (tx.get("to") or "").lower() == address.lower()
        and tx.get("input", "0x") not in ("0x", "")
        and tx.get("isError", "0") != "1"
    ]
    if not callable_txs:
        return {"address": address, "state": "SKIP", "reason": "no callable txs"}

    # Build hash -> original tx index for coverage join
    tx_by_hash = {tx["hash"]: tx for tx in callable_txs if tx.get("hash")}

    # Exact same call as crawl.py
    result = run_fresh_deploy_replay(
        address        = address,
        source_path    = source_path,
        meta           = meta,
        source_data    = source_data,
        abi            = abi,
        callable_txs   = callable_txs,
        k              = K,
        work_dir       = WORK_DIR,
        output_dir     = REPLAY_OUTPUT_DIR,
        pass_threshold = PASS_THRESHOLD,
        tx_timeout     = TX_TIMEOUT,
        llm_client     = llm_client,
        llm_model      = LLM_MODEL,
    )

    # Function coverage: join replay_result.json (has outcome) with
    # transactions.json (has functionName/input) via tx hash
    replay_file = REPLAY_OUTPUT_DIR / address / "replay_result.json"
    cov = {}
    if replay_file.exists():
        try:
            replay     = json.loads(replay_file.read_text())
            tx_results = replay.get("txs", [])
            cov        = _fn_coverage(abi, tx_results, tx_by_hash)
        except Exception as e:
            cov = {"fn_coverage_error": str(e)}

    return {**result, **cov}


def main():
    addresses = sys.argv[1:] if len(sys.argv) > 1 else None

    addresses = ["0xffc6dbff68a8e92a7984e474f7b7a9856945e0fb"]

    if addresses:
        dirs = [DATASET_DIR / a.lower() for a in addresses
                if (DATASET_DIR / a.lower()).is_dir()]
    else:
        dirs = sorted([d for d in DATASET_DIR.iterdir() if d.is_dir()],
                      key=lambda d: d.name)

    if not dirs:
        print("No contract directories found in " + str(DATASET_DIR))
        return

    REPLAY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    setup_workspace(WORK_DIR)
    llm_client = _make_llm_client()

    print("\nSanity replay: " + str(len(dirs)) + " contracts  (K=" + str(K) + ")\n")
    header = ("Address".ljust(44) + " " + "State".ljust(8) +
              " " + "pass_rate".rjust(9) + " " + "fn_cov".rjust(7) +
              " " + "k".rjust(5) + "  contract")
    print(header)
    print("-" * 100)

    results    = []
    counts     = defaultdict(int)
    pass_rates = []
    fn_covs    = []

    for i, d in enumerate(dirs, 1):
        print("\n[" + str(i) + "/" + str(len(dirs)) + "] " + d.name)
        try:
            r = process_contract(d.name, llm_client)
        except Exception as e:
            import traceback; traceback.print_exc()
            r = {"address": d.name, "state": "ERROR", "error": str(e)}
            print("  ERROR detail:", str(e))

        results.append(r)
        state     = r.get("state", "?")
        pass_rate = r.get("pass_rate")
        fn_cov    = r.get("fn_coverage")
        k_val     = r.get("k", "")
        name      = r.get("contract_name", "")

        pr_str  = "{:.1%}".format(pass_rate) if pass_rate is not None else "-"
        cov_str = "{:.1%}".format(fn_cov)    if fn_cov    is not None else "-"
        print("  " + d.name.ljust(42) + " " + state.ljust(8) +
              " " + pr_str.rjust(9) + " " + cov_str.rjust(7) +
              " " + str(k_val).rjust(5) + "  " + name)

        counts[state] += 1
        if pass_rate is not None: pass_rates.append(pass_rate)
        if fn_cov    is not None: fn_covs.append(fn_cov)

    print("\n" + "=" * 100)
    print("  Total: " + str(len(results)))
    for s, n in sorted(counts.items()):
        print("    " + s.ljust(12) + ": " + str(n))

    if pass_rates:
        avg_pr = sum(pass_rates) / len(pass_rates)
        above  = sum(1 for p in pass_rates if p >= PASS_THRESHOLD)
        print("\n  Pass rate (DONE):   avg={:.1%}  above {:.0%}: {}/{}".format(
            avg_pr, PASS_THRESHOLD, above, len(pass_rates)))

    if fn_covs:
        avg_cov = sum(fn_covs) / len(fn_covs)
        full    = sum(1 for c in fn_covs if c >= 1.0)
        print("  Function coverage:  avg={:.1%}  fully covered: {}/{}".format(
            avg_cov, full, len(fn_covs)))

    out = REPLAY_OUTPUT_DIR / "sanity_replay.json"
    out.write_text(json.dumps({
        "K": K, "pass_threshold": PASS_THRESHOLD,
        "n_contracts":   len(results),
        "state_counts":  dict(counts),
        "avg_pass_rate": round(sum(pass_rates)/len(pass_rates), 4) if pass_rates else None,
        "avg_fn_cov":    round(sum(fn_covs)/len(fn_covs), 4)       if fn_covs    else None,
        "contracts":     results,
    }, indent=2))
    print("\n  Saved -> " + str(out) + "\n")


if __name__ == "__main__":
    main()