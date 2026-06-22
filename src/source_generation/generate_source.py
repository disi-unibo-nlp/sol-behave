"""
generate_source.py  —  Function-level completion pipeline

Strategia: per ogni funzione con body non vuoto, costruisci un prompt
che contiene il sorgente ORIGINALE del contratto con SOLO quella funzione
sostituita da // TODO. Il modello vede tutto il contesto reale:
struct, eventi, variabili di stato, altre funzioni già implementate.

Loop:
    per ogni contratto:
        per ogni funzione con body (source_i = source con fn_i → TODO):
            batch di k prompt identici → k implementazioni
        assembla k versioni del contratto finale
        compila ciascuna

Questo è il setup più vicino ai paper di riferimento (SolBench, SolEval)
e richiede zero preprocessing artificiale.

python3 generate_source.py | tee "./generation_logs/${LLM_MODEL//\//_}_generation.log"
"""
import os
import json
import re
import time
import shutil
import subprocess
from pathlib import Path

from storage import ContractWorkspace
import sys
sys.path.append("/workdir/src/utils/")
from hardhat_utils import (
    prepare_and_compile,
    load_source_texts,
    hardhat_config,
    install_npm_deps,
    detect_remappings,
    setup_workspace
)
from pipeline import (
    #_find_matching_brace,
    _get_indent,
    sanitize_natspec,
    try_compile,
)

from source_utils import _find_matching_brace, _find_functions, _main_source_text, _load_source

# The task is code generation, we gotta make smart contracts written in solidity
# This is the model that performs  47.7% before repair and 59.8% compile@10 after repair:
#   Qwen/Qwen2.5-Coder-32B-Instruct-AWQ
# Generation task: Zero-shot generation of body functions, 
# then we do the repair :D

# Repair is basically asking the model to fix a compilation error, since a considerable amount of 
# compilation errors derive from typos, a single run of repair is enough to obtain a nice compile@10. 

# ── Configurazione ────────────────────────────────────────────────────────────

LLM_MODEL = os.getenv("LLM_MODEL", "unsloth/Qwen3.6-27B-MTP-GGUF")
CONTRACTS_DIR = Path("../dataset/contracts_dataset_1to20")
GENERATED_DIR = Path(f"./generated_oneshot_{LLM_MODEL}")
WORK_DIR      = Path("./hardhat_workspace/")
K             = 10
TEMPERATURE   = 0.7
MAX_TOKENS       = 2048    # token di OUTPUT per ogni completion
MAX_CONTEXT_LEN  = int(os.getenv("MAX_MODEL_LEN", "8192"))

_SYSTEM = """\
You are a Solidity smart contract development expert.
Your task: given a Solidity contract with ONE function body marked \
`// TODO: implement this`, output ONLY the statements that go \
INSIDE that function body.

Rules:
- Do NOT repeat the function signature or opening/closing braces.
- Do NOT wrap output in markdown code blocks.
- Do NOT add new functions, state variables, imports, or pragma.
- Output only valid Solidity statements, one per line."""

# ── Utilities ─────────────────────────────────────────────────────────────────

def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


# ── Estrazione funzioni con body ──────────────────────────────────────────────
def _truncate_source_for_prompt(source: str, fn: dict, max_chars: int = 12000) -> str:
    """
    Se il sorgente è troppo lungo, tronca mantenendo:
    - Le prime N righe (pragma, import, struct, eventi, variabili di stato)
    - Un finestra intorno alla funzione target
    - Le ultime righe (chiusura contratto)

    max_chars è una stima conservativa (32k token model → ~24k chars input budget,
    meno il prompt overhead → ~12k per il sorgente).
    """
    if len(source) <= max_chars:
        return source

    lines      = source.splitlines(keepends=True)
    total_lines = len(lines)

    # Trova le righe della funzione target
    fn_line_start = source[:fn["body_open"]].count('\n')
    fn_line_end   = source[:fn["body_close"]].count('\n')

    # Quante righe di contesto prima e dopo la funzione
    context_window = 60   # righe prima e dopo

    # Righe "header" del file: pragma, import, struct, eventi, state vars
    # Tipicamente le prime ~50 righe
    header_lines = 60

    # Righe da includere
    include: set[int] = set()
    include.update(range(min(header_lines, fn_line_start)))      # header
    include.update(range(max(0, fn_line_start - context_window), # contesto prima
                         fn_line_end + context_window))          # + funzione + contesto dopo
    include.update(range(max(0, total_lines - 5), total_lines))  # chiusura file

    # Ricostruisci con ellipsis tra sezioni non contigue
    result_lines = []
    prev = -1
    for i in sorted(include):
        if i >= total_lines:
            break
        if prev != -1 and i > prev + 1:
            result_lines.append("    // ... (truncated) ...\n")
        result_lines.append(lines[i])
        prev = i

    return "".join(result_lines)


