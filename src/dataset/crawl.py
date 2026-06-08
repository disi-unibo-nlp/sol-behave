"""
crawl.py  —  Ethereum mainnet contract crawler con fresh deploy e checkpoint.

Flusso
──────
1. Scansiona i blocchi a partire da START_BLOCK cercando indirizzi di
   contratto che abbiano ricevuto almeno n transazioni in un blocco
   (parametro --n, default 1000).  Il contatore è cumulativo su tutti
   i blocchi scannerizzati: n non è "n tx in un singolo blocco" ma
   "n tx totali all'indirizzo prima che venga considerato candidato".

2. Ogni candidato viene classificato via ContractName / import / ABI
   (logica invariata rispetto a crawl_data_categories.py).

3. Per ogni candidato classificato:
   a. Scarica sorgente, ABI, metadata, transazioni da Etherscan.
   b. Compila su Hardhat (fresh, no fork).
   c. Deploya fresh con argomenti sintetizzati deterministicamente
      (Etherscan ConstructorArguments → deploy tx decode → defaults).
      Nessun fallback LLM.
   d. Replica le prime k transazioni (--k, default 200, deve essere <= n).
   e. Se pass_rate >= --pass-threshold salva in --output-dir/<address>/.

4. Checkpoint globale in --checkpoint-file (JSON).
5. Checkpoint per contratto in --output-dir/<address>/checkpoint.json.

Uso
───
    python crawl.py [opzioni]

    python crawl.py --start-block 17000000 --n 500 --k 100 \\
        --pass-threshold 0.8 --output-dir ./dataset --max-blocks 10000

    # Riprende automaticamente dal checkpoint:
    python crawl.py --output-dir ./dataset

Variabili d'ambiente
────────────────────
    ETH_API_KEY   — chiave Etherscan
    ALCHEMY_KEY   — chiave Alchemy
"""

import argparse
import json
import os
import re
import shutil
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import sys
sys.path.append("/workdir/src/utils/")
from etherscan_utils import (
    ALCHEMY_KEY,
    etherscan_get_source,
    get_transactions_raw,
    eth_get_deploy_block,
    is_contract,
    is_proxy,
    get_block,
    parse_source,
    save_json,
    format_time
)
from deploy_replay import (
    setup_workspace,
    run_fresh_deploy_replay,
)
from blacklist import Blacklist

# ─────────────────────────────────────────────
#  CLASSIFICATORE
# ─────────────────────────────────────────────

_NAME_PATTERNS: list[tuple[str, str]] = [
    ("defi", r"(router|swap(per)?|pair|pool|amm|dex|liquidity"
             r"|uniswap|sushiswap|curve|balancer|pancake"
             r"|camelot|traderjoe|velodrome|aerodrome|solidly)"),
    ("defi", r"(vault|lend(ing)?|borrow|repay|stake|staking"
             r"|farm(ing)?|yield|aave|compound|harvest"
             r"|morpho|euler|radiant|silo|frax|convex|conic)"),
    ("bridge", r"(bridge|relay|crosschain|cross.?chain|l1.?l2|l2.?l1"
               r"|portal|wormhole|stargate|hop(protocol)?"
               r"|across|connext|celer|synapse|multichain|anyswap"
               r"|layerzero|hyperlane|axelar|debridge|socket)"),
    ("dao", r"(govern(ance|or)?|dao|timelock|multisig|safe|gnosis"
            r"|vote|voting|proposal|treasurer?"
            r"|snapshot|tally|compound.*gov|bravo|alpha.*gov)"),
    ("nft_market", r"(marketplace|market|auction|seaport|looksrare"
                   r"|exchange|bid|offer|listing|royalt"
                   r"|blur|x2y2|rarible|sudoswap|nftfi|bend.*dao)"),
    ("oracle", r"(oracle|price.?feed|aggregator|twap|keeper|api3"
               r"|fluxaggregator|accesscontrolledoffchain"
               r"|eac.*aggregator|offchain.*aggregator"
               r"|keeperregistry|automationregistry|forwarder"
               r"|vrfcoordinator|vrf.*coordinator"
               r"|pricefeed|feedregistry|sequencer.*uptime"
               r"|tellor|uma.*oracle|disputeresolution"
               r"|pyth|redstone|dia\.data|chronicle)"),
]

