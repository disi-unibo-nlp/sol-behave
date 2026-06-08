# script name: make_sequences_hardhat.py
# script order: 5b
"""
Generates DCR sequences by executing them directly on Hardhat, saving only
transactions that succeeded. The output transactions_synthetic.json has
100% replayability — every tx in it was already validated on-chain.

Pipeline:
  1. Deploy the contract (fresh deploy; constructor args synthesized by LLM
     if needed — no inject mode). Args are cached in deploy_params.json.
  2. Explore the DCR graph using real on-chain state for guard evaluation,
     accumulating at least MAX_STEPS successful transactions.
  3. If the DCR is exhausted before MAX_STEPS, reset DCR state + storage
     and continue with a new random seed.
  4. Keep only successful transactions.
  5. Drain phase: flush pending events, then ownership_change events last.

Coverage maximisation:
  - Events not yet successfully executed get a 3× sampling weight.
  - Events executed once get 1× weight.
  - Events executed ≥ MAX_EXEC_REPEATS times get 0.2× weight (still possible
    but deprioritised in favour of unexplored events).

Compile/deploy alignment with deploy_replay.py:
  - write_hardhat_config_aligned() mirrors deploy_replay._hardhat_config:
    overrides: {}, remapping detection, pragma extraction, evmVersion clamping.
  - install_npm_deps_aligned() mirrors deploy_replay._install_npm_deps.
  These replace the write_hardhat_config / install_npm_deps from hardhat_server
  which lacks overrides and remapping support.

Output (per contract):
  GENERATED_DIR/<address>/transactions_synthetic.json
  GENERATED_DIR/<address>/sequences_hardhat.json
  GENERATED_DIR/<address>/coverage.json
  DATASET_DIR/<address>/deploy_params.json

Usage:
  python make_sequences_hardhat.py
  python make_sequences_hardhat.py 0xADDRESS
  SEED=42 MAX_STEPS=200 python make_sequences_hardhat.py
"""

import json
import os
import random
import re as _re
import subprocess
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from eth_hash.auto import keccak

from eth_utils import to_checksum_address
from eth_abi import encode as abi_encode
from eth_utils import function_signature_to_4byte_selector

#sys.path.append("/workdir/src/utils/")
from deploy_replay import synthesize_constructor_args as _synth_args
# ── hardhat_utils: authoritative compile/install/config ──────────────────────
from hardhat_utils import (
    prepare_and_compile  as hu_prepare_and_compile,
    setup_workspace      as hu_setup_workspace,
    load_source_texts    as hu_load_source_texts,
    wipe_hardhat_artifacts as hu_wipe_hardhat_artifacts
)

from hardhat_server import (
    HardhatServer,
    DeployFailedError,
    setup_workspace,
    pick_fork_block,
    write_source,
    OWNER_ADDR,
    USER_ADDR,
    USER2_ADDR,
    hardhat_accounts,
)

from deploy_replay import (
        _write_source      as _dr_write_source,
        _install_npm_deps  as _dr_install_npm_deps,
        _detect_remappings as _dr_detect_remappings,
        _hardhat_config    as _dr_hardhat_config,
    )

DATASET_DIR   = Path(os.getenv("DATASET_DIR",   "../dataset/contracts_dataset_1to20"))
GENERATED_DIR = Path(os.getenv("GENERATED_DIR", "./generated_dcr_1to20"))
WORK_DIR      = Path(os.getenv("WORK_DIR",      "../dataset/hardhat_workspace_crawl"))
DCR_FILENAME  = os.getenv("DCR_FILENAME", "source_dcr.json")

SEED         = int(os.getenv("SEED",         "42"))
MAX_STEPS    = int(os.getenv("MAX_STEPS",    "1000"))
PENDING_BIAS = float(os.getenv("PENDING_BIAS", "0.7"))

# Events executed this many times get deprioritised (weight 0.2×)
MAX_EXEC_REPEATS = int(os.getenv("MAX_EXEC_REPEATS", "5"))
MAX_RESETS       = int(os.getenv("MAX_RESETS",       "300"))

LLM_API_URL = os.getenv("LLM_API_URL", "")
LLM_MODEL   = os.getenv("LLM_MODEL",   "claude-sonnet-4-20250514")


def _make_llm_client():
    if not LLM_API_URL:
        return None
    try:
        import openai
        return openai.OpenAI(base_url=LLM_API_URL, api_key=os.getenv("LLM_API_KEY", "EMPTY"))
    except ImportError:
        return None


_llm_client = _make_llm_client()

"""
for addr in 0x2b4e266b7d04f945f0de4b5b9fac5ca440974736 \
            0x2be8e422cb4a5a7f217a8f1b0658952a79132f28 \
            0xa4fc78495d545fd0991a4bc86a0fe01cda4422bd; do
    echo "=== $addr ==="
    npx hardhat compile --force
done
"""

# =============================================================================
#  Compile / deploy helpers — aligned with deploy_replay.py
# =============================================================================

# ── UCB weight for unexplored event bonus ─────────────────────────────────────

def _decode_constructor_args_from_hex(hex_str: str, abi_inputs: list) -> list | None:
    """
    Decode Etherscan constructor_arguments hex using the ABI.
    Tries full decode first, then aligned-from-end (Etherscan sometimes prepends
    extra bytes). Returns None if both attempts fail.
    """
    if not hex_str or not abi_inputs:
        return None
    try:
        from eth_abi import decode as _abi_decode
        types = [inp["type"] for inp in abi_inputs]
        data  = bytes.fromhex(hex_str.lstrip("0x"))
        try:
            decoded = _abi_decode(types, data)
            return list(decoded)
        except Exception:
            # Etherscan sometimes prepends a function selector or extra padding
            # — try decoding from the last N*32 bytes
            n_slots = sum(2 if t in ("string", "bytes") or t.endswith("[]") or "tuple" in t
                          else 1 for t in types)
            for offset in range(0, min(len(data), 128), 32):
                try:
                    decoded = _abi_decode(types, data[offset:])
                    return list(decoded)
                except Exception:
                    continue
        return None
    except Exception:
        return None


UCB_WEIGHT       = float(os.getenv("UCB_WEIGHT",        "2.0"))
MAX_EVENT_FAILURES = int(os.getenv("MAX_EVENT_FAILURES", "5"))


def _compile_and_configure(work_dir: Path, source_path: Path, meta: dict) -> bool:
    """
    Compile via hardhat_utils.prepare_and_compile — the same pipeline used
    by deploy_replay.py: multi-compiler, OZ versioning, remapping detection.
    Returns True on success.
    """
    ok, errs = hu_prepare_and_compile(source_path, meta, work_dir)
    if not ok:
        print("FAILED")
        print("=" * 80)
        print(errs)
        print("=" * 80)
    return ok



# =============================================================================
#  DCR helpers (unchanged from original)
# =============================================================================

def extract_state_variables_from_guards(dcr: dict) -> dict:
    SKIP = {
        "msg","sender","value","block","number","timestamp","true","false",
        "address","this","balance","uint256","uint","int","bool","bytes","string",
        "if","else","require","return","emit","new","delete","mapping","struct",
        "event","function","modifier","public","private","internal","external",
        "view","pure","payable","override","virtual","memory","storage","calldata",
        "from","to","origin","caller","gas","ether","wei","gwei",
    }
    state_vars: dict = {}
    guards = (
        [rel.get("guard","") for rel in dcr.get("relations",[])] +
        [b.get("guard","")   for b   in dcr.get("branches",[])]
    )
    for guard in guards:
        if not guard: continue
        g = _re.sub(r'address\s*\(\s*this\s*\)\s*\.\s*balance', '', guard)
        for m in _re.finditer(r'(\w+)\s*\(\s*msg\.sender\s*\)', g):
            name = m.group(1)
            if name not in SKIP:
                state_vars[f"{name}_signer"] = {"name": name, "args": ["signer"]}
        for m in _re.finditer(r'(\w+)\s*\(\s*\)', g):
            name = m.group(1)
            if name not in SKIP:
                state_vars[name] = {"name": name, "args": []}
        stripped = _re.sub(r'\w+\s*\([^)]*\)', '', g)
        stripped = _re.sub(r'\w+\s*\[[^\]]*\]', '', stripped)
        stripped = _re.sub(r'msg\s*\.\s*\w+', '',  stripped)
        stripped = _re.sub(r'block\s*\.\s*\w+', '', stripped)
        for m in _re.finditer(r'([a-zA-Z_]\w*)', stripped):
            name = m.group(1)
            if name in SKIP or name in state_vars or len(name) < 2: continue
            state_vars[name] = {"name": name, "args": []}
    return state_vars


