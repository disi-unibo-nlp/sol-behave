"""
generate_dcr.py
script order: 4

Genera grafi DCR per i contratti nel dataset usando llm_client.py.

Differenze rispetto alla versione con outlines:
- Usa llm_client.py (VLLMClient / APIClient) invece di outlines
- I passi indipendenti vengono mandati in batch alla GPU in parallelo:
    • branches + known_state   → un solo complete_batch (step 0+5)
    • events                   → dipende da branches (step 1)
    • relations + call_params  → un solo complete_batch (step 2+4)
    • marking                  → istantaneo, nessuna chiamata LLM (step 3)
- Un contratto alla volta: se qualcosa va storto è visibile subito
- Checkpoint: se source_dcr.json esiste già, il contratto viene saltato

Uso:
    python generate_dcr.py
    python generate_dcr.py --contracts ../dataset/contracts_dataset_1to20 \
                            --generated ./generated_dcr_1to20 \
                            --model     Qwen/Qwen2.5-Coder-32B-Instruct-AWQ \
                            --context-length 32768

Variabili d'ambiente (alternative ai flag CLI):
    CONTRACTS_DIR   GENERATED_DIR   LLM_MODEL   LLM_API_URL
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

#sys.path.insert(0, str(Path(__file__).parent))
#sys.path.append("/workdir/src/utils/")

from llm_client import VLLMClient, APIClient, LLMRequest

# ── Configurazione defaults ───────────────────────────────────────────────────
MAX_MODEL_LEN = os.getenv("MAX_MODEL_LEN", 5264) # export $MAX_MODEL_LEN=5264
CONTRACTS_DIR = Path(os.getenv("CONTRACTS_DIR", "../dataset/contracts_dataset_1to20"))
GENERATED_DIR = Path(os.getenv("GENERATED_DIR", "./generated_dcr_1to20"))
LLM_MODEL     = os.getenv("LLM_MODEL", "Qwen/Qwen2.5-Coder-32B-Instruct-AWQ")

MAX_TOKENS_BRANCHES    = 2048
MAX_TOKENS_KNOWN_STATE = 1024
MAX_TOKENS_EVENTS      = 4096
MAX_TOKENS_RELATIONS   = 4096
MAX_TOKENS_PARAMS      = 4096

# Contratti con sorgente > questa soglia vengono processati senza commenti
LARGE_SOURCE_THRESHOLD = 120_000

# ── Source loading ────────────────────────────────────────────────────────────

def load_solidity_source(contract_dir: Path) -> tuple[str, str]:
    """
    Carica il sorgente Solidity. Ritorna (source_text, kind).
    kind è "sol" o "json".
    Per source.json concatena tutti i file con header di commento.
    """
    sol  = contract_dir / "source.sol"
    jsn  = contract_dir / "source.json"

    if sol.exists():
        return sol.read_text(encoding="utf-8"), "sol"

    if jsn.exists():
        data    = json.loads(jsn.read_text(encoding="utf-8"))
        sources = data.get("sources", {})
        if sources:
            parts = [
                f"// ── FILE: {fp} ──\n{fc.get('content','')}"
                for fp, fc in sources.items()
                if fc.get("content", "").strip()
            ]
            return "\n\n".join(parts), "json"
        direct = data.get("content", "")
        if direct:
            return direct, "json"

    return "", "unknown"


def strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    source = re.sub(r"//[^\n]*",  "", source)
    source = re.sub(r"\n{3,}",    "\n\n", source)
    return source.strip()


def load_abi(contract_dir: Path) -> list:
    p = contract_dir / "abi.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def abi_callable_functions(abi: list) -> set[str]:
    return {
        e["name"]
        for e in abi
        if e.get("type") == "function"
        and e.get("stateMutability", "nonpayable") not in ("view", "pure")
    }


# ── Prompt builders ───────────────────────────────────────────────────────────
# (identici alla versione originale — nessuna modifica al contenuto dei prompt)

def build_known_state_prompt(code: str) -> str:
    return f"""You are a Solidity analyzer. Extract all state variables that have
a fixed initial value set in their declaration or in the constructor.

For each such variable output it as a key-value pair where:
- key: the exact variable name as it appears in the contract
- value: the initial value as a decimal string (e.g. "30", "3999", "1000000000000000")