_IMPORT_PATTERNS: list[tuple[str, str]] = [
    ("defi", r"(uniswap|sushiswap|curvefi|balancer|aave|compound"
             r"|yearn|harvest|morpho|euler|frax|convex)"),
    ("bridge", r"(wormhole|stargate|hop|across|connext|celer"
               r"|layerzero|hyperlane|axelar|debridge|multichain)"),
    ("dao", r"(openzeppelin.*govern|compound.*govern|\.governor"
            r"|timelock|\.voting|IGovernor|GovernorBravo|GovernorAlpha)"),
    ("nft_market", r"(seaport|looksrare|blur|x2y2|rarible|opensea|sudoswap)"),
    ("oracle", r"(@chainlink|smartcontractkit"
               r"|AggregatorV[23]Interface|AggregatorV2V3Interface"
               r"|FeedRegistryInterface|KeeperCompatible|AutomationCompatible"
               r"|VRFConsumerBase|VRFCoordinatorV2|VRFV2"
               r"|ITellor|UsingTellor|@tellor|@uma/core"
               r"|OptimisticOracle|@api3|IProxy.*api3"
               r"|IPyth|@pythnetwork|redstone-evm|@redstone"
               r"|IDIAOracle|@dia-data|IChronicle)"),
]

_ABI_SIGNATURES: dict[str, frozenset] = {
    "oracle": frozenset({
        "latestRoundData", "getRoundData", "latestAnswer", "latestTimestamp",
        "latestRound", "getAnswer", "getTimestamp", "description", "phaseId",
        "checkUpkeep", "performUpkeep", "registerUpkeep",
        "requestRandomWords", "fulfillRandomWords",
        "submitValue", "getDataBefore", "requestPrice", "proposePrice",
    }),
    "defi": frozenset({
        "swapExactTokensForTokens", "swapTokensForExactTokens",
        "swapExactETHForTokens", "swapExactTokensForETH",
        "addLiquidity", "addLiquidityETH", "removeLiquidity", "swap",
        "deposit", "withdraw", "borrow", "repay", "liquidate", "flashLoan",
        "getReserves", "getAmountsOut", "stake", "unstake", "getReward",
    }),
    "bridge": frozenset({
        "sendMessage", "receiveMessage", "bridgeETH", "bridgeERC20",
        "finalizeDeposit", "initiateWithdrawal", "finalizeWithdrawal",
        "lzReceive", "send", "receivePayload", "publishMessage",
    }),
    "dao": frozenset({
        "propose", "castVote", "castVoteWithReason", "castVoteBySig",
        "execute", "queue", "cancel", "state", "quorum",
        "votingDelay", "votingPeriod", "getVotes", "getPriorVotes",
        "queueTransaction", "executeTransaction", "execTransaction",
    }),
    "nft_market": frozenset({
        "fulfillBasicOrder", "fulfillOrder", "fulfillAdvancedOrder",
        "matchOrders", "cancelOrders", "validate", "getOrderHash",
        "matchAskWithTakerBid", "matchBidWithTakerAsk",
        "cancelMultipleMakerOrders",
    }),
}
MIN_ABI_HITS: dict[str, int] = {
    "oracle": 2, "defi": 3, "bridge": 2, "dao": 3, "nft_market": 2,
}

_TOKEN_IMPORT_RE = re.compile(r"ERC20|ERC721|ERC1155|ERC721A", re.IGNORECASE)


def _abi_fn_names(abi: list) -> set:
    return {e["name"] for e in abi
            if isinstance(e, dict) and e.get("type") == "function" and "name" in e}