@dataclass
class DCRState:
    executed: set = field(default_factory=set)
    included: set = field(default_factory=set)
    pending:  set = field(default_factory=set)


def get_enabled(dcr, state):
    conditions = {e["id"]: set() for e in dcr["events"]}
    for rel in dcr["relations"]:
        if rel.get("type") == "condition":
            conditions[rel["to"]].add(rel["from"])
    repeatable = {e["id"] for e in dcr["events"] if e.get("repeatable", False)}
    enabled = []
    for event in dcr["events"]:
        eid = event["id"]
        if eid not in state.included: continue
        if eid in state.executed and eid not in repeatable: continue
        if any(c not in state.executed and c in state.included
               for c in conditions[eid]): continue
        enabled.append(eid)
    return enabled


def fire_dcr(dcr, state, event_id, on_chain=None, signer_addr="", known_state=None):
    new_exec = state.executed | {event_id}
    new_incl = set(state.included)
    new_pend = set(state.pending)
    for rel in dcr["relations"]:
        if rel["from"] != event_id: continue
        rtype = rel.get("type")
        guard = rel.get("guard","")
        if rtype == "exclusion":
            if guard and on_chain is not None:
                if not evaluate_guard(guard, on_chain, signer_addr, known_state): continue
            new_incl.discard(rel["to"])
        elif rtype in ("inclusion","config"):
            new_incl.add(rel["to"])
        elif rtype == "response":
            if rel["to"] in new_incl: new_pend.add(rel["to"])
    new_pend.discard(event_id)
    return DCRState(new_exec, new_incl, new_pend)


def initial_state(dcr):
    return DCRState(
        executed=set(dcr["marking"]["executed"]),
        included=set(dcr["marking"]["included"]),
        pending= set(dcr["marking"]["pending"]),
    )


def _parse_arith(expr: str) -> int | None:
    """
    Parse a simple arithmetic expression of digits and +/-/*.
    Returns None if the expression still contains unresolved symbols
    (letters), so the caller can treat the guard conservatively.
    """
    s = _re.sub(r'\s*(ether|wei|gwei)\b', '', expr.strip())
    s = s.strip()
    # type(uint256).max → substitute known constant
    s = s.replace("type(uint256).max", str(2**256 - 1))
    s = s.replace("type(uint128).max", str(2**128 - 1))
    # address(0) → 0  (for address == address(0) comparisons)
    s = _re.sub(r'address\s*\(\s*0\s*\)', '0', s)
    if not s: return None
    if _re.search(r'[a-zA-Z_]', s):
        return None   # unresolved symbol — signal to caller
    try:
        if _re.match(r'^[\d\s\+\-\*\(\)]+$', s):
            return int(eval(s))  # safe: only digits and arithmetic ops
        return int(s)
    except Exception:
        return None


def _eval_simple(expr: str) -> bool:
    """
    Evaluate a simple comparison like '5 >= 3' or '0 == 0'.
    Returns True (conservative) if either side cannot be resolved.
    """
    for op in (">=", "<=", "!=", "==", ">", "<"):
        if op not in expr:
            continue
        parts = expr.split(op, 1)
        if len(parts) != 2:
            continue
        lhs = _parse_arith(parts[0].strip())
        rhs = _parse_arith(parts[1].strip())
        if lhs is None or rhs is None:
            return True   # conservative: unresolved symbol → don't block
        if op == ">=": return lhs >= rhs
        if op == "<=": return lhs <= rhs
        if op == "!=": return lhs != rhs
        if op == "==": return lhs == rhs
        if op == ">":  return lhs >  rhs
        if op == "<":  return lhs <  rhs
    # Boolean literals
    s = expr.strip().lower()
    if s == "true":  return True
    if s == "false": return False
    # Negation: !expr
    if s.startswith("!"):
        inner = _eval_simple(s[1:].strip())
        return not inner
    return True   # conservative default


def evaluate_guard(guard: str, on_chain: dict, signer_addr: str,
                   known_state: dict | None = None) -> bool:
    """
    Evaluate a Solidity guard string against current on-chain state.

    Resolution priority (highest to lowest):
      1. known_state  — DCR-declared constants (e.g. tenYearSupplyCap)
      2. on_chain     — live values read from the chain via read_state
      3. Hardcoded Solidity constants (type(uint256).max, address(0))

    Substitution strategy:
      - Symbols are sorted longest-first to avoid partial replacements
        (e.g. "maxBuyAmount" before "max")
      - balanceOf(msg.sender) → on_chain["balanceOf_signer"]
      - address(this).balance → on_chain["contractBalance"]
      - fn() zero-arg calls   → on_chain["fn"]
      - plain identifiers     → on_chain[name] or known_state[name]

    Conservative fallback: if after substitution any unresolved symbol
    remains, returns True (don't block the event — let the chain decide).
    """
    if not guard or not guard.strip():
        return True

    g = guard.strip()

    # Build substitution map: longest keys first to avoid partial matches
    subs: dict[str, str] = {}

    # known_state (DCR constants) — lowest priority, added first
    if known_state:
        for k, v in known_state.items():
            subs[k] = str(v)

    # on_chain values
    for k, v in on_chain.items():
        if v is None: continue
        subs[k] = str(v)

    # Derived aliases: "fn_signer" key → "fn(msg.sender)" pattern
    for k, v in on_chain.items():
        if k.endswith("_signer") and v is not None:
            fn_name = k[:-len("_signer")]
            subs[f"{fn_name}(msg.sender)"] = str(v)

    # address(this).balance
    if "contractBalance" in on_chain and on_chain["contractBalance"] is not None:
        subs["address(this).balance"] = str(on_chain["contractBalance"])

    # msg.sender → signer address (as integer for numeric comparisons,
    # but keep as hex string — address comparisons use == address(0))
    subs["msg.sender"] = signer_addr

    # Apply substitutions longest-key-first
    expr = g
    for sym in sorted(subs, key=len, reverse=True):
        val = subs[sym]
        if sym in expr:
            expr = expr.replace(sym, val)

    # Handle &&  / ||
    if "&&" in expr:
        return all(_eval_simple(p.strip()) for p in expr.split("&&"))
    if "||" in expr:
        return any(_eval_simple(p.strip()) for p in expr.split("||"))

    return _eval_simple(expr)


def apply_guard_filters(dcr, state, on_chain, signer_addr, fired_event=None, known_state=None):
    new_incl = set(state.included)
    changes  = []
    for rel in dcr["relations"]:
        guard = rel.get("guard","")
        if not guard: continue
        rtype = rel.get("type")
        if rtype not in ("exclusion","inclusion"): continue
        src, tgt = rel["from"], rel["to"]
        if fired_event is not None:
            if src != fired_event: continue
        else:
            if rtype == "exclusion": continue
        holds = evaluate_guard(guard, on_chain, signer_addr, known_state)
        if rtype == "exclusion" and holds:
            if tgt in new_incl: new_incl.discard(tgt); changes.append(f"EXCL {src}->%{tgt}")
        elif rtype == "inclusion" and holds:
            if tgt not in new_incl: new_incl.add(tgt); changes.append(f"INCL {src}->+{tgt}")
    return DCRState(state.executed, new_incl, state.pending), changes