def _make_prompt(source: str, fn: dict, contract_name: str,
                  compiler_version: str, max_chars: int = 12000) -> str:
    """
    Costruisce il prompt: sorgente originale (eventualmente troncato) con
    il body della funzione target sostituito da `// TODO: implement this`.
    """
    todo_body = "{\n        // TODO: implement this\n    }"
    source_with_todo = (
        source[:fn["body_open"]]
        + todo_body
        + source[fn["body_close"]:]
    )

    # Tronca se necessario
    source_with_todo = _truncate_source_for_prompt(
        source_with_todo, fn, max_chars=max_chars
    )

    fn_name_m = re.search(r'\bfunction\s+(\w+)\s*\(', fn["fn_sig"])
    fn_name   = fn_name_m.group(1) if fn_name_m else "the function"

    return (
        f"// Contract: {contract_name}  (compiler: {compiler_version})\n"
        f"// Complete ONLY the body of function `{fn_name}` marked with "
        f"'// TODO: implement this'.\n"
        f"// Output ONLY the body statements (no braces, no signature).\n\n"
        + source_with_todo
    )


# ── Assembly ──────────────────────────────────────────────────────────────────

def _apply_completion(source: str, fn: dict, body: str) -> str:
    """Sostituisce il body della funzione con il completamento generato."""
    body = re.sub(r"<think>.*?</think>", "", body, flags=re.DOTALL).strip()
    if "```" in body:
        body = "\n".join(
            l for l in body.splitlines()
            if not l.strip().startswith("```")
        ).strip()
    body = "\n".join(
        l for l in body.splitlines()
        if not l.strip().startswith("pragma ")
        and not l.strip().startswith("import ")
    ).strip()

    # Il modello a volte ripete la firma della funzione nell'output.
    # Rimuovi tutto fino alla prima { se il body inizia con una firma.
    fn_name_m = re.search(r'\bfunction\s+(\w+)\s*\(', fn["fn_sig"])
    if fn_name_m:
        fn_name = fn_name_m.group(1)
        # Rimuovi "function name(...) ... {" iniziale
        stripped = re.sub(
            r'^\s*function\s+' + re.escape(fn_name) + r'\s*\([^{]*\{',
            '', body, flags=re.DOTALL
        ).strip()
        if stripped != body:
            body = stripped
            # Rimuovi anche l'eventuale } finale in eccesso
            if body.endswith('}') and body.count('{') < body.count('}'):
                body = body[:body.rfind('}')].strip()

    indent = _get_indent(source, fn["body_open"])
    inner  = indent + "    "
    body_indented = "\n".join(
        (inner + l if l.strip() else "") for l in body.splitlines()
    )
    new_body = f"{{\n{body_indented}\n{indent}}}"
    return source[:fn["body_open"]] + new_body + source[fn["body_close"]:]


# ── Pipeline principale ────────────────────────────────────────────────────────