RULES:
- Only include variables with a KNOWN constant initial value
- Convert ether/wei literals to wei strings (e.g. 0.001 ether -> "1000000000000000")
- Do NOT include variables whose initial value depends on constructor arguments
- Do NOT include mappings, arrays, or complex types
- Do NOT include variables initialized to 0 (that is the default)
- Include both public and private variables

Return ONLY a valid JSON object with a "known_state" key. /no_think

Contract:
{code}"""


def build_branches_prompt(code: str, abi_functions: list[str] | None = None) -> str:
    abi_hint = ""
    if abi_functions:
        abi_hint = (
            f"\n\nABI FUNCTIONS (ONLY these are callable on-chain):\n"
            + "\n".join(f"- {fn}" for fn in abi_functions)
            + "\n\nDo NOT create branches for any function not in this list.\n"
        )
    return f"""You are a Solidity analyzer. For each public/external state-changing function,
identify all distinct execution branches (if/else paths with different state changes or ETH flows).
{abi_hint}
For each branch output:
- function: exact Solidity function name (public/external only, in ABI)
- branch_id: camelCase id (e.g. "mintFree", "mintPaid")
- guard: condition selecting this branch, "" if no branching
- effect: brief description of the state change

RULES:
- ONLY public/external functions — never internal/private/view/pure
- Constructor → function="constructor", branch_id="deploy"
- One entry with guard="" for functions with no branching

Return ONLY a valid JSON object with a "branches" array. /no_think

Contract:
{code}"""


def build_events_prompt(code: str, branches: list[dict]) -> str:
    branch_hint = ""
    if branches:
        branch_hint = (
            "\n\nIDENTIFIED BRANCHES:\n"
            + "\n".join(
                f'- branch_id="{b["branch_id"]}" function="{b["function"]}"'
                f' guard="{b["guard"]}" effect="{b["effect"]}"'
                for b in branches
            )
            + "\n\nCreate ONE event per branch. If guard is empty, use function name as id.\n"
        )
    return f"""You are a smart contract analyzer. Extract DCR graph events.
{branch_hint}
INCLUDE: public/external state-changing and payable functions (and their branches).
EXCLUDE: view/pure functions, getters, internal/private functions.

Fields:
- id: branch_id if available, else camelCase function name
- label: short human-readable description of this branch
- function_name: EXACT Solidity function name from the ABI
- role: "owner" if onlyOwner/access-controlled, else "any"
- payable: true only if the Solidity function is marked payable
- repeatable: true for mint/transfer/approve; false for withdraw/setUri/one-time setup
- ownership_change: true ONLY for transferOwnership/renounceOwnership
- multi_user: true ONLY if the function explicitly takes multiple DISTINCT user addresses
  as parameters (e.g. batchTransfer(address[] recipients), teamMint(addr1, addr2)).
  Set false for functions that take a single address param (even if it is "to" or "spender"),
  and for all owner-only functions. When in doubt: false.
- drain_priority: integer ordering for the drain phase (called last, after main sequence).
  Use 0 for normal events (not in drain phase).
  Use 10 for transferOwnership (changes owner but contract stays functional).
  Use 99 for renounceOwnership or selfdestruct (permanently breaks onlyOwner after this).
  Rule: higher drain_priority = executed later. Only ownership_change events get non-zero values.

Return ONLY a valid JSON object with an "events" array.

Contract:
{code}"""


def build_relations_prompt(code: str, events: list[dict],
                            branches: list[dict]) -> str:
    ids     = [e["id"] for e in events]
    summary = "\n".join(
        f'- {e["id"]} (fn={e.get("function_name",e["id"])}, '
        f'role={e["role"]}, payable={e["payable"]}, repeatable={e["repeatable"]})'
        for e in events
    )
    branch_hint = ""
    if branches:
        branch_hint = "\n\nBRANCH GUARDS:\n" + "\n".join(
            f'- {b["branch_id"]}: guard="{b["guard"]}", effect="{b["effect"]}"'
            for b in branches
        ) + "\n"
    return f"""You are a smart contract analyzer. Extract DCR relations between these events.

RELATION TYPES:
- condition:  A must execute before B can execute
- response:   executing A obligates B to eventually execute
- exclusion:  executing A permanently disables B
- inclusion:  executing A re-enables B

EVENTS (use ONLY these ids in from/to):
{summary}