# =============================================================================
#  ABI encoding helpers (unchanged from original)
# =============================================================================

def normalize_abi_value(sol_type, value, components=None):
    if sol_type.endswith("[]"):
        inner = sol_type[:-2]
        if value is None: return []
        if not isinstance(value, list): value = [value]
        return [normalize_abi_value(inner, v, components) for v in value]
    if sol_type.startswith("tuple"):
        if components is None: return value
        if isinstance(value, dict):
            vals = [value.get(c["name"]) for c in components]
        else:
            vals = value
        return tuple(
            normalize_abi_value(c["type"], v, c.get("components"))
            for c, v in zip(components, vals)
        )
    if sol_type == "address":
        if value in ("owner", OWNER_ADDR): return to_checksum_address(OWNER_ADDR)
        if value in ("user1", USER_ADDR):  return to_checksum_address(USER_ADDR)
        if value in ("user2", USER2_ADDR): return to_checksum_address(USER2_ADDR)
        # user3..userN — map to hardhat_account(n)
        m = re.match(r"^user(\d+)$", str(value))
        if m:
            idx = int(m.group(1))
            try:    return to_checksum_address(hardhat_account(idx))
            except: return to_checksum_address(USER_ADDR)
        try:    return to_checksum_address(str(value))
        except:
            try:    return to_checksum_address(str(value).lower())
            except: return to_checksum_address(OWNER_ADDR)
    if sol_type == "bool":
        if isinstance(value, bool): return value
        if isinstance(value, str):  return value.lower() == "true"
        return bool(value)
    if sol_type == "string":
        return str(value)
    if sol_type.startswith("bytes"):
        if value is None: return b""
        if isinstance(value, (bytes, bytearray)): return bytes(value)
        if isinstance(value, str):
            if value.startswith("0x"):
                hex_part = value[2:]
                if len(hex_part) % 2: hex_part = "0" + hex_part
                return bytes.fromhex(hex_part)
            return value.encode()
        return bytes(value)
    if sol_type.startswith(("uint", "int")):
        if value is None: return 0
        if isinstance(value, bool): return int(value)
        if isinstance(value, int):  return value
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("0x"): return int(value, 16)
            return int(value)
        return int(value)
    return value


def encode_call(fn_entry, params, value_wei, caller_addr):
    
    inputs    = fn_entry.get("inputs", [])
    types     = [i["type"] for i in inputs]
    sig       = fn_entry["name"] + "(" + ",".join(types) + ")"
    sel       = function_signature_to_4byte_selector(sig)
    resolved  = [normalize_abi_value(inp["type"], v, inp.get("components"))
                 for inp, v in zip(inputs, params)]
    input_hex = "0x" + sel.hex()
    if types: input_hex += abi_encode(types, resolved).hex()
    return {"from": caller_addr, "input": input_hex,
            "value": str(value_wei), "gas": "500000"}


def build_hist_suggestions(transactions_path, abi):
    if not transactions_path or not transactions_path.exists(): return {}
    try: txs = json.loads(transactions_path.read_text())
    except Exception: return {}
    if isinstance(txs, dict): txs = txs.get("result", txs.get("transactions", []))
    
    sel_map = {}
    for e in abi:
        if e.get("type") != "function": continue
        inputs = e.get("inputs", [])
        sig    = e["name"] + "(" + ",".join(i["type"] for i in inputs) + ")"
        sel_map[keccak(sig.encode())[:4].hex()] = e
    suggestions = defaultdict(list)
    for tx in txs:
        calldata = tx.get("input") or tx.get("data") or ""
        if not calldata or len(calldata) < 10: continue
        fn = sel_map.get(calldata[2:10].lower())
        if fn is None: continue
        raw = tx.get("value","0")
        try: v = int(raw,16) if str(raw).startswith("0x") else int(raw)
        except: v = 0
        suggestions[fn["name"]].append({"input":calldata,"value_wei":str(v),
                                         "from":tx.get("from",OWNER_ADDR)})
    return {k: v[:10] for k,v in suggestions.items()}


def branch_requires_zero_value(guard):
    if not guard: return None
    if "msg.value == 0" in guard: return True
    if any(p in guard for p in ["msg.value > 0","msg.value != 0"]): return False
    return None


def _decode_params(fn_entry: dict, params: list | None, value_wei: int) -> dict:
    result: dict = {}
    if params is not None:
        for inp, val in zip(fn_entry.get("inputs", []), params):
            if isinstance(val, (bytes, bytearray)):
                val = "0x" + val.hex()
            result[inp["name"]] = {"type": inp["type"], "value": val}
    if value_wei:
        result["msg.value"] = {"type": "uint256", "value": value_wei,
                               "note": f"{value_wei/1e18:.6f} ETH"}
    return result


def synthesize_params_from_guard(fn_entry, branch_guard, on_chain, known_state,
                                  base_params, rng, llm_client=None):
    if not branch_guard or not base_params: return base_params
    inputs = fn_entry.get("inputs", [])
    if not inputs or llm_client is None: return base_params
    state_ctx   = {**(known_state or {}), **(on_chain or {})}
    state_lines = "\n".join(f"  {k} = {v}" for k,v in sorted(state_ctx.items())
                             if v is not None and str(v) not in ("","None")
                             and not k.startswith("_"))
    param_desc  = "\n".join(f"  {i['name']} ({i['type']}): currently {v}"
                             for i,v in zip(inputs,base_params))
    prompt = (f"Branch guard (must be TRUE):\n  {branch_guard}\n\n"
              f"On-chain state:\n{state_lines}\n\n"
              f"Current params:\n{param_desc}\n\n"
              f"Return ONLY a JSON object mapping param names to adjusted values.\n/no_think")
    try:
        rsp = llm_client.chat.completions.create(
            model=LLM_MODEL, messages=[{"role":"user","content":prompt}],
            max_tokens=256, temperature=0.0)
        raw = _re.sub(r"^```(?:json)?\s*|\s*```$","",
                      rsp.choices[0].message.content.strip()).strip()
        adj = json.loads(raw)
        params = list(base_params)
        for i, inp in enumerate(inputs):
            if inp["name"] in adj and i < len(params):
                val = adj[inp["name"]]
                if inp["type"].startswith(("uint","int")):  params[i] = int(val)
                elif inp["type"] == "bool":
                    params[i] = bool(val) if not isinstance(val,str) else val.lower()=="true"
                elif inp["type"] in ("address","string"): params[i] = str(val)
                else: params[i] = val
        return params
    except Exception: return base_params


def validate_params_against_abi(fn_entry, params):
    try:
        for inp, val in zip(fn_entry.get("inputs", []), params):
            normalize_abi_value(inp["type"], val, inp.get("components"))
        return True
    except Exception:
        return False


def repair_array_lengths(fn_entry, params):
    inputs     = fn_entry.get("inputs", [])
    array_idxs = [i for i, inp in enumerate(inputs) if inp["type"].endswith("[]")]
    if len(array_idxs) < 2: return params
    lengths = [len(params[idx]) for idx in array_idxs if isinstance(params[idx], list)]
    if not lengths: return params
    target_len = min(lengths)
    repaired   = list(params)
    for idx in array_idxs:
        if isinstance(repaired[idx], list):
            repaired[idx] = repaired[idx][:target_len]
    return repaired