def run_oneshot(
    contracts:     list[Path],
    client,
    k:             int         = K,
    batch_size:    int         = 32,
    generated_dir: Path        = GENERATED_DIR,
    work_dir:      Path        = WORK_DIR,
) -> None:
    generated_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    run_start        = time.time()
    total            = len(contracts)
    overall_compiled = 0
    overall_samples  = 0
    contract_results = []

    for ci, contract_dir in enumerate(contracts, 1):
        meta   = _load_json(contract_dir / "metadata.json")
        cname  = meta.get("contract_name", "Contract")
        ver    = meta.get("compiler_version", "")
        addr   = contract_dir.name

        out_dir = generated_dir / addr
        out_dir.mkdir(parents=True, exist_ok=True)

        # Checkpoint — riprendi da dove ci siamo interrotti
        ckpt_path = out_dir / "result.json"
        if ckpt_path.exists():
            result   = json.loads(ckpt_path.read_text())
            compiled = result.get("compiled_count", 0)
            print(f"  [{ci}/{total}]  {addr[:16]}…  {cname}  "
                  f"— already done ({compiled}/{k}), skipping")
            overall_compiled += compiled
            overall_samples  += k
            contract_results.append(result)
            continue

        contract_start = time.time()

        main_source, main_fp = _main_source_text(contract_dir)
        is_multifile = main_fp is not None and (contract_dir / "source.json").exists()

        if not main_source:
            print(f"  [{ci}/{total}]  {addr[:16]}…  {cname}  — no source, skip")
            continue

        fns = _find_functions(main_source)
        if not fns:
            print(f"  [{ci}/{total}]  {addr[:16]}…  {cname}  — no functions to complete")
            for i in range(1, k + 1):
                _save_sample(out_dir, i, main_source, contract_dir, main_fp)
            compiled_this, sample_results = _compile_all_samples(
                out_dir, k, contract_dir, meta, work_dir, is_multifile, main_fp)
            overall_compiled += compiled_this
            overall_samples  += k
            result = _save_result(out_dir, cname, addr, k, compiled_this,
                                   time.time() - contract_start)
            contract_results.append(result)
            _write_summary(generated_dir, contract_results,
                           overall_compiled, overall_samples,
                           time.time() - run_start)
            continue

        print(f"  [{ci}/{total}]  {addr[:16]}…  {cname}  "
              f"({len(fns)} functions, k={k})")

        working = [main_source for _ in range(k)]

        for fi, fn in enumerate(fns):
            fn_name_m = re.search(r'\bfunction\s+(\w+)\s*\(', fn["fn_sig"])
            fn_label  = fn_name_m.group(1) if fn_name_m else f"fn{fi}"

            prompt_source = working[0]
            current_fns   = _find_functions(prompt_source)
            target_fn     = next(
                (f for f in current_fns if fn_label in f["fn_sig"]), None
            )
            if target_fn is None:
                print(f"      fn {fi+1}/{len(fns)}  {fn_label}  — not found, skip")
                continue

            max_src_chars = max(4000, (MAX_CONTEXT_LEN - MAX_TOKENS) * 3 - 500)
            prompt  = _make_prompt(prompt_source, target_fn, cname, ver,
                                    max_chars=max_src_chars)
            request = {
                "system":      _SYSTEM,
                "user":        prompt,
                "max_tokens":  MAX_TOKENS,
                "temperature": TEMPERATURE,
            }

            n_batches = (k + batch_size - 1) // batch_size
            responses: list[str] = []
            for bi in range(n_batches):
                sl   = slice(bi * batch_size, (bi + 1) * batch_size)
                reqs = [request] * len(range(*sl.indices(k)))
                print(f"      fn {fi+1}/{len(fns)}  {fn_label}"
                      f"  batch {bi+1}/{n_batches} ({len(reqs)}×k) ...",
                      end="  ", flush=True)
                t0 = time.time()
                try:
                    resp = client.complete_batch(reqs)
                except Exception as e:
                    print(f"✗  {e}")
                    resp = [""] * len(reqs)
                print(f"✓  {time.time()-t0:.1f}s")
                responses.extend(resp)

            for si, raw in enumerate(responses[:k]):
                if not raw:
                    continue
                sample_fns = _find_functions(working[si])
                sf = next((f for f in sample_fns if fn_label in f["fn_sig"]), None)
                if sf:
                    working[si] = _apply_completion(working[si], sf, raw)

        for i, text in enumerate(working, 1):
            text = sanitize_natspec(text)
            _save_sample(out_dir, i, text, contract_dir, main_fp)

        compiled_this, sample_results = _compile_all_samples(
            out_dir, k, contract_dir, meta, work_dir, is_multifile, main_fp)
        overall_compiled += compiled_this
        overall_samples  += k

        elapsed      = time.time() - contract_start
        compile_at_k = compiled_this / k if k else 0
        print(f"  → {cname}  compiled {compiled_this}/{k}  "
              f"compile@{k}={compile_at_k:.0%}  elapsed={elapsed:.0f}s  "
              f"(total: {overall_compiled}/{overall_samples})")

        result = _save_result(out_dir, cname, addr, k, compiled_this, elapsed, sample_results)
        contract_results.append(result)
        _write_summary(generated_dir, contract_results,
                       overall_compiled, overall_samples,
                       time.time() - run_start)

    # Summary finale
    total_elapsed = time.time() - run_start
    print(f"\n{'='*60}")
    print(f"  Total compiled : {overall_compiled}/{overall_samples}")
    if overall_samples:
        print(f"  avg compile@{k}  : {overall_compiled/overall_samples:.1%}")
    print(f"  Total time     : {total_elapsed:.0f}s  ({total_elapsed/60:.1f} min)")
    _write_summary(generated_dir, contract_results,
                   overall_compiled, overall_samples, total_elapsed)


