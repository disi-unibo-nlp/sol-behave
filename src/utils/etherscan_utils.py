"""
script name: etherscan_utils.py
"""

import os
import json
import requests

ALCHEMY_KEY     = os.getenv("ALCHEMY_KEY", "")
ALCHEMY_NETWORK = os.getenv("ALCHEMY_NETWORK", "eth-mainnet")
ALCHEMY_URL     = f"https://{ALCHEMY_NETWORK}.g.alchemy.com/v2/{ALCHEMY_KEY}"


ETHERSCAN_KEY  = os.getenv("ETH_API_KEY", "")
ETHERSCAN_BASE = "https://api.etherscan.io/v2/api"
CHAIN_ID       = 1

def format_time(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"


# ── Etherscan helpers ─────────────────────────────────────────────────────────

def _get(params: dict, timeout: int = 15) -> dict:
    params = {"chainid": CHAIN_ID, "apikey": ETHERSCAN_KEY, **params}
    r = requests.get(ETHERSCAN_BASE, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def get_tx_count(address: str) -> int:
    """Conta le transazioni in ingresso al contratto (txlist, max 10k)."""
    try:
        data = _get({
            "module": "account", "action": "txlist",
            "address": address, "sort": "asc",
            "startblock": 0, "endblock": 99999999,
        })
        result = data.get("result", [])
        if isinstance(result, str):
            return 0
        # Etherscan restituisce max 10k tx; se arriviamo a 10k il contratto
        # è sicuramente abbastanza popolare
        return len(result)
    except Exception:
        return 0


def get_source_and_abi(address: str) -> dict | None:
    """Recupera sorgente + ABI + ContractName da Etherscan."""
    try:
        data = _get({"module": "contract", "action": "getsourcecode",
                     "address": address})
        if data.get("status") == "1":
            return data["result"][0]
    except Exception:
        pass
    return None


def _rpc_alchemy(method: str, params: list, timeout: int = 15) -> dict:
    """Chiama Alchemy RPC direttamente (più affidabile di Etherscan proxy per blocchi)."""
    alchemy_key = os.getenv("ALCHEMY_KEY", "")
    alchemy_net = os.getenv("ALCHEMY_NETWORK", "eth-mainnet")
    url = f"https://{alchemy_net}.g.alchemy.com/v2/{alchemy_key}"
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"RPC {method}: {data['error']}")
    return data.get("result", {})


# ─────────────────────────────────────────────
#  RPC / ETHERSCAN HELPERS
# ─────────────────────────────────────────────



def rpc(method, params, timeout=20):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = requests.post(ALCHEMY_URL, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"RPC error {method}: {data['error']}")
    return data["result"]


def get_block(block_number):
    return rpc("eth_getBlockByNumber", [hex(block_number), True])


def is_contract(address, block_number):
    return rpc("eth_getCode", [address, hex(block_number)]) != "0x"


def is_proxy(address: str) -> bool:
    try:
        r = requests.get(ETHERSCAN_BASE, params={
            "chainid": CHAIN_ID, "module": "contract",
            "action": "getsourcecode", "address": address, "apikey": ETHERSCAN_KEY,
        }, timeout=15)
        r.raise_for_status()
        result = r.json()
        if result.get("status") == "1":
            return result["result"][0].get("Proxy") == "1"
    except Exception as e:
        print(f"  proxy check error on {address}: {e}")
    return False


def eth_get_receipt(tx_hash: str):
    try:
        return rpc("eth_getTransactionReceipt", [tx_hash])
    except Exception:
        return None


def eth_call(address: str, data: str, block: str):
    try:
        return rpc("eth_call", [{"to": address, "data": data}, block])
    except Exception:
        return None


def to_bignumber(hex_str: str) -> dict:
    return {"type": "BigNumber", "hex": hex_str or "0x00"}


def etherscan_get_source(address):
    r = requests.get(ETHERSCAN_BASE, params={
        "chainid": CHAIN_ID, "module": "contract",
        "action": "getsourcecode", "address": address, "apikey": ETHERSCAN_KEY,
    }, timeout=15)
    r.raise_for_status()
    result = r.json()
    if result.get("status") != "1":
        raise RuntimeError(f"Etherscan error: {result.get('message')} — {result.get('result')}")
    return result["result"][0]


def eth_get_deploy_block(address: str):
    try:
        r = requests.get(ETHERSCAN_BASE, params={
            "chainid": CHAIN_ID, "module": "contract",
            "action": "getcontractcreation",
            "contractaddresses": address, "apikey": ETHERSCAN_KEY,
        }, timeout=15)
        r.raise_for_status()
        result = r.json()
        if result.get("status") == "1":
            creation_tx = result["result"][0].get("txHash")
            if creation_tx:
                receipt = eth_get_receipt(creation_tx)
                if receipt and receipt.get("blockNumber"):
                    return int(receipt["blockNumber"], 16)
    except Exception as e:
        print(f"    ⚠ deploy_block lookup failed: {e}")
    return None


def get_transactions_raw(address) -> list:
    r = requests.get(ETHERSCAN_BASE, params={
        "chainid": CHAIN_ID, "module": "account", "action": "txlist",
        "address": address, "sort": "asc", "apikey": ETHERSCAN_KEY,
    }, timeout=15)
    r.raise_for_status()
    result = r.json()
    txs = result.get("result") or []
    if isinstance(txs, str):
        raise RuntimeError(f"txlist error: {txs}")
    return txs


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def parse_source(raw: str):
    if not raw or raw.strip() == "":
        return "empty", None
    stripped = raw.strip()
    if stripped.startswith("{{") and stripped.endswith("}}"):
        try:
            return "standard", json.loads(stripped[1:-1])
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse Standard JSON Input: {e}")
    if stripped.startswith("{"):
        try:
            return "standard", json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return "flat", raw


def build_trace_entry(tx_hash: str, receipt, return_value=None) -> dict:
    if receipt is None:
        return {"receipt": None, "hash": tx_hash,
                "trace": {"failed": True, "gas": 0, "returnValue": ""},
                "logs": [], "return_value": return_value}

    status_hex       = receipt.get("status", "0x0")
    gas_used_hex     = receipt.get("gasUsed", "0x0")
    effective_gp_hex = receipt.get("effectiveGasPrice", "0x0")
    cumulative_hex   = receipt.get("cumulativeGasUsed", "0x0")
    block_number_int = int(receipt.get("blockNumber", "0x0"), 16)
    tx_index_int     = int(receipt.get("transactionIndex", "0x0"), 16)

    return {
        "receipt": {
            "to": receipt.get("to"), "from": receipt.get("from"),
            "contractAddress": receipt.get("contractAddress"),
            "transactionIndex": tx_index_int,
            "gasUsed": to_bignumber(gas_used_hex),
            "logsBloom": receipt.get("logsBloom", "0x" + "0" * 512),
            "blockHash": receipt.get("blockHash"),
            "transactionHash": receipt.get("transactionHash"),
            "logs": receipt.get("logs", []),
            "blockNumber": block_number_int, "confirmations": 1,
            "cumulativeGasUsed": to_bignumber(cumulative_hex),
            "effectiveGasPrice": to_bignumber(effective_gp_hex),
            "status": int(status_hex, 16),
            "type": int(receipt.get("type", "0x0"), 16),
            "byzantium": True,
        },
        "hash": tx_hash,
        "trace": {"failed": status_hex == "0x0",
                  "gas": int(gas_used_hex, 16), "returnValue": ""},
        "logs": receipt.get("logs", []),
        "return_value": return_value,
    }