def resolve_params(event, fn_entry, call_params, hist, branches, signer_addr,
                   rng, on_chain=None, known_state=None):
    eid      = event["id"]
    fn_name  = fn_entry["name"]
    cp       = call_params.get(eid, {})
    branch   = branches.get(eid)
    req_zero = branch_requires_zero_value(branch["guard"] if branch else "")
    hist_txs = hist.get(fn_name, [])
    if req_zero is True:   hist_txs = [t for t in hist_txs if int(t["value_wei"])==0]
    elif req_zero is False: hist_txs = [t for t in hist_txs if int(t["value_wei"])>0]

    if cp and cp.get("params") is not None:
        value_wei = int(cp.get("value_wei",0))
        if req_zero is True:  value_wei = 0
        if req_zero is False and value_wei == 0: value_wei = 1_000_000_000_000_000
        params = list(cp["params"])
        if on_chain and branch and branch.get("guard"):
            params = synthesize_params_from_guard(fn_entry, branch["guard"], on_chain,
                                                   known_state or {}, params, rng, _llm_client)
            params = repair_array_lengths(fn_entry, params)
            if req_zero is False or (req_zero is None and value_wei > 0):
                price = int(on_chain.get("publicTokenPrice") or
                            (known_state or {}).get("publicTokenPrice","0") or "0")
                inputs  = fn_entry.get("inputs",[])
                qty_idx = next((i for i,inp in enumerate(inputs)
                                if inp["type"].startswith(("uint","int"))
                                and i < len(params)), None)
                if price > 0 and qty_idx is not None:
                    qty = normalize_abi_value(inputs[qty_idx]["type"],
                                              params[qty_idx],
                                              inputs[qty_idx].get("components"))
                    bg = branch.get("guard","")
                    if "maxFreeMintPerWallet" in bg and ">" in bg and "<=" not in bg:
                        max_free = int(on_chain.get("maxFreeMintPerWallet") or
                                       (known_state or {}).get("maxFreeMintPerWallet","1"))
                        claimed  = int(on_chain.get("freeMintClaimed_signer","0"))
                        paid_qty = max(0, qty - max(0, max_free-claimed))
                        value_wei = paid_qty*price if paid_qty>0 else price
                    else:
                        value_wei = qty*price
        if not validate_params_against_abi(fn_entry, params):
            print(f"\n[INVALID ABI PARAMS] fn={fn_name} params={params}")
            return None
        return params, value_wei

    if hist_txs: return None, int(rng.choice(hist_txs)["value_wei"])
    return [], 0


# =============================================================================
#  Coverage-maximising event selection
# =============================================================================

def _ucb_score(event_id: str, success_counts: dict[str, int],
               attempt_counts: dict[str, int], total_steps: int) -> float:
    """Upper Confidence Bound score — untried events get +inf."""
    import math
    n = attempt_counts.get(event_id, 0)
    if n == 0:
        return float("inf")
    exploit = success_counts.get(event_id, 0) / n
    explore = UCB_WEIGHT * math.sqrt(math.log(max(1, total_steps)) / n)
    return exploit + explore


def _select_event(
    dcr: dict,
    dcr_state: DCRState,
    rng: random.Random,
    success_counts: dict[str, int],
    attempt_counts: dict[str, int],
    failure_counts: dict[str, int],
    persistent_failures: dict[str, int],
    total_steps: int,
    max_persistent: int,
) -> str | None:
    """
    Choose the next event to fire.

    Priority:
    1. Pending events (must eventually fire).
    2. Prerequisite events (unblock currently blocked events).
    3. UCB selection among safe enabled events — untried events always
       preferred, then events with best success/attempt ratio + exploration.

    Events with failure_count >= MAX_EVENT_FAILURES are excluded from the
    safe pool (consecutive, resets each cycle).
    Events with persistent_failures >= max_persistent are permanently excluded
    (cumulative across all resets — prevents infinite reset loops).
    Returns None if all events are exhausted.
    """
    enabled      = get_enabled(dcr, dcr_state)
    events_by_id = {e["id"]: e for e in dcr["events"]}

    non_rep_exec = {e["id"] for e in dcr["events"]
                    if not e.get("repeatable", False) and e["id"] in dcr_state.executed}
    ownership_ids = {eid for eid in enabled
                     if events_by_id.get(eid, {}).get("ownership_change", False)}
    destructive   = set(ownership_ids)
    for eid in enabled:
        excl_tgts = [r["to"] for r in dcr["relations"]
                     if r["from"] == eid and r.get("type") == "exclusion"
                     and r["to"] in dcr_state.included
                     and r["to"] not in non_rep_exec]
        if len(excl_tgts) >= 3:
            destructive.add(eid)

    def _ok(eid: str) -> bool:
        return (failure_counts.get(eid, 0) < MAX_EVENT_FAILURES
                and persistent_failures.get(eid, 0) < max_persistent)

    safe_enabled = [e for e in enabled if e not in destructive and _ok(e)]
    if not safe_enabled:
        safe_enabled = [e for e in enabled if _ok(e)]
    if not safe_enabled:
        return None

    def _best_ucb(pool: list[str]) -> str:
        best_id, best_s = None, -1.0
        for eid in pool:
            s = _ucb_score(eid, success_counts, attempt_counts, total_steps)
            if s > best_s or (s == best_s and rng.random() < 0.5):
                best_s, best_id = s, eid
        return best_id

    pending_en = [e for e in safe_enabled if e in dcr_state.pending]
    if pending_en and rng.random() < PENDING_BIAS:
        return _best_ucb(pending_en)

    blocked_cond = {rel["from"] for rel in dcr["relations"]
                    if rel.get("type") == "condition"
                    and rel["to"] in dcr_state.included
                    and rel["from"] not in dcr_state.executed
                    and rel["from"] in enabled}
    safe_prereqs = [e for e in blocked_cond if e not in destructive and _ok(e)]
    if safe_prereqs and rng.random() < 0.8:
        return _best_ucb(safe_prereqs)

    return _best_ucb(safe_enabled)


# =============================================================================
#  Main sequence generation
# =============================================================================