def _save_sample(out_dir: Path, idx: int, text: str,
                  contract_dir: Path, main_fp: str | None) -> None:
    """
    Salva un sample su disco nel formato giusto:
    - Multi-file: inietta `text` nel source.json originale → sample_i.json
    - Single-file: scrive direttamente → sample_i.sol
    """
    if main_fp and (contract_dir / "source.json").exists():
        src_data = json.loads((contract_dir / "source.json").read_text())
        new_sources = dict(src_data.get("sources", {}))
        new_sources[main_fp] = {"content": text}
        src_data["sources"] = new_sources
        (out_dir / f"sample_{idx}.json").write_text(json.dumps(src_data))
    else:
        (out_dir / f"sample_{idx}.sol").write_text(text)


def _compile_all_samples(
    out_dir: Path, k: int, contract_dir: Path,
    meta: dict, work_dir: Path,
    is_multifile: bool = False, main_fp: str | None = None,
) -> tuple[int, list[dict]]:
    """Compila tutti i sample. Ritorna (n_compiled, sample_results)."""
    compiled      = 0
    sample_results = []

    for i in range(1, k + 1):
        json_p = out_dir / f"sample_{i}.json"
        sol_p  = out_dir / f"sample_{i}.sol"
        sample_path = json_p if json_p.exists() else (sol_p if sol_p.exists() else None)
        if sample_path is None:
            continue

        if sample_path.suffix == ".json":
            ok, errs = prepare_and_compile(sample_path, meta, work_dir)
        else:
            source = sample_path.read_text()
            ok, errs = try_compile(source, meta, work_dir,
                                   contract_dir if is_multifile else None)

        sample_results.append({
            "idx":      i,
            "compiled": ok,
            "error":    errs[:400] if not ok and errs else "",
        })

        if ok:
            compiled += 1
            print(f"    ✓ sample {i}")
        else:
            err_lines = [l for l in (errs or "").splitlines() if l.strip()][:4]
            print(f"    ✗ sample {i}: {chr(10).join(err_lines[:2])}")
            line_m = re.search(r':(\d+):\d+:', errs or "")
            if line_m:
                if sample_path.suffix == ".json":
                    try:
                        data     = json.loads(sample_path.read_text())
                        srcs     = data.get("sources", {})
                        cname_lc = meta.get("contract_name", "").lower()
                        fp       = main_fp or next(
                            (fp for fp in srcs if cname_lc in Path(fp).stem.lower()),
                            next(iter(srcs), None)
                        )
                        src_text = srcs[fp]["content"] if fp else ""
                    except Exception:
                        src_text = ""
                else:
                    src_text = sample_path.read_text()
                lineno = int(line_m.group(1))
                lines  = src_text.splitlines()
                for li in range(max(0, lineno-3), min(len(lines), lineno+2)):
                    marker = ">>>" if li == lineno - 1 else "   "
                    print(f"        {marker} {li+1:4d}  {lines[li]}")

    return compiled, sample_results


def _save_result(out_dir: Path, cname: str, addr: str, k: int,
                  compiled: int, elapsed_s: float,
                  sample_results: list[dict] | None = None) -> dict:
    result = {
        "contract_name":  cname,
        "address":        addr,
        "k":              k,
        "compiled_count": compiled,
        "compile_at_k":   round(compiled / k, 4) if k else 0,
        "elapsed_s":      round(elapsed_s, 1),
        "samples":        sample_results or [],
    }
    (out_dir / "result.json").write_text(json.dumps(result, indent=2))
    return result


def _write_summary(generated_dir: Path, contract_results: list[dict],
                   overall_compiled: int, overall_samples: int,
                   elapsed_s: float) -> None:
    """Scrive batch_summary.json aggiornato dopo ogni contratto."""
    avg = overall_compiled / overall_samples if overall_samples else 0
    summary = {
        "total_contracts":  len(contract_results),
        "total_samples":    overall_samples,
        "total_compiled":   overall_compiled,
        "avg_compile_at_k": round(avg, 4),
        "elapsed_s":        round(elapsed_s, 1),
        "elapsed_min":      round(elapsed_s / 60, 1),
        "contracts":        contract_results,
    }
    (generated_dir / "batch_summary.json").write_text(
        json.dumps(summary, indent=2)
    )


