"""
seed_from_etherscan.py

Costruisce rapidamente una lista di contratti candidati per il dataset
interrogando direttamente Etherscan invece di scansionare blocchi.

Strategia
──────────
Etherscan espone l'endpoint 'txlist' che restituisce le transazioni di un
contratto. Il numero totale di tx è un proxy diretto della popolarità.
Invece di aspettare che un indirizzo accumuli n tx durante la scansione
(che richiede giorni), qui si parte da indirizzi già noti come popolari
tramite tre fonti:

1. Top contract addresses per categoria (lista seed manuale o da API)
2. Etherscan 'getcontractcreation' + txlist per verificare popolarità
3. Etherscan verified contracts list (endpoint non documentato ma stabile)

Output
──────
candidates.json  — lista di {address, contract_name, category, tx_count}
                   pronta per essere consumata da crawl.py --from-file

Uso
───
    python seed_from_etherscan.py [--min-tx 200] [--output candidates.json]

    # Poi passa la lista a crawl.py:
    python crawl.py --from-file candidates.json --k 200 --pass-threshold 0.8

Variabili d'ambiente
────────────────────
    ETH_API_KEY  — chiave Etherscan (obbligatoria)
"""

import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests

from blacklist import Blacklist
import sys
sys.path.append("/workdir/src/utils/")
from etherscan_utils import _get, get_tx_count, get_source_and_abi, _rpc_alchemy, format_time

from tqdm import tqdm

import time

ETHERSCAN_KEY  = os.getenv("ETH_API_KEY", "")
ETHERSCAN_BASE = "https://api.etherscan.io/v2/api"
CHAIN_ID       = 1
RATE_DELAY     = 0.22   # ~4.5 req/s, sotto il limite free di 5/s