def generate_sequences_hardhat(address, dcr, server, fqn, abi, source_text,
                                abi_index, hist, seed, max_steps):
    known_state  = dcr.get("known_state", {})
    call_params  = dcr.get("call_params", {})
    branches_map = {b["branch_id"]: b for b in dcr.get("branches", [])}
    events_by_id = {e["id"]: e for e in dcr["events"]}
    state_vars   = extract_state_variables_from_guards(dcr)
    print(f"    State vars from guards: {list(state_vars.keys())}")

    all_txs    = []
    block_num  = 1
    rng        = random.Random(seed)
    dcr_state  = initial_state(dcr)
    signer_idx = 0
    steps_ok   = 0
    reset_count = 0
    user_signers_default = hardhat_accounts(1, 2)   # [account[1]] — single stable user
    user_signers_multi   = hardhat_accounts(1, 5)   # [account[1..4]] — 4 distinct users

    # Coverage tracking
    success_counts:       dict[str, int] = defaultdict(int)
    attempt_counts:       dict[str, int] = defaultdict(int)
    failure_counts:       dict[str, int] = defaultdict(int)  # consecutive, resets each cycle
    persistent_failures:  dict[str, int] = defaultdict(int)  # cumulative, never reset
    successful_functions: set[str]       = set()
    successful_events:    set[str]       = set()
    successful_guards:    set[str]       = set()
    attempted_functions:  set[str]       = set()
    # Events that have permanently failed too many times across all resets
    MAX_PERSISTENT_FAILURES = MAX_EVENT_FAILURES * 3

    # Deploy once
    deploy_res = server.deploy(fqn=fqn, abi=abi, source_text=source_text,
                               address=address, dataset_dir=DATASET_DIR,
                               llm_client=_llm_client, llm_model=LLM_MODEL)
    print(f"    Deployed: {deploy_res['address'][:16]}  "
          f"args_src={deploy_res.get('args_source','?')}  "
          f"slots={deploy_res.get('deploy_slots',0)}")

    state_res = server.send({"read_state": {"signer": OWNER_ADDR, "state_vars": state_vars}})
    on_chain  = state_res.get("state", {})
    dcr_state, _ = apply_guard_filters(dcr, dcr_state, on_chain, OWNER_ADDR,
                                        fired_event=None, known_state=known_state)

    # Stall detection: if no success in the last STALL_WINDOW attempts, reset.
    # This handles contracts where all enabled events revert due to on-chain
    # state (e.g. transferFrom with no allowance) and UCB alone can't escape.
    STALL_WINDOW = max(20, len(dcr["events"]) * 4)
    stall_counter = 0   # attempts since last success
    steps_ok_at_last_reset = 0  # to detect zero-progress stall cycles
    stop_loop = False

    def _all_banned() -> bool:
        return all(persistent_failures.get(e["id"], 0) >= MAX_PERSISTENT_FAILURES
                   for e in dcr["events"])

    # ── Main loop ─────────────────────────────────────────────────────────────
    while steps_ok < max_steps and not stop_loop:
        if _all_banned():
            print(f"    [stop] all events permanently banned, exiting early")
            stop_loop = True
            break

        event_id = _select_event(dcr, dcr_state, rng,
                                  success_counts, attempt_counts,
                                  failure_counts, persistent_failures,
                                  total_steps=steps_ok + 1,
                                  max_persistent=MAX_PERSISTENT_FAILURES)

        if event_id is None:
            if _all_banned():
                print(f"    [stop] all events permanently banned, exiting early")
                stop_loop = True
                break
            # Otherwise reset and try again (structural failures, not all banned)
            reset_count  += 1
            if reset_count >= MAX_RESETS:
                print(f"    [stop] reached MAX_RESETS={MAX_RESETS}, saving {len(all_txs)} txs and moving on")
                stop_loop = True
                break
            failure_counts.clear()
            rng       = random.Random(seed + reset_count * 1000)
            dcr_state = initial_state(dcr)
            res = server.send({"reset_storage": True})
            if not res.get("ok"):
                print(f"    [warn] reset failed: {res.get('error')}")
            state_res = server.send({"read_state": {"signer": OWNER_ADDR,
                                                     "state_vars": state_vars}})
            on_chain  = state_res.get("state", {})
            dcr_state, _ = apply_guard_filters(dcr, dcr_state, on_chain, OWNER_ADDR,
                                                fired_event=None, known_state=known_state)
            print(f"    [reset #{reset_count}] failures cleared, DCR reset")
            continue

        # Also reset if DCR is simply exhausted (get_enabled returns [])
        if not get_enabled(dcr, dcr_state):
            if _all_banned():
                print(f"    [stop] all events permanently banned, exiting early")
                stop_loop = True
                break
            reset_count += 1
            if reset_count >= MAX_RESETS:
                print(f"    [stop] reached MAX_RESETS={MAX_RESETS}, saving {len(all_txs)} txs and moving on")
                stop_loop = True
                break
            rng       = random.Random(seed + reset_count * 1000)
            dcr_state = initial_state(dcr)
            res = server.send({"reset_storage": True})
            if not res.get("ok"):
                print(f"    [warn] reset failed: {res.get('error')}")
            state_res = server.send({"read_state": {"signer": OWNER_ADDR,
                                                     "state_vars": state_vars}})
            on_chain  = state_res.get("state", {})
            dcr_state, _ = apply_guard_filters(dcr, dcr_state, on_chain, OWNER_ADDR,
                                                fired_event=None, known_state=known_state)
            print(f"    [reset #{reset_count}] DCR exhausted, continuing")
            continue

        event    = events_by_id[event_id]
        fn_name  = event.get("function_name", event_id)
        attempted_functions.add(fn_name)

        fn_entry = abi_index.get(fn_name)
        if fn_entry is None:
            print(f"      [warn] {fn_name} not in ABI, skipping")
            attempt_counts[event_id] += 1
            failure_counts[event_id]    += 1
            persistent_failures[event_id] += 1
            dcr_state = fire_dcr(dcr, dcr_state, event_id)
            continue

        is_multi  = event.get("multi_user", False)
        pool      = user_signers_multi if is_multi else user_signers_default
        signer_addr = (OWNER_ADDR if event["role"] == "owner"
                       else pool[signer_idx % len(pool)])
        if event["role"] != "owner":
            signer_idx += 1

        state_res = server.send({"read_state": {"signer": signer_addr,
                                                 "state_vars": state_vars}})
        on_chain  = state_res.get("state", {})

        param_result = resolve_params(event, fn_entry, call_params, hist, branches_map,
                                      signer_addr, rng, on_chain, known_state)
        if param_result is None:
            attempt_counts[event_id] += 1
            failure_counts[event_id]    += 1
            persistent_failures[event_id] += 1
            dcr_state = fire_dcr(dcr, dcr_state, event_id)
            continue
        params, value_wei = param_result

        branch       = branches_map.get(event_id)
        branch_guard = branch["guard"] if branch else ""
        param_source = "unknown"
        try:
            if params is None:
                hist_txs = hist.get(fn_name, [])
                req_zero = branch_requires_zero_value(branch_guard)
                if req_zero is True:    hist_txs = [t for t in hist_txs if int(t["value_wei"])==0]
                elif req_zero is False: hist_txs = [t for t in hist_txs if int(t["value_wei"])>0]
                if not hist_txs:
                    dcr_state = fire_dcr(dcr, dcr_state, event_id)
                    continue
                t  = rng.choice(hist_txs)
                tx = {"from": signer_addr, "input": t["input"],
                      "value": t["value_wei"], "gas": "500000"}
                param_source = "historical"
            else:
                tx = encode_call(fn_entry, params, value_wei, signer_addr)
                cp   = call_params.get(event_id, {})
                base = list(cp.get("params", []))
                if on_chain and branch and params != base: param_source = "guard_synthesis"
                elif _llm_client and on_chain and branch:  param_source = "llm_synthesis"
                else:                                       param_source = "dcr_call_params"
        except Exception as e:
            print(f"      [warn] encode failed for {fn_name}: {e}")
            attempt_counts[event_id] += 1
            failure_counts[event_id]    += 1
            persistent_failures[event_id] += 1
            dcr_state = fire_dcr(dcr, dcr_state, event_id)
            continue

        attempt_counts[event_id] += 1
        stall_counter += 1
        result = server.send({"tx": tx})
        status = result.get("status")

        if status == "success":
            stall_counter = 0
            success_counts[event_id] += 1
            failure_counts[event_id]  = 0
            successful_functions.add(fn_name)
            successful_events.add(event_id)
            if branch_guard:
                successful_guards.add(branch_guard)

            state_res = server.send({"read_state": {"signer": signer_addr,
                                                     "state_vars": state_vars}})
            on_chain  = state_res.get("state", {})
            dcr_state = fire_dcr(dcr, dcr_state, event_id, on_chain, signer_addr, known_state)
            dcr_state, _ = apply_guard_filters(dcr, dcr_state, on_chain, signer_addr,
                                                fired_event=event_id, known_state=known_state)
            role_tag      = "owner" if event["role"] == "owner" else "user "
            val_eth       = f"{int(tx['value'])/1e18:.4f}E" if int(tx["value"]) > 0 else "0E"
            guard_display = f"  guard={branch_guard!r}" if branch_guard else ""
            cov_tag       = f"  [cov={len(successful_functions)}/{len(abi_index)}]"
            print(f"      [ok]   {event_id:<28} {role_tag}  "
                  f"params={params if params is not None else ['<raw>']}  "
                  f"val={val_eth}  src={param_source}{guard_display}{cov_tag}")

            fn_inputs = fn_entry.get("inputs", [])
            fn_sig    = fn_name + "(" + ",".join(i["type"] for i in fn_inputs) + ")"
            server.record_successful_call(
                address=address, dataset_dir=DATASET_DIR,
                fn_name=fn_name, fn_sig=fn_sig, from_addr=signer_addr,
                params=params if params is not None else [],
                value_wei=int(tx["value"]), param_source=param_source,
                event_id=event_id, branch_guard=branch_guard,
                sequence_idx=0, seed=seed,
            )
            vtx = {
                "hash":             "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:16],
                "blockNumber":      str(block_num),
                "timeStamp":        str(1_700_000_000 + block_num * 12),
                "from":             signer_addr,
                "to":               address,
                "value":            tx["value"],
                "input":            tx["input"],
                "methodId":         tx["input"][:10],
                "functionName":     fn_name,
                "txreceipt_status": "1",
                "isError":          "0",
                "gas":              "500000",
                "gasPrice":         "1000000000",
                "gasUsed":          "100000",
                "nonce":            str(block_num),
                "transactionIndex": "0",
                "contractAddress":  "",
                "cumulativeGasUsed":"100000",
                "confirmations":    "100",
                "_sequence_idx":    0,
                "_event_id":        event_id,
                "_param_source":    param_source,
                "_branch_guard":    branch_guard,
                "_fn_sig":          fn_sig,
                "_decoded_params":  _decode_params(fn_entry, params, int(tx["value"])),
            }
            all_txs.append(vtx)
            block_num += 1
            steps_ok  += 1
        else:
            # On-chain revert: state-dependent, not a structural failure.
            # Only attempt_counts (already incremented above) reduces the UCB
            # score — failure_counts is NOT touched so the event is never
            # permanently excluded for revert reasons alone.
            print(f"      [skip] {event_id:<28} "
                  f"reverted: {result.get('revert_reason','')[:80]}")

        # Stall detection: too many consecutive failures → reset storage/state
        # so that state-dependent events get a fresh chance.
        if stall_counter >= STALL_WINDOW:
            # If steps_ok hasn't grown since the last reset, this contract is
            # stuck regardless of storage state — ban all stalling events and
            # break out so we save what we have.
            no_progress = (steps_ok == steps_ok_at_last_reset)

            reset_count  += 1
            if reset_count >= MAX_RESETS:
                print(f"    [stop] reached MAX_RESETS={MAX_RESETS}, saving {len(all_txs)} txs and moving on")
                stop_loop = True
                break
            stall_counter = 0
            steps_ok_at_last_reset = steps_ok
            failure_counts.clear()
            rng       = random.Random(seed + reset_count * 1000)
            dcr_state = initial_state(dcr)
            server.send({"reset_storage": True})
            state_res = server.send({"read_state": {"signer": OWNER_ADDR,
                                                     "state_vars": state_vars}})
            on_chain  = state_res.get("state", {})
            dcr_state, _ = apply_guard_filters(dcr, dcr_state, on_chain, OWNER_ADDR,
                                                fired_event=None, known_state=known_state)
            # Permanently ban events attempted many times with zero successes.
            for eid, n_attempts in attempt_counts.items():
                if success_counts.get(eid, 0) == 0 and n_attempts >= STALL_WINDOW:
                    persistent_failures[eid] = MAX_PERSISTENT_FAILURES
            if no_progress:
                # steps_ok didn't grow at all in the last stall window —
                # the contract is stuck in its current state. Ban every
                # currently-enabled event so the loop exits cleanly.
                for eid in get_enabled(dcr, dcr_state) or [e["id"] for e in dcr["events"]]:
                    persistent_failures[eid] = MAX_PERSISTENT_FAILURES
                # Also ban anything that was never reached
                for e in dcr["events"]:
                    if attempt_counts.get(e["id"], 0) == 0:
                        persistent_failures[e["id"]] = MAX_PERSISTENT_FAILURES
                print(f"    [stall reset #{reset_count}] no progress — banning unreachable events")
            else:
                print(f"    [stall reset #{reset_count}] no progress in {STALL_WINDOW} attempts")
            # Check immediately after banning — don't wait for next iteration
            if _all_banned():
                print(f"    [stop] all events permanently banned, exiting early")
                stop_loop = True

    # ── Drain phase: ownership_change events in drain_priority order ──────────
    # Collect all ownership_change events that are still fireable,
    # sorted by drain_priority (ascending: lower = earlier).
    # Events with drain_priority=0 are treated as priority=10 if
    # ownership_change=True and no explicit priority is set.
    drain_candidates = []
    for event in dcr["events"]:
        eid = event["id"]
        if not event.get("ownership_change", False): continue
        if eid not in dcr_state.included:            continue
        if eid in dcr_state.executed and not event.get("repeatable", False): continue
        if eid not in get_enabled(dcr, dcr_state):  continue
        fn_name  = event.get("function_name", eid)
        fn_entry = abi_index.get(fn_name)
        if fn_entry is None: continue
        # Default priority for ownership_change events without explicit field:
        # renounceOwnership → 99, everything else → 10
        default_p = 99 if "renounce" in fn_name.lower() else 10
        priority  = int(event.get("drain_priority", default_p))
        drain_candidates.append((priority, event, fn_entry))

    drain_candidates.sort(key=lambda x: x[0])

    for _, event, fn_entry in drain_candidates:
        eid       = event["id"]
        fn_name   = event.get("function_name", eid)
        cp        = call_params.get(eid, {})
        params    = cp.get("params", [])
        value_wei = int(cp.get("value_wei", 0))
        try:
            tx  = encode_call(fn_entry, params, value_wei, OWNER_ADDR)
            res = server.send({"tx": tx})
            if res.get("status") == "success":
                fn_inputs = fn_entry.get("inputs", [])
                fn_sig    = fn_name + "(" + ",".join(i["type"] for i in fn_inputs) + ")"
                server.record_successful_call(
                    address=address, dataset_dir=DATASET_DIR,
                    fn_name=fn_name, fn_sig=fn_sig, from_addr=OWNER_ADDR,
                    params=params, value_wei=value_wei, param_source="dcr_call_params",
                    event_id=eid, branch_guard="", sequence_idx=0, seed=seed,
                )
                vtx = {
                    "hash":             "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:16],
                    "blockNumber":      str(block_num),
                    "timeStamp":        str(1_700_000_000 + block_num * 12),
                    "from":             OWNER_ADDR,
                    "to":               address,
                    "value":            str(value_wei),
                    "input":            tx["input"],
                    "methodId":         tx["input"][:10],
                    "functionName":     fn_name,
                    "txreceipt_status": "1",
                    "isError":          "0",
                    "gas":              "500000",
                    "gasPrice":         "1000000000",
                    "gasUsed":          "100000",
                    "nonce":            str(block_num),
                    "transactionIndex": "0",
                    "contractAddress":  "",
                    "cumulativeGasUsed":"100000",
                    "confirmations":    "100",
                    "_sequence_idx":    0,
                    "_event_id":        eid,
                    "_param_source":    "dcr_call_params",
                    "_branch_guard":    "",
                    "_fn_sig":          fn_sig,
                    "_decoded_params":  _decode_params(fn_entry, params, value_wei),
                }
                all_txs.append(vtx)
                block_num += 1
                successful_functions.add(fn_name)
                successful_events.add(eid)
                # Update DCR state so subsequent drain events see the new state
                dcr_state = fire_dcr(dcr, dcr_state, eid, known_state=known_state)
                print(f"      [drain p={event.get('drain_priority', '?')}] "
                      f"{fn_name}  [cov={len(successful_functions)}/{len(abi_index)}]")
            else:
                reason = (res.get("revert_reason") or res.get("error") or "")[:60]
                print(f"      [drain skip] {fn_name}  reverted: {reason}")
        except Exception as e:
            print(f"      [drain error] {fn_name}: {e}")

    accepted = len(dcr_state.pending) == 0
    print(f"    seed={seed}  [{'ok' if accepted else '!!'}]  "
          f"steps_ok={steps_ok}  resets={reset_count}  "
          f"txs={len(all_txs)}  pending={sorted(dcr_state.pending)}")

    total_abi_functions = {
        e["name"] for e in abi
        if e.get("type") == "function"
        and e.get("stateMutability", "nonpayable") not in ("view", "pure")
    }
    coverage = {
        "function_coverage": {
            "successful":           len(successful_functions),
            "attempted":            len(attempted_functions),
            "total_abi":            len(total_abi_functions),
            "coverage_ratio":       round(len(successful_functions) /
                                          max(1, len(total_abi_functions)), 4),
            "successful_functions": sorted(successful_functions),
            "attempted_functions":  sorted(attempted_functions),
        },
        "dcr_event_coverage": {
            "successful":    len(successful_events),
            "attempted":     len(attempt_counts),
            "total_events":  len(dcr["events"]),
            "coverage_ratio": round(len(successful_events) /
                                    max(1, len(dcr["events"])), 4),
            "successful_events": sorted(successful_events),
        },
        "guard_coverage": {
            "successful_guards": len(successful_guards),
            "guards":            sorted(successful_guards),
        },
        "ucb_stats": {
            "success_counts": dict(success_counts),
            "attempt_counts": dict(attempt_counts),
        },
    }

    seq_metadata = [{"seed": seed, "seq_idx": 0, "accepted": accepted,
                      "final_pending": sorted(dcr_state.pending),
                      "steps_ok": steps_ok, "dcr_resets": reset_count,
                      "txs": len(all_txs)}]
    return all_txs, seq_metadata, coverage