def recompile_only(
    contracts:     list[Path],
    k:             int,
    generated_dir: Path,
    work_dir:      Path,
) -> None:
    """
    Ricompila i sample già generati senza chiamare il modello.
    Utile per:
    - Aggiornare i risultati dopo aver fixato l'ambiente Hardhat
    - Aggiungere il logging degli errori a una run precedente
    - Verificare che i sample compilino in un ambiente pulito
    """
    run_start        = time.time()
    total            = len(contracts)
    overall_compiled = 0
    overall_samples  = 0
    contract_results = []

    for ci, contract_dir in enumerate(contracts, 1):
        meta   = _load_json(contract_dir / "metadata.json")
        cname  = meta.get("contract_name", "Contract")
        addr   = contract_dir.name
        out_dir = generated_dir / addr

        if not out_dir.exists():
            print(f"  [{ci}/{total}]  {addr[:16]}…  {cname}  — no generated dir, skip")
            continue

        # Conta i sample disponibili
        existing = sorted([
            p for p in out_dir.iterdir()
            if re.match(r'sample_\d+\.(sol|json)$', p.name)
        ], key=lambda p: int(re.search(r'\d+', p.name).group()))

        if not existing:
            print(f"  [{ci}/{total}]  {addr[:16]}…  {cname}  — no samples, skip")
            continue

        actual_k = len(existing)
        _, main_fp = _main_source_text(contract_dir)
        is_multifile = main_fp is not None and (contract_dir / "source.json").exists()

        print(f"  [{ci}/{total}]  {addr[:16]}…  {cname}  "
              f"(recompiling {actual_k} samples)")

        t0 = time.time()
        compiled_this, sample_results = _compile_all_samples(
            out_dir, actual_k, contract_dir, meta, work_dir,
            is_multifile, main_fp,
        )
        overall_compiled += compiled_this
        overall_samples  += actual_k
        elapsed = time.time() - t0

        compile_at_k = compiled_this / actual_k if actual_k else 0
        print(f"  → {cname}  compiled {compiled_this}/{actual_k}  "
              f"compile@{actual_k}={compile_at_k:.0%}  elapsed={elapsed:.0f}s")

        result = _save_result(out_dir, cname, addr, actual_k,
                               compiled_this, elapsed, sample_results)
        contract_results.append(result)
        _write_summary(generated_dir, contract_results,
                       overall_compiled, overall_samples,
                       time.time() - run_start)

    total_elapsed = time.time() - run_start
    print(f"\n{'='*60}")
    print(f"  Total recompiled: {overall_compiled}/{overall_samples}")
    if overall_samples:
        print(f"  avg compile@k   : {overall_compiled/overall_samples:.1%}")
    print(f"  Total time      : {total_elapsed:.0f}s  ({total_elapsed/60:.1f} min)")
    _write_summary(generated_dir, contract_results,
                   overall_compiled, overall_samples, total_elapsed)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")

    import argparse
    parser = argparse.ArgumentParser(
        description="Generate and/or compile Solidity contract completions."
    )
    parser.add_argument("--model",           default=str(LLM_MODEL))
    parser.add_argument("--contracts",       default=str(CONTRACTS_DIR))
    parser.add_argument("--generated",       default=str(GENERATED_DIR))
    parser.add_argument("--work_dir",        default=str(WORK_DIR))
    parser.add_argument("--k",               type=int,   default=K)
    parser.add_argument("--batch_size",      type=int,   default=32)
    parser.add_argument("--temperature",     type=float, default=TEMPERATURE)
    parser.add_argument("--max_tokens",      type=int,   default=MAX_TOKENS,
                         help="Max OUTPUT tokens per completion (default 2048)")
    parser.add_argument("--context_length",  type=int,   default=MAX_CONTEXT_LEN,
                         help="Model context window in tokens (default 32768)")
    parser.add_argument("--recompile-only",  action="store_true",
                         dest="recompile_only",
                         help="Skip generation, recompile existing samples only. "
                              "Useful after fixing the Hardhat environment.")
    args = parser.parse_args()

    TEMPERATURE     = args.temperature
    MAX_TOKENS      = args.max_tokens
    MAX_CONTEXT_LEN = args.context_length

    contracts_dir = Path(args.contracts)
    contracts = sorted([d for d in contracts_dir.iterdir()
                        if d.is_dir() and (d / "metadata.json").exists()])


    print(f"Found {len(contracts)} contracts in {contracts_dir}")

    generated_dir = Path(args.generated)
    work_dir      = Path(args.work_dir)
    setup_workspace(work_dir)

    if args.recompile_only:
        print(f"Mode: RECOMPILE ONLY  (no LLM calls)")
        print(f"Generated dir : {generated_dir}")
        print(f"Work dir      : {work_dir}")
        recompile_only(
            contracts     = contracts,
            k             = args.k,
            generated_dir = generated_dir,
            work_dir      = work_dir,
        )
    else:
        from llm_client import VLLMClient
        print(f"Loading model {args.model} ...")
        client = VLLMClient(args.model, max_model_len=args.context_length)
        print("Model loaded.")
        run_oneshot(
            contracts     = contracts,
            client        = client,
            k             = args.k,
            batch_size    = args.batch_size,
            generated_dir = generated_dir,
            work_dir      = work_dir,
        )