def classify_contract(contract_name: str, source_raw: str, abi: list) -> str:
    name = contract_name.strip()
    for category, pattern in _NAME_PATTERNS:
        if re.search(pattern, name, re.IGNORECASE):
            return category
    imports_text = " ".join(re.findall(r'''["']([^"']+)["']''', source_raw))
    for category, pattern in _IMPORT_PATTERNS:
        if re.search(pattern, imports_text, re.IGNORECASE):
            return category
    if abi:
        fn_names = _abi_fn_names(abi)
        for category, sig_set in _ABI_SIGNATURES.items():
            if len(fn_names & sig_set) >= MIN_ABI_HITS.get(category, 2):
                return category
    if _TOKEN_IMPORT_RE.search(imports_text):
        return "generic_token"
    return "other"


# ─────────────────────────────────────────────
#  CHECKPOINT GLOBALE
# ─────────────────────────────────────────────

def load_global_checkpoint(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text())
            print(f"  Checkpoint: block={data.get('next_block')}, "
                  f"saved={data.get('total_saved', 0)}, "
                  f"evaluated={data.get('total_evaluated', 0)}")
            return data
        except Exception as e:
            print(f"  checkpoint error ({e}), starting fresh")
    return {}


def save_global_checkpoint(
    path: Path, next_block: int, counter: dict, checked: set,
    saved_per_cat: dict, total_evaluated: int,
) -> None:
    path.write_text(json.dumps({
        "timestamp":       datetime.utcnow().isoformat(),
        "next_block":      next_block,
        "saved_per_cat":   saved_per_cat,
        "total_saved":     sum(saved_per_cat.values()),
        "total_evaluated": total_evaluated,
        "counter":         counter,
        "checked":         list(checked),
    }, indent=2))


# ─────────────────────────────────────────────
#  CANDIDATE PROCESSING
# ─────────────────────────────────────────────

RATE_DELAY = 0.25


def process_candidate(
    address:        str,
    category:       str,
    source_data:    dict,
    k:              int,
    pass_threshold: float,
    tx_timeout:     int,
    work_dir:       Path,
    output_dir:     Path,
) -> dict:
    """
    Scarica i dati del contratto, compila, deploya fresh e riproduce k tx.
    Nessun LLM: constructor args da Etherscan o deploy tx decode o defaults.
    """
    from deploy_replay import _load_ckpt

    contract_dir = output_dir / address
    ckpt = _load_ckpt(contract_dir) if contract_dir.exists() else {"state": "NOT_STARTED"}

    if ckpt.get("state") in ("DONE", "FAILED"):
        saved = ckpt.get("saved", False)
        print(f"  [{address[:10]}] already {ckpt['state']}"
              f"{' (saved)' if saved else ''} — skip")
        return ckpt

    # ── 1. Raccolta dati ──────────────────────────────────────────────────
    print(f"  [{address[:10]}] collecting data ...")
    source_type, source_content = parse_source(source_data.get("SourceCode", ""))
    if source_type == "empty":
        print("  not verified — skip")
        return {"address": address, "category": category,
                "state": "FAILED", "reason": "not verified"}

    if source_data.get("Proxy") == "1":
        return {"address": address, "category": category,
                "state": "FAILED", "reason": "proxy"}

    contract_dir.mkdir(parents=True, exist_ok=True)

    if source_type == "flat":
        (contract_dir / "source.sol").write_text(source_content)
        source_path = contract_dir / "source.sol"
    else:
        save_json(contract_dir / "source.json", source_content)
        source_path = contract_dir / "source.json"

    raw_abi = source_data.get("ABI", "")
    try:
        abi = json.loads(raw_abi) if raw_abi and raw_abi != "Contract source code not verified" else []
    except Exception:
        abi = []
    save_json(contract_dir / "abi.json", abi)

    deploy_block = eth_get_deploy_block(address)
    time.sleep(RATE_DELAY)
    meta = {
        "address":               address,
        "contract_name":         source_data.get("ContractName", ""),
        "compiler_version":      source_data.get("CompilerVersion", ""),
        "optimization_used":     source_data.get("OptimizationUsed") == "1",
        "runs":                  int(source_data.get("Runs") or 200),
        "evm_version":           source_data.get("EVMVersion", "default"),
        "constructor_arguments": source_data.get("ConstructorArguments", ""),
        "license_type":          source_data.get("LicenseType", ""),
        "is_proxy":              source_data.get("Proxy") == "1",
        "implementation":        source_data.get("Implementation", ""),
        "deploy_block":          deploy_block,
        "category":              category,
    }
    save_json(contract_dir / "metadata.json", meta)

    print(f"  [{address[:10]}] fetching transactions ...", end="  ", flush=True)
    try:
        raw_txs = get_transactions_raw(address)
    except Exception as e:
        print(f"  {e}")
        return {"address": address, "state": "FAILED", "reason": f"txlist error: {e}"}
    time.sleep(RATE_DELAY)

    if deploy_block is not None:
        raw_txs = [tx for tx in raw_txs
                   if int(tx.get("blockNumber", 0)) >= deploy_block]
    save_json(contract_dir / "transactions.json", raw_txs)

    callable_txs = [
        tx for tx in raw_txs
        if (tx.get("to") or "").lower() == address.lower()
        and tx.get("input", "0x") not in ("0x", "")
        and tx.get("isError", "0") != "1"
    ]
    print(f"  {len(callable_txs)} callable txs")

    if not callable_txs:
        return {"address": address, "state": "FAILED",
                "reason": "no callable txs", "category": category}

    # ── 2. Compile + Deploy + Replay (no LLM) ─────────────────────────────
    result = run_fresh_deploy_replay(
        address        = address,
        source_path    = source_path,
        meta           = meta,
        source_data    = source_data,   # Etherscan dict with ConstructorArguments
        abi            = abi,
        callable_txs   = callable_txs,
        k              = k,
        work_dir       = work_dir,
        output_dir     = output_dir,
        pass_threshold = pass_threshold,
        tx_timeout     = tx_timeout,
        llm_client     = None,          # no LLM
        llm_model      = "",
    )
    result["category"] = category
    return result