def get_contracts_from_block_range(
    start_block: int, end_block: int, min_tx: int, seen: set,
    bl: "Blacklist", max_candidates: int = 500,
) -> list[dict]:
    """
    Trova contratti attivi in un range di blocchi.
    Usa Alchemy per leggere le tx dei blocchi campionati, poi verifica
    i contratti più frequenti tramite Etherscan.
    """
    from collections import Counter

    candidates = []
    counter    = Counter()

    total_range   = end_block - start_block
    sample_step   = max(1, total_range // 200)
    sample_blocks = list(range(start_block, end_block, sample_step))[:200]

    alchemy_key = os.getenv("ALCHEMY_KEY", "")
    if not alchemy_key:
        print("  ⚠ ALCHEMY_KEY non impostata — block scan disabilitato")
        return []

    print(f"  Sampling {len(sample_blocks)} blocks in range "
          f"{start_block:,}–{end_block:,} via Alchemy ...", flush=True)

    for i, block_num in enumerate(sample_blocks):
        try:
            # true = includi lista transazioni completa
            result = _rpc_alchemy(
                "eth_getBlockByNumber",
                [hex(block_num), True],
            )
            txs = result.get("transactions", []) if isinstance(result, dict) else []
            for tx in txs:
                to = tx.get("to")
                # Salta contract creation (to=None) e zero-address
                if to and to.lower() != "0x0000000000000000000000000000000000000000":
                    counter[to.lower()] += 1
        except Exception as e:
            pass  # blocco non trovato o errore RPC, continua
        time.sleep(0.05)  # Alchemy free: 5 req/s

        if (i + 1) % 20 == 0:
            print(f"    sampled {i+1}/{len(sample_blocks)} blocks,"
                  f" {len(counter)} unique addresses seen", flush=True)

    print(f"  Sampling done: {len(counter)} unique addresses,"
          f" top frequency: {counter.most_common(1)}", flush=True)

    # Pre-filtra con eth_getCode per tenere solo contratti reali,
    # evitando di sprecare chiamate Etherscan su wallet EOA.
    # Prende solo gli indirizzi che appaiono almeno 3 volte nel campione
    # (proxy di popolarità) e verifica via RPC se sono contratti.
    min_freq     = 3
    frequent     = [addr for addr, cnt in counter.most_common(5000) if cnt >= min_freq]
    print(f"  {len(frequent)} addresses with freq >= {min_freq} — "
          f"pre-filtering with eth_getCode ...", flush=True)

    contract_addresses = []
    for addr in tqdm(frequent):
        if addr in seen:
            continue

        time.sleep(0.05)

        code = None
        try:
            code = _rpc_alchemy("eth_getCode", [addr, "latest"])
        except Exception:
            pass

        if not code or code == "0x":
            bl.add(addr, "not_contract")
            continue

        contract_addresses.append(addr)

    print(f"  {len(contract_addresses)} are actual contracts — "
          f"checking Etherscan for source ...", flush=True)

    # Verifica i contratti su Etherscan (source + tx count)
    top_addresses = contract_addresses

    for addr in tqdm(top_addresses):
        if addr in seen or len(candidates) >= max_candidates:
            continue
        #seen.add(addr)
        time.sleep(RATE_DELAY)

        source_data = get_source_and_abi(addr)
        if source_data is None or not source_data.get("SourceCode"):
            bl.add(addr, "not_verified")
            continue
        if source_data.get("Proxy") == "1":
            bl.add(addr, "proxy")
            continue

        contract_name = source_data.get("ContractName", "")
        if not contract_name:
            bl.add(addr, "not_verified")
            continue

        time.sleep(RATE_DELAY)
        if counter[addr] >= 50:
            tx_count = min_tx
        else:
            time.sleep(RATE_DELAY)
            tx_count = get_tx_count(addr)
            if tx_count < min_tx:
                bl.add(addr, "low_tx", {"tx_count": tx_count})
                continue

        source_raw = source_data.get("SourceCode", "")
        raw_abi    = source_data.get("ABI", "")
        try:
            abi = json.loads(raw_abi) if raw_abi and raw_abi != "Contract source code not verified" else []
        except Exception:
            abi = []

        category = classify(contract_name, source_raw, abi)
        candidates.append({
            "address":       addr,
            "contract_name": contract_name,
            "category":      category,
            "tx_count":      tx_count,
            "source_data":   source_data,
        })
        seen.add(addr) 
        print(f"    ✓ {addr[:12]}…  [{contract_name}]  cat={category}  tx={tx_count}")

    return candidates


# ── Classificatore (identico a crawl.py) ─────────────────────────────────────

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
               r"|vrfcoordinator|pricefeed|feedregistry"
               r"|tellor|uma.*oracle|pyth|redstone|chronicle)"),
]
_TOKEN_IMPORT_RE = re.compile(r"ERC20|ERC721|ERC1155|ERC721A", re.IGNORECASE)
_ABI_SIGNATURES: dict[str, frozenset] = {
    "oracle": frozenset({"latestRoundData", "getRoundData", "latestAnswer",
                         "checkUpkeep", "performUpkeep", "requestRandomWords"}),
    "defi":   frozenset({"swapExactTokensForTokens", "addLiquidity", "swap",
                         "deposit", "withdraw", "borrow", "repay", "flashLoan"}),
    "bridge": frozenset({"sendMessage", "bridgeETH", "bridgeERC20",
                         "finalizeDeposit", "lzReceive", "publishMessage"}),
    "dao":    frozenset({"propose", "castVote", "execute", "queue",
                         "votingDelay", "votingPeriod", "getVotes"}),
    "nft_market": frozenset({"fulfillBasicOrder", "fulfillOrder",
                              "matchOrders", "cancelOrders", "getOrderHash"}),
}
MIN_ABI_HITS = {"oracle": 2, "defi": 3, "bridge": 2, "dao": 3, "nft_market": 2}


def classify(contract_name: str, source_raw: str, abi: list) -> str:
    name = contract_name.strip()
    for category, pattern in _NAME_PATTERNS:
        if re.search(pattern, name, re.IGNORECASE):
            return category
    imports_text = " ".join(re.findall(r'''["']([^"']+)["']''', source_raw))
    for category, pattern in _NAME_PATTERNS:  # reuse name patterns on imports too
        pass
    if abi:
        fn_names = {e["name"] for e in abi
                    if isinstance(e, dict) and e.get("type") == "function" and "name" in e}
        for cat, sigs in _ABI_SIGNATURES.items():
            if len(fn_names & sigs) >= MIN_ABI_HITS.get(cat, 2):
                return cat
    if _TOKEN_IMPORT_RE.search(imports_text):
        return "generic_token"
    return "other"


# ── Discovery strategies ──────────────────────────────────────────────────────