# =============================================================================
#  Per-contract processing
# =============================================================================

def process_contract(address):
    import re
    import subprocess
    dcr_file  = GENERATED_DIR / address / DCR_FILENAME
    meta_file = DATASET_DIR   / address / "metadata.json"
    abi_file  = DATASET_DIR   / address / "abi.json"
    hist_file = DATASET_DIR   / address / "transactions.json"
 
    for f, name in [(dcr_file, DCR_FILENAME), (meta_file, "metadata.json"),
                    (abi_file, "abi.json")]:
        if not f.exists():
            print(f"  [skip] missing {name}")
            return
 
    source_path = None
    for name in ("source.sol", "source.json"):
        p = DATASET_DIR / address / name
        if p.exists():
            source_path = p
            break
    if not source_path:
        print("  [skip] no source")
        return
 
    synth_path = GENERATED_DIR / address / "transactions_synthetic.json"
    if synth_path.exists():
        try:
            existing_txs = json.loads(synth_path.read_text())
            n = len(existing_txs)
            if n >= MAX_STEPS:
                print(f"  [skip] already have {n} txs >= MAX_STEPS={MAX_STEPS}")
                return
            seq_path = GENERATED_DIR / address / "sequences_hardhat.json"
            cov_path = GENERATED_DIR / address / "coverage.json"
            if seq_path.exists():
                seq = json.loads(seq_path.read_text())
                if seq and seq[0].get("dcr_resets", 0) >= MAX_RESETS:
                    print(f"  [skip] already hit MAX_RESETS with {n} txs — structurally limited")
                    return
            if cov_path.exists():
                try:
                    cov = json.loads(cov_path.read_text())
                    if cov.get("dcr_event_coverage", {}).get("successful", -1) == 0:
                        print(f"  [skip] 0 successful events in coverage.json — structurally impossible")
                        return
                except Exception:
                    pass
            if not seq_path.exists():
                print(f"  [resume] found {n} txs, need {MAX_STEPS}")
        except Exception:
            pass
 
    dcr           = json.loads(dcr_file.read_text())
    meta          = json.loads(meta_file.read_text())
    abi           = json.loads(abi_file.read_text())
    contract_name = meta.get("contract_name", "Contract")
    abi_index     = {
        e["name"]: e for e in abi
        if e.get("type") == "function"
        and e.get("stateMutability", "nonpayable") not in ("view", "pure")
    }
    hist       = build_hist_suggestions(hist_file, abi)
    fork_block = pick_fork_block(address, DATASET_DIR)
 
    print(f"  {contract_name}  fork_block={fork_block}")
 
    # ── Shared compile helper using deploy_replay's own pipeline ─────────────
    # This is identical to what sanity_replay.py uses and correctly handles
    # pragma/version mismatches via overrides: {} in hardhat.config.js.
    
 
    def _compile_with_deploy_replay(work_dir, src_path, contract_meta) -> tuple[bool, str]:
        """Compile using deploy_replay config but hardhat_utils deps (has OZ versioning)."""
        hu_wipe_hardhat_artifacts(work_dir)
        ok, errs = hu_prepare_and_compile(src_path, contract_meta, work_dir)
        if not ok:
            # Check if the failure is OZ version mismatch (OZ5 API used with OZ4 installed)
            # Symptoms: Ownable(msg.sender), _update override, super._update
            src_text = (
                src_path.read_text() if src_path.suffix == ".sol"
                else "\n".join(
                    v.get("content", "")
                    for v in json.loads(src_path.read_text()).get("sources", {}).values()
                )
            )
            oz5_patterns = [
                r'Ownable\s*\(\s*\w+\s*\)',   # Ownable(owner) — OZ5 constructor
                r'super\._update\s*\(',        # super._update() — OZ5 ERC20
                r'function\s+_update.*override',  # _update override — OZ5
            ]
            needs_oz5 = any(_re.search(p, src_text) for p in oz5_patterns)
            if needs_oz5 and "@openzeppelin/contracts@4" not in errs and "override" in errs:
                # Force install OZ5 and retry
                nm = work_dir / "node_modules" / "@openzeppelin" / "contracts"
                if nm.exists():
                    import shutil
                    shutil.rmtree(nm)
                nm_up = work_dir / "node_modules" / "@openzeppelin" / "contracts-upgradeable"
                if nm_up.exists():
                    import shutil
                    shutil.rmtree(nm_up)
                subprocess.run(
                    ["npm", "install", "--save-dev",
                     "@openzeppelin/contracts@5",
                     "@openzeppelin/contracts-upgradeable@5"],
                    cwd=work_dir, capture_output=True, text=True,
                )
                ok2, errs2 = hu_prepare_and_compile(src_path, contract_meta, work_dir)
                if ok2:
                    return True, ""
                errs = errs2  # fall through to overrides retry with OZ5 installed

            # Overwrite config with deploy_replay's version (adds overrides: {}) and retry
            remaps = _dr_detect_remappings(work_dir, src_path)
            (work_dir / "hardhat.config.js").write_text(
                _dr_hardhat_config(contract_meta, work_dir,
                                   remaps or None, source_text=src_text)
            )
            cp = subprocess.run(
                ["npx", "hardhat", "compile", "--force"],
                cwd=work_dir, capture_output=True, text=True,
            )
            return cp.returncode == 0, cp.stderr
        return True, ""
 
    def _fqn_for(src_path, cname) -> str:
        """Compute FQN, finding the file that actually contains the contract."""
        if src_path.suffix == ".sol":
            return f"contracts/{src_path.name}:{cname}"
        try:
            sources = json.loads(src_path.read_text()).get("sources", {})
            for fp, fc in sources.items():
                if re.search(rf'\bcontract\s+{re.escape(cname)}\b',
                             fc.get("content", "")):
                    return f"contracts/{fp}:{cname}"
            # fallback: first file
            first = next(iter(sources), None)
            if first:
                return f"contracts/{first}:{cname}"
        except Exception:
            pass
        return cname
 
    # ── Compile ───────────────────────────────────────────────────────────────
    print("  Compiling ...", end="  ", flush=True)
    ok, errs = _compile_with_deploy_replay(WORK_DIR, source_path, meta)
    if not ok:
        print("FAILED")
        print("=" * 80)
        print(errs)
        print("=" * 80)
        return
    print("OK")
 
    # write_source copies files into workspace and returns (fqn, source_text)
    # but its FQN picks the first file which may be an import — fix it below
    fqn, source_text = write_source(WORK_DIR, source_path, contract_name)
    #fqn, source_text = _dr_write_source(WORK_DIR, source_path)
    fqn = _fqn_for(source_path, contract_name)
 
    # ── Pre-synthesize constructor args ───────────────────────────────────────
    deploy_params_file = DATASET_DIR / address / "deploy_params.json"
    if not deploy_params_file.exists():
        ctor     = next((e for e in abi if e.get("type") == "constructor"), None)
        ctor_hex = meta.get("constructor_arguments", "") or meta.get("ConstructorArguments", "")
        args     = None
        args_source = "unknown"
 
        # Priority 1: Etherscan constructor_arguments (always correct)
        if ctor_hex and ctor and ctor.get("inputs"):
            decoded = _decode_constructor_args_from_hex(ctor_hex, ctor["inputs"])
            if decoded is not None:
                coerced = []
                for inp, val in zip(ctor["inputs"], decoded):
                    if inp["type"] == "address":
                        try:
                            coerced.append(to_checksum_address(val))
                        except Exception:
                            coerced.append(str(val))
                    elif isinstance(val, (bytes, bytearray)):
                        coerced.append("0x" + val.hex())
                    else:
                        coerced.append(val)
                args        = coerced
                args_source = "etherscan"
                print(f"  Constructor args (etherscan): {args}")
 
        # Priority 2: LLM synthesis from deploy_replay
        if args is None:
            try:
                synth = _synth_args(
                    abi=abi, source_text=source_text,
                    contract_name=contract_name,
                    llm_client=_llm_client, llm_model=LLM_MODEL,
                )
                if synth:
                    args        = synth
                    args_source = "llm_synthesis"
                    print(f"  Pre-synthesized constructor args: {args}")
            except Exception as e:
                print(f"  [warn] pre-synthesis failed: {e}")
 
        # Priority 3: owner-aware heuristic fallback
        if args is None and ctor and ctor.get("inputs"):
            import re as _re2
            DURATION_RE = _re2.compile(
                r"(duration|period|time|delay|expiry|interval)", _re2.IGNORECASE
            )
            args = []
            for inp in ctor["inputs"]:
                t, name = inp["type"], inp.get("name", "")
                if t == "address":
                    args.append(OWNER_ADDR)
                elif t.startswith(("uint", "int")):
                    args.append(86400 if DURATION_RE.search(name) else 0)
                elif t == "bool":
                    args.append(False)
                elif t == "string":
                    args.append("")
                else:
                    args.append(0)
            args_source = "heuristic"
            print(f"  Pre-synthesized args (heuristic): {args}")
 
        if args is not None:
            deploy_params_file.write_text(json.dumps({
                "constructor_args": args,
                "args_source":      args_source,
            }, indent=2))
 
    # ── Deploy + generate sequences (with HH700 retry) ────────────────────────
    for attempt in range(2):
        server = HardhatServer(WORK_DIR)
        try:
            all_txs, seq_meta, coverage = generate_sequences_hardhat(
                address=address, dcr=dcr, server=server, fqn=fqn, abi=abi,
                source_text=source_text, abi_index=abi_index, hist=hist,
                seed=SEED, max_steps=MAX_STEPS,
            )
            break  # success
        except DeployFailedError as e:
            server.close()
            err_str = str(e)
            if "HH700" in err_str and attempt == 0:
                # Stale artifact — wipe and recompile using deploy_replay pipeline
                print(f"  [warn] HH700 stale artifact — wiping and retrying")
                hu_wipe_hardhat_artifacts(WORK_DIR)
                deploy_params_file = DATASET_DIR / address / "deploy_params.json"
                if deploy_params_file.exists():
                    deploy_params_file.unlink()
                print("  Recompiling ...", end="  ", flush=True)
                ok, errs = _compile_with_deploy_replay(WORK_DIR, source_path, meta)
                if not ok:
                    print(f"FAILED\n{errs[-400:]}")
                    return
                print("OK")
                fqn, source_text = write_source(WORK_DIR, source_path, contract_name)
                #fqn, source_text = _dr_write_source(WORK_DIR, source_path)
                fqn = _fqn_for(source_path, contract_name)
                continue
            elif "Minimum" in err_str and attempt == 0:
                # Constructor requires more array elements — expand address arrays
                # using additional Hardhat accounts and retry
                deploy_params_file = DATASET_DIR / address / "deploy_params.json"
                if deploy_params_file.exists():
                    try:
                        dp = json.loads(deploy_params_file.read_text())
                        args = dp.get("constructor_args", [])
                        # Find address[] params and expand to 5 hardhat accounts
                        all_accounts = [
                            "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
                            "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
                            "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",
                            "0x90F79bf6EB2c4f870365E785982E1f101E93b906",
                            "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65",
                        ]
                        new_args = []
                        for arg in args:
                            if isinstance(arg, list) and all(
                                isinstance(a, str) and a.startswith("0x") for a in arg
                            ):
                                new_args.append(all_accounts[:5])
                            else:
                                new_args.append(arg)
                        dp["constructor_args"] = new_args
                        dp["args_verified"] = False  # allow re-attempt
                        deploy_params_file.write_text(json.dumps(dp, indent=2))
                        print(f"  [retry] expanded address array to 5 accounts")
                    except Exception:
                        pass
                continue
            else:
                print(f"  ERROR: {e}")
                return
        finally:
            server.close()
    else:
        print(f"  ERROR: deploy failed after artifact wipe, skipping")
        return
 
    out_dir = GENERATED_DIR / address
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "transactions_synthetic.json").write_text(json.dumps(all_txs, indent=2))
    (out_dir / "sequences_hardhat.json").write_text(json.dumps(seq_meta, indent=2))
    (out_dir / "coverage.json").write_text(json.dumps(coverage, indent=2))
 
    fcov = coverage["function_coverage"]
    print(f"  -> function coverage: "
          f"{fcov['successful']}/{fcov['total_abi']} ({fcov['coverage_ratio']:.2%})")
    accepted = sum(1 for s in seq_meta if s["accepted"])
    print(f"  -> {len(all_txs)} validated txs  accepted={accepted}/{len(seq_meta)}")


# =============================================================================
#  Entry point
# =============================================================================

def main():
    addresses = sys.argv[1:] if len(sys.argv) > 1 else None

    #addresses = ["0x0001a500a6b18995b03f44bb040a5ffc28e45cb0"]

    if addresses:
        dirs = [GENERATED_DIR / a.lower()
                for a in addresses
                if (GENERATED_DIR / a.lower()).is_dir()]
    else:
        dirs = [d for d in sorted(GENERATED_DIR.iterdir())
                if d.is_dir() and (d / DCR_FILENAME).exists()]
    if not dirs:
        print(f"No contracts found in {GENERATED_DIR}")
        return
    setup_workspace(WORK_DIR)
    print(f"\nGenerating validated sequences for {len(dirs)} contracts\n")
    for i, d in enumerate(dirs, 1):
        print(f"[{i}/{len(dirs)}] {d.name}")
        try:
            process_contract(d.name)
        except Exception as e:
            print(f"  ERROR: {e}")
        print()


if __name__ == "__main__":
    main()