# ─────────────────────────────────────────────
#  ARGPARSE
# ─────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Ethereum mainnet contract crawler con fresh deploy e checkpoint.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--start-block",    type=int,   default=17_000_000)
    p.add_argument("--max-blocks",     type=int,   default=None)
    p.add_argument("--n",              type=int,   default=1000)
    p.add_argument("--scan-delay",     type=float, default=0.05)
    p.add_argument("--k",              type=int,   default=200)
    p.add_argument("--pass-threshold", type=float, default=0.50)
    p.add_argument("--tx-timeout",     type=int,   default=60)
    p.add_argument("--output-dir",     type=Path,  default=Path("/workdir/src/dataset/contracts_dataset"))
    p.add_argument("--work-dir",       type=Path,  default=Path("/workdir/src/dataset/hardhat_workspace_crawl"))
    p.add_argument("--checkpoint-file",type=Path,  default=Path("/workdir/src/dataset/crawl_checkpoint.json"))
    p.add_argument("--results-file",   type=Path,  default=Path("/workdir/src/dataset/crawl_results.json"))
    p.add_argument("--blacklist",      type=Path,  default=Path("/workdir/src/dataset/blacklist.json"))
    p.add_argument("--no-other",       action="store_true")
    p.add_argument("--from-file",      type=Path,  default="/workdir/src/dataset/candidates.json")
    args = p.parse_args()
    if not args.from_file and args.k > args.n:
        p.error(f"--k ({args.k}) must be <= --n ({args.n})")
    return args


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def _progress(saved_per_cat: dict) -> str:
    parts = [f"{cat}: {n}" for cat, n in sorted(saved_per_cat.items())]
    return "  [saved]  " + "   |   ".join(parts) if parts else "  [saved]  (none)"