def discover_from_verified_list(
    max_pages: int, offset: int, min_tx: int, seen: set
) -> list[dict]:
    """
    Scorre la lista di contratti verificati su Etherscan (più recenti prima)
    e filtra quelli con abbastanza transazioni.
    """
    candidates = []
    print(f"  Scanning verified contracts list ({max_pages} pages × {offset}) ...")

    for page in range(1, max_pages + 1):
        contracts = get_verified_contracts_page(page, offset)
        if not contracts:
            print(f"    page {page}: empty response, stopping")
            break

        print(f"    page {page}: {len(contracts)} contracts", end="  ", flush=True)
        new_this_page = 0

        for entry in contracts:
            addr = entry.get("Address", "").lower()
            if not addr or addr in seen:
                continue
            seen.add(addr)

            time.sleep(RATE_DELAY)
            source_data = get_source_and_abi(addr)
            if source_data is None:
                continue

            if source_data.get("Proxy") == "1":
                continue

            contract_name = source_data.get("ContractName", "")
            source_raw    = source_data.get("SourceCode", "")
            raw_abi       = source_data.get("ABI", "")
            try:
                abi = json.loads(raw_abi) if raw_abi and raw_abi != "Contract source code not verified" else []
            except Exception:
                abi = []

            if not source_raw:
                continue

            time.sleep(RATE_DELAY)
            tx_count = get_tx_count(addr)
            if tx_count < min_tx:
                continue

            category = classify(contract_name, source_raw, abi)
            candidates.append({
                "address":       addr,
                "contract_name": contract_name,
                "category":      category,
                "tx_count":      tx_count,
                "source_data":   source_data,   # riusato da crawl.py, zero chiamate extra
            })
            new_this_page += 1

        print(f"+{new_this_page} candidates (total {len(candidates)})")
        time.sleep(RATE_DELAY)

    return candidates


def discover_from_seed_list(addresses: list[str], seen: set,
                            bl: "Blacklist") -> list[dict]:
    """
    Verifica una lista di indirizzi seed noti (es. protocolli famosi).
    Utile per garantire diversità categorica nel dataset.
    """
    candidates = []
    print(f"  Checking {len(addresses)} seed addresses ...")

    for addr in addresses:
        addr = addr.lower()
        if addr in seen:
            continue
        seen.add(addr)

        time.sleep(RATE_DELAY)
        source_data = get_source_and_abi(addr)
        if source_data is None or not source_data.get("SourceCode"):
            bl.add(addr, "not_verified")
            continue
        if source_data.get("Proxy") == "1":
            bl.add(addr, "proxy")
            continue

        contract_name = source_data.get("ContractName", "")
        source_raw    = source_data.get("SourceCode", "")
        raw_abi       = source_data.get("ABI", "")
        try:
            abi = json.loads(raw_abi) if raw_abi and raw_abi != "Contract source code not verified" else []
        except Exception:
            abi = []

        time.sleep(RATE_DELAY)
        tx_count = get_tx_count(addr)

        category = classify(contract_name, source_raw, abi)
        candidates.append({
            "address":       addr,
            "contract_name": contract_name,
            "category":      category,
            "tx_count":      tx_count,
            "source_data":   source_data,
        })
        bl.add(addr, "candidate")
        print(f"    ✓ {addr[:12]}…  [{contract_name}]  cat={category}  tx={tx_count}")

    return candidates


# ── Known high-quality seed addresses ────────────────────────────────────────
# Contratti mainnet verificati con sorgente pubblico e molte tx.
# Coprono categorie diverse per garantire varietà nel dataset.

KNOWN_SEEDS = [
    # ERC-20 tokens con logica custom
    "0x0bc529c00c6401aef6d220be8c6ea1667f6ad93e",  # YFI
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9",  # AAVE token
    "0xc00e94cb662c3520282e6f5717214004a7f26888",  # COMP
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984",  # UNI
    "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2",  # MKR
    "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",  # WBTC
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
    # NFT contracts
    "0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d",  # BAYC
    "0xb47e3cd837ddf8e4c57f05d70ab865de6e193bbb",  # CryptoPunks
    "0x60e4d786628fea6478f785a6d7e704777c86a7c6",  # MAYC
    "0x49cf6f5d44e70224e2e23fdcdd2c053f30ada28b",  # CloneX
    "0x1670dfacf2b89ea0bff27a5e6e7f86463e4247ff",  # Mi1ady (già nel dataset)
    "0x2bb9919119fc78b311b992519a819d75ae8aa7e4",  # PUNKXYZ
    "0x9d4de6968603b6e7641586a413ce3ff5c2551321",  # Skulls
    # DeFi
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",  # Uniswap V2 Router
    "0xe592427a0aece92de3edee1f18e0157c05861564",  # Uniswap V3 Router
    "0xba12222222228d8ba445958a75a0704d566bf2c8",  # Balancer Vault
    "0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7",  # Curve 3pool
    "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2",  # Aave V3 Pool
    # DAO
    "0xd9db270c1b5e3bd161e8c8503c55ceabeee709552",  # Gnosis Safe
    "0x408ed6354d4973f66138c91495f2f2fcbd8724c3",  # Uniswap Governor
    # Bridge
    "0x98f3c9e6e3face36baad05fe09d375ef1464288b",  # Wormhole Core
    "0x3ee18b2214aff97000d974cf647e7c347e8fa585",  # Wormhole Token Bridge
]