Valid ids: {ids}
{branch_hint}
RULES:
- "from" and "to" MUST be ids from the list — never invent new ids
- "guard": on-chain condition for this relation; use "" if always applies — NEVER null
- ETH accumulation → response: payable event A + withdraw B → response A→B, guard "address(this).balance > 0"
- Self-exclusion for per-wallet limits: balanceOf guard → exclusion event→itself
- Config → re-enable: setX event enables events it unblocks
- Fewer accurate relations > many speculative ones

Return ONLY a valid JSON object with a "relations" array.

Contract:
{code}"""


def build_params_prompt(code: str, events: list[dict]) -> str:
    summary = "\n".join(
        f'- {e["id"]}: {e["label"]} '
        f'(fn={e.get("function_name",e["id"])}, payable={e["payable"]})'
        for e in events
    )
    return f"""You are a smart contract test parameter generator.

EVENTS:
{summary}

For each event provide realistic Hardhat test parameters:
- event_id: exact event id from the list
- function_name: exact Solidity function name from the ABI
- params: array of realistic test values in order
  (addresses: "owner" or "user1"; amounts: 1/2/3; strings: "ipfs://QmTest/"; booleans: true)
- value_wei: ETH to send as string in wei ("0" if not payable;
  "5000000000000000" for paid mint; appropriate value for other payable fns)

Return ONLY a valid JSON object with a "call_params" array. /no_think