def main():
    args = parse_args()

    print(f"""
crawl.py  —  Ethereum mainnet contract crawler
  start_block    = {args.start_block}
  n (threshold)  = {args.n}
  k (replay txs) = {args.k}
  pass_threshold = {args.pass_threshold:.0%}
  tx_timeout     = {args.tx_timeout}s
  output_dir     = {args.output_dir}
  work_dir       = {args.work_dir}
  checkpoint     = {args.checkpoint_file}
  LLM            = disabled (constructor args from Etherscan/tx-decode/defaults)
""")

    if not os.getenv("ETH_API_KEY"):
        print("  ETH_API_KEY not set — Etherscan calls will fail")
    if not ALCHEMY_KEY:
        print("  ALCHEMY_KEY not set — block scanning requires Alchemy")

    args.output_dir.mkdir(exist_ok=True)
    setup_workspace(args.work_dir)
    bl = Blacklist(args.blacklist)

    # ── Modalità --from-file ───────────────────────────────────────────────
    if args.from_file:
        if not Path(args.from_file).exists():
            print(f"  File not found: {args.from_file}")
            sys.exit(1)
        saved_per_cat   = {}
        all_results     = []
        total_evaluated = [0]
        _run_from_file(args, saved_per_cat, all_results, total_evaluated, bl)
        bl.save()
        return

    # ── Checkpoint + scansione blocchi ────────────────────────────────────
    cp = load_global_checkpoint(args.checkpoint_file)
    block_number    = cp.get("next_block", args.start_block)
    saved_per_cat   = cp.get("saved_per_cat", {})
    total_evaluated = cp.get("total_evaluated", 0)
    counter         = defaultdict(int, cp.get("counter", {}))
    checked         = set(cp.get("checked", [])) | bl.addresses()
    all_results     = []

    print(f"Resuming from block {block_number}")
    print(_progress(saved_per_cat))

    end_block = (block_number + args.max_blocks) if args.max_blocks else None

    def _save_checkpoint():
        save_global_checkpoint(
            args.checkpoint_file, block_number, dict(counter),
            checked, saved_per_cat, total_evaluated,
        )

    def _save_results():
        args.results_file.write_text(json.dumps({
            "timestamp":       datetime.utcnow().isoformat(),
            "config": {"n": args.n, "k": args.k, "pass_threshold": args.pass_threshold,
                       "start_block": args.start_block},
            "saved_per_cat":   saved_per_cat,
            "total_saved":     sum(saved_per_cat.values()),
            "total_evaluated": total_evaluated,
            "results":         all_results,
        }, indent=2))

    try:
        while True:
            if end_block and block_number >= end_block:
                print(f"\nReached max_blocks ({args.max_blocks}), stopping.")
                break

            try:
                block = get_block(block_number)
                if block is None:
                    block_number += 1
                    continue

                for tx in block.get("transactions", []):
                    to = tx.get("to")
                    if to is None or tx.get("input", "0x") == "0x":
                        continue
                    addr = to.lower()
                    counter[addr] += 1
                    if counter[addr] < args.n or addr in checked:
                        continue
                    checked.add(addr)

                    try:
                        time.sleep(args.scan_delay)
                        if not is_contract(addr, block_number):
                            continue
                        time.sleep(args.scan_delay)
                        if is_proxy(addr):
                            print(f"  skipped proxy: {addr}")
                            continue
                        time.sleep(RATE_DELAY)
                        source_data   = etherscan_get_source(addr)
                        contract_name = source_data.get("ContractName", "")
                        source_raw    = source_data.get("SourceCode", "")
                        raw_abi = source_data.get("ABI", "")
                        try:
                            abi = (json.loads(raw_abi)
                                   if raw_abi and raw_abi != "Contract source code not verified"
                                   else [])
                        except Exception:
                            abi = []
                        category = classify_contract(contract_name, source_raw, abi)
                        if category == "other" and args.no_other:
                            print(f"  skipped (--no-other): {addr}  [{contract_name}]")
                            continue
                        print(f"\n── Candidate [{category}]: {addr}  [{contract_name}]"
                              f"  (count={counter[addr]}, block={block_number})")
                        result = process_candidate(
                            address        = addr,
                            category       = category,
                            source_data    = source_data,
                            k              = args.k,
                            pass_threshold = args.pass_threshold,
                            tx_timeout     = args.tx_timeout,
                            work_dir       = args.work_dir,
                            output_dir     = args.output_dir,
                        )
                        all_results.append(result)
                        total_evaluated += 1
                        if result.get("saved"):
                            saved_per_cat[category] = saved_per_cat.get(category, 0) + 1
                            print(_progress(saved_per_cat))
                            _save_results()
                        _save_checkpoint()
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        print(f"  candidate error on {addr}: {e}")
                    time.sleep(RATE_DELAY)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  block error {block_number}: {e}")

            if block_number % 100 == 0:
                print(f"\n  [scan] block {block_number}"
                      f" | addresses seen: {len(counter)}"
                      f" | evaluated: {total_evaluated}")
                print(_progress(saved_per_cat))
                _save_checkpoint()

            block_number += 1
            time.sleep(args.scan_delay)

    except KeyboardInterrupt:
        print(f"\nInterrotto — salvo checkpoint al blocco {block_number} ...")
        _save_checkpoint()
        _save_results()
        print(f"  checkpoint -> {args.checkpoint_file}")
        print(f"  results    -> {args.results_file}")
        print(_progress(saved_per_cat))
        sys.exit(0)

    _save_checkpoint()
    _save_results()
    print(f"\nDone.")
    print(f"  dataset  -> {args.output_dir}/")
    print(f"  results  -> {args.results_file}")
    print(_progress(saved_per_cat))