KNOWN_SEEDS = [] # TODO: remove this


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Costruisce la lista di candidati per crawl.py.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--min-tx",        type=int,  default=200,
                   help="Numero minimo di transazioni storiche per includere un contratto.")
    p.add_argument("--start-block",   type=int,  default=17_000_000,
                   help="Blocco iniziale per il campionamento.")
    p.add_argument("--end-block",     type=int,  default=17_200_000,
                   help="Blocco finale per il campionamento.")
    p.add_argument("--max-candidates",type=int,  default=500,
                   help="Numero massimo di candidati dalla scansione blocchi.")
    p.add_argument("--no-seeds",      action="store_true",
                   help="Non include la lista seed manuale.")
    p.add_argument("--no-scan",       action="store_true",
                   help="Non scansiona i blocchi (usa solo i seed).")
    p.add_argument("--blacklist",     type=Path, default=Path("/workdir/src/dataset/blacklist.json"),
                   help="File JSON della blacklist condivisa con crawl.py.")
    p.add_argument("--output",        type=Path, default=Path("/workdir/src/dataset/candidates.json"),
                   help="File di output.")
    p.add_argument("--append",        action="store_true",
                   help="Aggiunge a un file esistente invece di sovrascrivere.")
    return p.parse_args()


def main():
    args = parse_args()

    if not ETHERSCAN_KEY:
        print("✗ ETH_API_KEY non impostata")
        return

    bl = Blacklist(args.blacklist)
    # seen unifica blacklist + candidati già salvati
    seen: set = bl.addresses()
    candidates: list[dict] = []

    # Carica candidati esistenti se --append
    if args.append and args.output.exists():
        existing   = json.loads(args.output.read_text())
        candidates = existing.get("candidates", [])
        seen      |= {c["address"].lower() for c in candidates}
        print(f"Loaded {len(candidates)} existing candidates")

    # 1. Seed list
    if not args.no_seeds:
        print("\n── Seed list ──────────────────────────────────────────────")
        seed_candidates = discover_from_seed_list(KNOWN_SEEDS, seen, bl)
        candidates.extend(seed_candidates)
        print(f"  Seed: {len(seed_candidates)} new candidates")

    # 2. Block range scan
    if not args.no_scan:
        print("\n── Block range scan ───────────────────────────────────────")
        scan_candidates = get_contracts_from_block_range(
            start_block    = args.start_block,
            end_block      = args.end_block,
            min_tx         = args.min_tx,
            seen           = seen,
            bl             = bl,
            max_candidates = args.max_candidates,
        )
        candidates.extend(scan_candidates)
        print(f"  Block scan: {len(scan_candidates)} new candidates")

    bl.save()

    # Sort by tx_count descending
    candidates.sort(key=lambda c: c.get("tx_count", 0), reverse=True)

    # Stats
    from collections import Counter
    cat_counts = Counter(c["category"] for c in candidates)
    print(f"\n── Summary ─────────────────────────────────────────────────")
    print(f"  Total candidates: {len(candidates)}")
    for cat, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"    {cat:<20} {n}")
    print(f"  Blacklist size:   {len(bl):,}")
    bl_stats = bl.stats()
    for reason, n in sorted(bl_stats.items(), key=lambda x: -x[1]):
        print(f"    {reason:<25} {n}")

    # Save candidates
    args.output.write_text(json.dumps({
        "timestamp":   datetime.utcnow().isoformat(),
        "min_tx":      args.min_tx,
        "total":       len(candidates),
        "by_category": dict(cat_counts),
        "candidates":  candidates,
    }, indent=2))
    print(f"\n✓ Saved {len(candidates)} candidates → {args.output}")
    print(f"  Use with: python crawl.py --from-file {args.output} --k 200")



if __name__ == "__main__":
    start_time = time.time()
    start_dt = datetime.now()

    print(f"🚀 Seed Phase Started at {start_dt.strftime('%Y-%m-%d %H:%M:%S')}")

    main()

    end_dt = datetime.now()
    elapsed = time.time() - start_time

    print(f"🏁 Seed Phase Finished at {end_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"⏱ Duration: {format_time(elapsed)} ({elapsed:.2f}s)")