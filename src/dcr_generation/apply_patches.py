"""
Patch make_sequences_hardhat.py to add Etherscan constructor arg decoding
as Priority 1 before LLM synthesis and heuristic fallback.
"""
import sys
from pathlib import Path

target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("make_sequences_hardhat.py")
src = target.read_text()
n = 0

# ── PATCH 1: add _decode_constructor_args_from_hex after imports ─────────────
# Insert after the line: from hardhat_server import (
ANCHOR = "UCB_WEIGHT       = float(os.getenv(\"UCB_WEIGHT\",        \"2.0\"))"
INSERT = '''
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


'''
if ANCHOR in src and "_decode_constructor_args_from_hex" not in src:
    src = src.replace(ANCHOR, INSERT + ANCHOR, 1)
    n += 1
    print("  ✓ patch 1: added _decode_constructor_args_from_hex")
else:
    print("  ✗ patch 1 skipped (anchor not found or already applied)")

# ── PATCH 2: replace the entire pre-synthesis block in process_contract ──────
OLD_BLOCK = '''    deploy_params_file = DATASET_DIR / address / "deploy_params.json"
    if not deploy_params_file.exists():
        try:
            args = _synth_args(
                abi=abi,
                source_text=source_text,
                contract_name=contract_name,
                llm_client=_llm_client,
                llm_model=LLM_MODEL,
            )
            if args:
                deploy_params_file.write_text(json.dumps({
                    "constructor_args": args,
                    "args_source": "pre_synthesized",
                }, indent=2))
                print(f"  Pre-synthesized constructor args: {args}")
        except Exception as e:
            print(f"  [warn] pre-synthesis failed: {e}")

    deploy_params_file = DATASET_DIR / address / "deploy_params.json"
    if not deploy_params_file.exists():
        ctor = next((e for e in abi if e.get("type") == "constructor"), None)
        if ctor and ctor.get("inputs"):
            import re as _re
            DURATION_RE = _re.compile(
                r"(duration|period|time|delay|expiry|interval)", _re.IGNORECASE
            )
            args = []
            for inp in ctor["inputs"]:
                t    = inp["type"]
                name = inp.get("name", "")
                if t == "address":
                    # OWNER_ADDR for ALL address params — address(0) triggers
                    # "mint to zero address", "invalid receiver", ZeroL1Council, etc.
                    args.append(OWNER_ADDR)
                elif t.startswith(("uint", "int")):
                    # Duration/period params need non-zero values
                    args.append(86400 if DURATION_RE.search(name) else 0)
                elif t == "bool":
                    args.append(False)
                elif t in ("string",):
                    args.append("")
                else:
                    args.append(0)
            deploy_params_file.write_text(json.dumps({
                "constructor_args": args,
                "args_source": "pre_synthesized_owner_aware",
            }, indent=2))
            print(f"  Pre-synthesized args (owner-aware): {args}")'''

NEW_BLOCK = '''    deploy_params_file = DATASET_DIR / address / "deploy_params.json"
    if not deploy_params_file.exists():
        ctor     = next((e for e in abi if e.get("type") == "constructor"), None)
        ctor_hex = meta.get("constructor_arguments", "") or meta.get("ConstructorArguments", "")
        args     = None
        args_source = "unknown"

        # ── Priority 1: decode from Etherscan constructor_arguments ──────────
        # This is always correct — it's the exact args used on mainnet.
        if ctor_hex and ctor and ctor.get("inputs"):
            decoded = _decode_constructor_args_from_hex(ctor_hex, ctor["inputs"])
            if decoded is not None:
                # Convert bytes/HexBytes to checksum addresses where needed
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

        # ── Priority 2: LLM / deploy_replay synthesis ────────────────────────
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

        # ── Priority 3: owner-aware heuristic fallback ────────────────────────
        if args is None and ctor and ctor.get("inputs"):
            import re as _re2
            DURATION_RE = _re2.compile(
                r"(duration|period|time|delay|expiry|interval)", _re2.IGNORECASE
            )
            args = []
            for inp in ctor["inputs"]:
                t, name = inp["type"], inp.get("name", "")
                if t == "address":
                    # OWNER_ADDR for ALL address params — address(0) triggers
                    # "mint to zero address", "invalid receiver", ZeroL1Council, etc.
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
            }, indent=2))'''

if OLD_BLOCK in src:
    src = src.replace(OLD_BLOCK, NEW_BLOCK, 1)
    n += 1
    print("  ✓ patch 2: replaced pre-synthesis block with 3-priority logic")
else:
    print("  ✗ patch 2 not found — check whitespace/indentation")

target.write_text(src)
print(f"\nApplied {n}/2 patches to {target}")