Contract:
{code}"""


# ── JSON parsing ──────────────────────────────────────────────────────────────

def parse_json_response(raw: str, label: str) -> dict:
    """
    Estrae un oggetto JSON dalla risposta del modello.
    Gestisce markdown fences e testo prima/dopo il JSON.
    """
    if not raw or not raw.strip():
        raise ValueError(f"{label}: empty response")

    text = raw.strip()
    # Rimuovi markdown fences
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    # Rimuovi eventuali <think>...</think>
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # Trova il primo oggetto JSON bilanciato
    start = text.find("{")
    if start == -1:
        raise ValueError(f"{label}: no JSON object in response. First 200 chars:\n{text[:200]}")

    depth = 0
    for i in range(start, len(text)):
        if   text[i] == "{": depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i+1])
                except json.JSONDecodeError as e:
                    raise ValueError(
                        f"{label}: JSON parse error: {e}\n"
                        f"Candidate:\n{text[start:i+1][:400]}"
                    )

    raise ValueError(f"{label}: unbalanced JSON. Text:\n{text[:400]}")


# ── DCR generation ────────────────────────────────────────────────────────────

def generate_dcr(
    solidity_code:  str,
    client,
    stripped_code:  str | None = None,
    callable_fns:   set[str] | None = None,
) -> dict:
    """
    Genera il grafo DCR per un contratto.

    Struttura delle chiamate LLM (batch dove possibile):
      Batch A (parallel): branches + known_state      → 2 richieste
      Sequential:         events(branches)             → 1 richiesta
      Batch B (parallel): relations(events) + params   → 2 richieste
      Instant:            marking                      → nessuna richiesta LLM

    Un contratto alla volta — nessun parallelismo cross-contract.
    """
    if stripped_code is None:
        stripped_code = strip_comments(solidity_code)

    # Usa il sorgente senza commenti se molto grande
    code = stripped_code if len(solidity_code) > LARGE_SOURCE_THRESHOLD else solidity_code

    abi_fn_list = sorted(callable_fns) if callable_fns else None

    def _safe_call(prompt: str, label: str, max_tokens: int) -> dict:
        """
        Chiama il modello con fallback al sorgente senza commenti
        in caso di errore di contesto.
        """
        for attempt, current_code in enumerate([code, stripped_code], 1):
            current_prompt = prompt if attempt == 1 else \
                prompt.replace(code, stripped_code, 1)
            try:
                raw = client.complete(
                    system="You are a Solidity expert. Output ONLY valid JSON.",
                    user=current_prompt,
                    max_tokens=max_tokens,
                    temperature=0.0,
                )
                return parse_json_response(raw, label)
            except Exception as e:
                err = str(e).lower()
                is_context = any(k in err for k in (
                    "context", "length", "token", "too long",
                    "maximum", "exceeded", "truncat", "overflow",
                ))
                if attempt == 1 and is_context:
                    print(f"      [warn] {label}: context overflow, retrying without comments...")
                    continue
                raise

    # ── Batch A: branches + known_state (indipendenti) ────────────────────────
    print("  [A] Extracting branches + known_state (batch) ...", end=" ", flush=True)
    t0 = time.time()

    branches_prompt    = build_branches_prompt(code, abi_functions=abi_fn_list)
    known_state_prompt = build_known_state_prompt(code)

    batch_a = client.complete_batch([
        {"system": "You are a Solidity expert. Output ONLY valid JSON.",
         "user": branches_prompt,
         "max_tokens": MAX_TOKENS_BRANCHES,
         "temperature": 0.0},
        {"system": "You are a Solidity expert. Output ONLY valid JSON.",
         "user": known_state_prompt,
         "max_tokens": MAX_TOKENS_KNOWN_STATE,
         "temperature": 0.0},
    ])
    print(f"✓  {time.time()-t0:.1f}s")

    # Parsa branches
    try:
        branches = parse_json_response(batch_a[0], "branches").get("branches", [])
    except Exception as e:
        print(f"      [warn] branches parse failed ({e}), continuing without branches")
        branches = []

    # Filtra branches per ABI
    if callable_fns:
        branches = [
            b for b in branches
            if b.get("function") in callable_fns or b.get("function") == "constructor"
        ]

    # Parsa known_state
    try:
        known_state = parse_json_response(batch_a[1], "known_state").get("known_state", {})
        known_state = {k: str(v) for k, v in known_state.items()}
    except Exception as e:
        print(f"      [warn] known_state parse failed ({e}), using empty")
        known_state = {}

    print(f"      branches: {[b['branch_id'] for b in branches]}")
    print(f"      known_state: {known_state}")

    # ── Step 1: events (dipende da branches) ──────────────────────────────────
    print("  [1] Extracting events ...", end=" ", flush=True)
    t0 = time.time()
    events_prompt = build_events_prompt(code, branches)
    try:
        raw_events = client.complete(
            system="You are a Solidity expert. Output ONLY valid JSON.",
            user=events_prompt,
            max_tokens=MAX_TOKENS_EVENTS,
            temperature=0.0,
        )
        events = parse_json_response(raw_events, "events").get("events", [])
    except Exception as e:
        print(f"\n      [warn] events failed ({e}), using empty list")
        events = []

    # Rimuovi deploy e filtra per ABI
    events = [e for e in events if e["id"] != "deploy"]
    if callable_fns:
        before = len(events)
        events = [e for e in events if e.get("function_name", e["id"]) in callable_fns]
        removed = before - len(events)
        if removed:
            print(f"\n      ABI filter removed {removed} non-callable events")

    print(f"✓  {time.time()-t0:.1f}s  → {len(events)} events: {[e['id'] for e in events]}")

    if not events:
        print("      [warn] no events generated, returning minimal DCR")
        return {
            "events": [], "relations": [], "branches": branches,
            "marking": {"executed": [], "included": [], "pending": []},
            "call_params": {}, "known_state": known_state,
        }

    # ── Batch B: relations + params (entrambi dipendono da events, sono indipendenti tra loro)
    print("  [B] Extracting relations + call_params (batch) ...", end=" ", flush=True)
    t0 = time.time()

    relations_prompt = build_relations_prompt(code, events, branches)
    params_prompt    = build_params_prompt(code, events)

    batch_b = client.complete_batch([
        {"system": "You are a Solidity expert. Output ONLY valid JSON.",
         "user": relations_prompt,
         "max_tokens": MAX_TOKENS_RELATIONS,
         "temperature": 0.0},
        {"system": "You are a Solidity expert. Output ONLY valid JSON.",
         "user": params_prompt,
         "max_tokens": MAX_TOKENS_PARAMS,
         "temperature": 0.0},
    ])
    print(f"✓  {time.time()-t0:.1f}s")

    # Parsa relations
    valid_ids = {e["id"] for e in events}
    try:
        raw_relations = parse_json_response(batch_b[0], "relations").get("relations", [])
        relations = [
            r for r in raw_relations
            if r.get("from") in valid_ids and r.get("to") in valid_ids
            and r.get("type") in ("condition", "response", "exclusion", "inclusion")
            and isinstance(r.get("guard", ""), str)
        ]
    except Exception as e:
        print(f"      [warn] relations parse failed ({e}), using empty")
        relations = []

    # Parsa call_params
    try:
        raw_params  = parse_json_response(batch_b[1], "call_params").get("call_params", [])
        call_params = {p["event_id"]: p for p in raw_params if p.get("event_id") in valid_ids}
    except Exception as e:
        print(f"      [warn] call_params parse failed ({e}), using empty")
        call_params = {}

    print(f"      relations: {len(relations)}  call_params: {list(call_params.keys())}")

    # ── Step 3: marking (istantaneo) ──────────────────────────────────────────
    marking = {
        "executed": [],
        "included": [e["id"] for e in events],
        "pending":  [],
    }

    return {
        "events":      events,
        "relations":   relations,
        "branches":    branches,
        "marking":     marking,
        "call_params": call_params,
        "known_state": known_state,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate DCR graphs for Solidity contracts."
    )
    parser.add_argument("--contracts",       default=str(CONTRACTS_DIR),
                        help="Dataset directory with contract subdirs")
    parser.add_argument("--generated",       default=str(GENERATED_DIR),
                        help="Output directory for DCR JSON files")
    parser.add_argument("--model",           default=LLM_MODEL,
                        help="vLLM model name (in-process) or '' for remote server")
    parser.add_argument("--context-length",  type=int, default=MAX_MODEL_LEN,
                        help="Model context window in tokens (default 32768)")
    parser.add_argument("--no-skip",         action="store_true",
                        help="Re-process contracts even if source_dcr.json exists")
    args = parser.parse_args()

    contracts_dir = Path(args.contracts)
    generated_dir = Path(args.generated)
    generated_dir.mkdir(parents=True, exist_ok=True)

    contract_dirs = sorted([d for d in contracts_dir.iterdir() if d.is_dir()])
    if not contract_dirs:
        print(f"No contract directories found in {contracts_dir}")
        sys.exit(1)

    print(f"Contracts : {contracts_dir}  ({len(contract_dirs)} dirs)")
    print(f"Output    : {generated_dir}")
    print(f"Model     : {args.model or '(remote server)'}")
    print()

    # Carica il client LLM
    if args.model:
        from llm_client import VLLMClient
        print(f"Loading model {args.model} ...")
        client = VLLMClient(args.model, max_model_len=args.context_length,
                            temperature=0.0)
        print("Model loaded.\n")
    else:
        from llm_client import VLLMClient
        print("Connecting to remote vLLM server ...")
        client = VLLMClient(temperature=0.0)
        print()

    # Processa un contratto alla volta
    ok_count = fail_count = skip_count = 0

    for ci, contract_dir in enumerate(contract_dirs, 1):
        address = contract_dir.name
        #if address == "0x0001a500a6b18995b03f44bb040a5ffc28e45cb0":
        out_dir  = generated_dir / address
        out_path = out_dir / "source_dcr.json"

        # Checkpoint
        if out_path.exists() and not args.no_skip:
            skip_count += 1
            print(f"[{ci}/{len(contract_dirs)}]  {address[:16]}…  ✓ already done — skip")
            continue

        # Carica sorgente
        solidity_code, source_kind = load_solidity_source(contract_dir)
        if not solidity_code.strip():
            print(f"[{ci}/{len(contract_dirs)}]  {address[:16]}…  ✗ no source — skip")
            fail_count += 1
            continue

        stripped_code = strip_comments(solidity_code)
        abi           = load_abi(contract_dir)
        callable_fns  = abi_callable_functions(abi)

        print(f"[{ci}/{len(contract_dirs)}]  {address[:16]}…  "
            f"(kind={source_kind}, "
            f"chars={len(solidity_code):,}→{len(stripped_code):,}, "
            f"abi_fns={len(callable_fns)})")
        if callable_fns:
            print(f"  callable: {sorted(callable_fns)}")

        t_contract = time.time()
        try:
            dcr = generate_dcr(
                solidity_code, client,
                stripped_code=stripped_code,
                callable_fns=callable_fns if callable_fns else None,
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(dcr, indent=2), encoding="utf-8")
            elapsed = time.time() - t_contract
            print(f"  → saved  ({elapsed:.0f}s)  "
                f"events={len(dcr['events'])}  "
                f"relations={len(dcr['relations'])}\n")
            ok_count += 1
        except Exception as e:
            elapsed = time.time() - t_contract
            print(f"  ✗ Error ({elapsed:.0f}s): {e}\n")
            fail_count += 1

    print(f"{'='*50}")
    print(f"  Done: {ok_count} OK  {fail_count} failed  {skip_count} skipped")