def _run_from_file(args, saved_per_cat, all_results, total_evaluated, bl):
    data       = json.loads(Path(args.from_file).read_text())
    candidates = data.get("candidates", [])
    print(f"  Loaded {len(candidates)} candidates from {args.from_file}")

    def _save():
        args.results_file.write_text(json.dumps({
            "timestamp":   datetime.utcnow().isoformat(),
            "source_file": str(args.from_file),
            "config": {"k": args.k, "pass_threshold": args.pass_threshold},
            "saved_per_cat":   saved_per_cat,
            "total_saved":     sum(saved_per_cat.values()),
            "total_evaluated": total_evaluated[0],
            "results":         all_results,
        }, indent=2))

    for i, cand in enumerate(candidates, 1):
        addr        = cand["address"].lower()
        category    = cand.get("category", "other")
        source_data = cand.get("source_data")

        if bl.contains(addr):
            reason = bl.get_reason(addr)
            if reason not in ("candidate", "saved"):
                print(f"  [{i}/{len(candidates)}] {addr[:12]} skip (blacklist: {reason})")
                continue

        print(f"\n[{i}/{len(candidates)}] {addr}  [{cand.get('contract_name','')}]"
              f"  cat={category}  tx={cand.get('tx_count','?')}")

        if source_data is None:
            from etherscan_utils import etherscan_get_source
            try:
                source_data = etherscan_get_source(addr)
                time.sleep(RATE_DELAY)
            except Exception as e:
                print(f"  source fetch failed: {e}")
                continue

        try:
            result = process_candidate(
                address        = addr,
                category       = category,
                source_data    = source_data,
                k              = args.k,
                pass_threshold = args.pass_threshold,
                tx_timeout     = args.tx_timeout,
                work_dir       = args.work_dir,
                output_dir     = args.output_dir,
            )
            all_results.append(result)
            total_evaluated[0] += 1

            if result.get("saved"):
                saved_per_cat[category] = saved_per_cat.get(category, 0) + 1
                bl.add(addr, "saved")
                print(_progress(saved_per_cat))
                _save()
            elif result.get("state") == "FAILED":
                bl.add(addr, result.get("reason", "failed")[:30])

        except KeyboardInterrupt:
            print(f"\nInterrotto — salvo risultati parziali ...")
            _save()
            sys.exit(0)
        except Exception as e:
            print(f"  error: {e}")

    _save()
    print(f"\nDone — processed {total_evaluated[0]} contracts")
    print(_progress(saved_per_cat))


if __name__ == "__main__":
    start_time = time.time()
    start_dt   = datetime.now()
    print(f"Crawl started at {start_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    main()
    end_dt  = datetime.now()
    elapsed = time.time() - start_time
    print(f"Crawl finished at {end_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Duration: {format_time(elapsed)} ({elapsed:.2f}s)")