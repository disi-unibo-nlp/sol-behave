"""
repair_source.py  —  Single-pass repair of failed compiled samples

Per ogni sample che non ha compilato, costruisce un prompt con:
  - Il sorgente generato (fallito)
  - Il messaggio di errore del compilatore
  - Istruzioni per correggere SOLO l'errore indicato

Un solo tentativo di repair per sample (metodologicamente pulito).
I sample già compilati vengono saltati.

Uso:
    python repair_source.py \
        --contracts ../dataset/contracts_dataset_1to20 \
        --generated ./generated_1to20 \
        --repaired  ./repaired_1to20 \
        --work_dir  ./hardhat_workspace_compile_k \
        --model     Qwen/Qwen2.5-Coder-32B-Instruct-AWQ \
        --context_length 32768
"""

import json
import re
import time
from pathlib import Path
import os

import sys
sys.path.append("/workdir/src/utils/")

from hardhat_utils import prepare_and_compile
from pipeline import try_compile, sanitize_natspec
from generate_source import _find_functions

CONTRACTS_DIR = Path("../dataset/contracts_dataset_1to20")
GENERATED_DIR = Path("./generated_oneshot")
REPAIRED_DIR  = Path("./repaired_oneshot")
WORK_DIR      = Path("../dataset/hardhat_workspace_crawl")
MAX_TOKENS    = 2048
MAX_CONTEXT_LEN  = int(os.getenv("MAX_MODEL_LEN", "8192"))
TEMPERATURE   = 0.2   # bassa: il repair è deterministico, non vogliamo creatività

_SYSTEM = """\
You are a Solidity compiler error expert.
You will be given a Solidity smart contract that failed to compile, \
along with the compiler error message.
Fix ONLY the specific error indicated. Do not rewrite the contract.
Output the complete corrected Solidity source file."""


def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _get_sample_source(sample_path: Path, contract_dir: Path,
                        meta: dict) -> tuple[str, str | None]:
    """
    Restituisce (testo_file_principale, main_filepath_nel_json | None).
    Per multi-file estrae il file principale dal JSON.
    """
    if sample_path.suffix == ".json":
        try:
            data    = json.loads(sample_path.read_text())
            sources = data.get("sources", {})
            cname   = meta.get("contract_name", "").lower()
            main_fp = next(
                (fp for fp in sources if cname in Path(fp).stem.lower()),
                list(sources.keys())[0] if sources else None
            )
            return sources[main_fp]["content"] if main_fp else "", main_fp
        except Exception:
            return "", None
    return sample_path.read_text(), None


def _save_repaired_sample(repaired_dir: Path, idx: int, source: str,
                           contract_dir: Path, main_fp: str | None) -> Path:
    if main_fp and (contract_dir / "source.json").exists():
        src_data    = json.loads((contract_dir / "source.json").read_text())
        new_sources = dict(src_data.get("sources", {}))
        new_sources[main_fp] = {"content": source}
        src_data["sources"]  = new_sources
        out_path = repaired_dir / f"sample_{idx}.json"
        out_path.write_text(json.dumps(src_data))
    else:
        out_path = repaired_dir / f"sample_{idx}.sol"
        out_path.write_text(source)
    return out_path


def _compile_sample(sample_path: Path, contract_dir: Path,
                     meta: dict, work_dir: Path) -> tuple[bool, str]:
    if sample_path.suffix == ".json":
        return prepare_and_compile(sample_path, meta, work_dir)
    source = sample_path.read_text()
    is_mf  = (contract_dir / "source.json").exists()
    return try_compile(source, meta, work_dir, contract_dir if is_mf else None)


def _find_function_at_line(source: str, lineno: int) -> dict | None:
    """
    Trova la funzione che contiene la riga `lineno` (1-based).
    Ritorna il dict della funzione da _find_functions, oppure None.
    """
    
    fns = _find_functions(source)
    for fn in fns:
        fn_start_line = source[:fn["fn_start"]].count('\n') + 1
        fn_end_line   = source[:fn["body_close"]].count('\n') + 1
        if fn_start_line <= lineno <= fn_end_line:
            return fn
    return None


def _make_repair_prompt(source: str, fn: dict, error: str,
                         contract_name: str,
                         max_chars: int = 20000) -> str:
    """
    Prompt chirurgico: mostra il contratto con SOLO la funzione problematica
    marcata con il messaggio di errore nel body, e chiede di riscrivere
    solo il body di quella funzione.
    """
    # Estrai le prime righe dell'errore (le più informative)
    err_lines = [l for l in error.splitlines() if l.strip()][:10]
    err_short = "\n".join(err_lines)

    fn_name_m = re.search(r'\bfunction\s+(\w+)\s*\(', fn["fn_sig"])
    fn_name   = fn_name_m.group(1) if fn_name_m else "the function"

    # Sostituisci il body con il body originale annotato con l'errore
    current_body = source[fn["body_open"]:fn["body_close"]+1]
    annotated_body = (
        f"{{\n"
        f"    // COMPILER ERROR:\n"
        + "\n".join(f"    // {l}" for l in err_lines)
        + f"\n    // Fix the error above. Output ONLY the corrected body statements.\n"
        + "    // TODO: implement this\n"
        + "}"
    )
    source_annotated = (
        source[:fn["body_open"]]
        + annotated_body
        + source[fn["body_close"]+1:]
    )

    # Tronca se necessario mantenendo la funzione target visibile
    if len(source_annotated) > max_chars:
        from generate_source import _truncate_source_for_prompt
        source_annotated = _truncate_source_for_prompt(
            source_annotated, fn, max_chars=max_chars
        )

    return (
        f"Contract `{contract_name}` failed to compile with this error:\n\n"
        f"```\n{err_short}\n```\n\n"
        f"Fix function `{fn_name}` which contains the error. "
        f"Output ONLY the corrected body statements "
        f"(no signature, no braces, no markdown):\n\n"
        + source_annotated
    )


def repair_all(
    contracts:     list[Path],
    client,
    generated_dir: Path = GENERATED_DIR,
    repaired_dir:  Path = REPAIRED_DIR,
    work_dir:      Path = WORK_DIR,
    batch_size:    int  = 32,
) -> None:
    repaired_dir.mkdir(parents=True, exist_ok=True)
    from generate_source import _apply_completion

    run_start = time.time()
    total     = len(contracts)
    stats     = {"already_ok": 0, "repaired": 0, "still_fail": 0,
                 "no_error_info": 0, "no_fn_found": 0, "skipped": 0}
    all_results = []

    for ci, contract_dir in enumerate(contracts, 1):
        meta  = _load_json(contract_dir / "metadata.json")
        cname = meta.get("contract_name", "Contract")
        addr  = contract_dir.name

        gen_dir     = generated_dir / addr
        rep_out_dir = repaired_dir  / addr
        rep_out_dir.mkdir(parents=True, exist_ok=True)

        result_path = gen_dir / "result.json"
        if not result_path.exists():
            print(f"  [{ci}/{total}]  {addr[:16]}…  {cname}  — no result.json, skip")
            stats["skipped"] += 1
            continue

        orig_result  = _load_json(result_path)
        samples      = orig_result.get("samples", [])
        k            = orig_result.get("k", 10)
        ok_samples   = [s for s in samples if s.get("compiled", False)]
        fail_samples = [s for s in samples if not s.get("compiled", False)]

        # Copia i sample già OK
        for s in ok_samples:
            idx = s["idx"]
            for ext in (".json", ".sol"):
                src = gen_dir / f"sample_{idx}{ext}"
                dst = rep_out_dir / f"sample_{idx}{ext}"
                if src.exists() and not dst.exists():
                    dst.write_bytes(src.read_bytes())
            stats["already_ok"] += 1

        if not fail_samples:
            print(f"  [{ci}/{total}]  {addr[:16]}…  {cname}  — all OK, skip")
            orig_result["repaired_count"] = 0
            (rep_out_dir / "result.json").write_text(json.dumps(orig_result, indent=2))
            all_results.append(orig_result)
            continue

        print(f"  [{ci}/{total}]  {addr[:16]}…  {cname}  "
              f"({len(fail_samples)}/{k} failed)")

        # ── Per ogni sample fallito: trova la funzione, genera il repair ──
        # Raggruppa i job per poter fare batch cross-sample
        jobs = []   # (idx, source, fn, error, main_fp, sample_path)
        for s in fail_samples:
            idx   = s["idx"]
            error = s.get("error", "")

            sample_path = next(
                (gen_dir / f"sample_{idx}{ext}"
                 for ext in (".json", ".sol")
                 if (gen_dir / f"sample_{idx}{ext}").exists()),
                None
            )
            if sample_path is None:
                stats["no_error_info"] += 1
                continue

            source, main_fp = _get_sample_source(sample_path, contract_dir, meta)
            if not source or not error:
                stats["no_error_info"] += 1
                # Copia l'originale
                for ext in (".json", ".sol"):
                    src = gen_dir / f"sample_{idx}{ext}"
                    if src.exists():
                        (rep_out_dir / f"sample_{idx}{ext}").write_bytes(
                            src.read_bytes())
                continue

            # Trova il numero di riga dall'errore
            line_m  = re.search(r':(\d+):\d+:', error)
            fn      = None
            if line_m:
                lineno = int(line_m.group(1))
                fn     = _find_function_at_line(source, lineno)

            if fn is None:
                print(f"    sample {idx}: can't locate function at error line, "
                      f"copying original")
                stats["no_fn_found"] += 1
                for ext in (".json", ".sol"):
                    src = gen_dir / f"sample_{idx}{ext}"
                    if src.exists():
                        (rep_out_dir / f"sample_{idx}{ext}").write_bytes(
                            src.read_bytes())
                continue

            fn_name_m = re.search(r'\bfunction\s+(\w+)\s*\(', fn["fn_sig"])
            fn_label  = fn_name_m.group(1) if fn_name_m else "?"
            print(f"    sample {idx}: error in `{fn_label}`, scheduling repair")
            jobs.append((idx, source, fn, error, main_fp, sample_path))

        if not jobs:
            orig_result["repaired_count"] = 0
            (rep_out_dir / "result.json").write_text(json.dumps(orig_result, indent=2))
            all_results.append(orig_result)
            continue

        # Batch repair
        max_chars = max(4000, (MAX_CONTEXT_LEN - MAX_TOKENS) * 3 - 500)
        requests  = [
            {
                "system":      _SYSTEM,
                "user":        _make_repair_prompt(src, fn, err, cname, max_chars),
                "max_tokens":  MAX_TOKENS,
                "temperature": TEMPERATURE,
            }
            for (_, src, fn, err, _, _) in jobs
        ]

        n_batches = (len(requests) + batch_size - 1) // batch_size
        responses: list[str] = []
        for bi in range(n_batches):
            sl   = slice(bi * batch_size, (bi + 1) * batch_size)
            reqs = requests[sl]
            print(f"    repair batch {bi+1}/{n_batches} "
                  f"({len(reqs)} samples) ...", end="  ", flush=True)
            t0 = time.time()
            try:
                resp = client.complete_batch(reqs)
            except Exception as e:
                print(f"✗  {e}")
                resp = [""] * len(reqs)
            print(f"✓  {time.time()-t0:.1f}s")
            responses.extend(resp)

        # Applica i repair e compila
        contract_repaired = 0
        for (idx, source, fn, error, main_fp, sample_path), raw in \
                zip(jobs, responses):

            if not raw:
                # Copia l'originale senza modifiche
                for ext in (".json", ".sol"):
                    src = gen_dir / f"sample_{idx}{ext}"
                    if src.exists():
                        (rep_out_dir / f"sample_{idx}{ext}").write_bytes(
                            src.read_bytes())
                stats["still_fail"] += 1
                continue

            # Pulisci il body generato (stessa logica di _apply_completion)
            body = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            if "```" in body:
                body = "\n".join(
                    l for l in body.splitlines()
                    if not l.strip().startswith("```")
                ).strip()

            # Inietta nel sorgente originale
            fixed_source = _apply_completion(source, fn, body)
            fixed_source = sanitize_natspec(fixed_source)

            # Salva
            rep_path = _save_repaired_sample(
                rep_out_dir, idx, fixed_source, contract_dir, main_fp
            )

            # Compila
            ok, errs = _compile_sample(rep_path, contract_dir, meta, work_dir)
            fn_name_m = re.search(r'\bfunction\s+(\w+)\s*\(', fn["fn_sig"])
            fn_label  = fn_name_m.group(1) if fn_name_m else "?"
            if ok:
                contract_repaired += 1
                stats["repaired"]  += 1
                print(f"    ✓ sample {idx}  [{fn_label}] repaired")
            else:
                stats["still_fail"] += 1
                first_err = next(
                    (l for l in (errs or "").splitlines() if l.strip()), ""
                )
                print(f"    ✗ sample {idx}  [{fn_label}] still fails: "
                      f"{first_err[:70]}")

        total_ok = len(ok_samples) + contract_repaired
        print(f"  → {cname}  {total_ok}/{k} compiled after repair  "
              f"(+{contract_repaired} fixed)")

        orig_result["compiled_count"] = total_ok
        orig_result["compile_at_k"]   = round(total_ok / k, 4) if k else 0
        orig_result["repaired_count"] = contract_repaired
        (rep_out_dir / "result.json").write_text(json.dumps(orig_result, indent=2))
        all_results.append(orig_result)
        _write_repair_summary(repaired_dir, all_results,
                               time.time() - run_start, stats)

    # Summary finale
    total_elapsed = time.time() - run_start
    total_c = sum(r.get("compiled_count", 0) for r in all_results)
    total_s = sum(r.get("k", 10) for r in all_results)
    orig_c  = total_c - stats["repaired"]

    print(f"\n{'='*60}")
    print(f"  Before repair : {orig_c}/{total_s}  ({orig_c/total_s:.1%})")
    print(f"  After repair  : {total_c}/{total_s}  ({total_c/total_s:.1%})")
    print(f"  Repaired      : +{stats['repaired']} samples")
    print(f"  Still failing : {stats['still_fail']} samples")
    print(f"  No fn found   : {stats['no_fn_found']} samples")
    print(f"  Total time    : {total_elapsed:.0f}s  ({total_elapsed/60:.1f} min)")
    _write_repair_summary(repaired_dir, all_results, total_elapsed, stats)


def _write_repair_summary(repaired_dir: Path, all_results: list[dict],
                           elapsed_s: float, stats: dict) -> None:
    total_c = sum(r.get("compiled_count", 0) for r in all_results)
    total_s = sum(r.get("k", 10) for r in all_results)
    summary = {
        "total_contracts":  len(all_results),
        "total_samples":    total_s,
        "total_compiled":   total_c,
        "avg_compile_at_k": round(total_c / total_s, 4) if total_s else 0,
        "repaired_count":   stats.get("repaired", 0),
        "still_fail":       stats.get("still_fail", 0),
        "no_fn_found":      stats.get("no_fn_found", 0),
        "elapsed_s":        round(elapsed_s, 1),
        "elapsed_min":      round(elapsed_s / 60, 1),
        "contracts":        all_results,
    }
    (repaired_dir / "batch_summary.json").write_text(
        json.dumps(summary, indent=2)
    )


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    from llm_client import VLLMClient

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--model",          default="Qwen/Qwen2.5-Coder-32B-Instruct-AWQ")
    parser.add_argument("--contracts",      default=str(CONTRACTS_DIR))
    parser.add_argument("--generated",      default=str(GENERATED_DIR))
    parser.add_argument("--repaired",       default=str(REPAIRED_DIR))
    parser.add_argument("--work_dir",       default=str(WORK_DIR))
    parser.add_argument("--batch_size",     type=int,   default=32)
    parser.add_argument("--max_tokens",     type=int,   default=MAX_TOKENS)
    parser.add_argument("--context_length", type=int,   default=MAX_CONTEXT_LEN)
    parser.add_argument("--temperature",    type=float, default=TEMPERATURE)
    args = parser.parse_args()

    MAX_TOKENS      = args.max_tokens
    MAX_CONTEXT_LEN = args.context_length
    TEMPERATURE     = args.temperature

    contracts_dir = Path(args.contracts)
    contracts = sorted([d for d in contracts_dir.iterdir()
                        if d.is_dir() and (d / "metadata.json").exists()])
    print(f"Found {len(contracts)} contracts")
    print(f"Generated dir : {args.generated}")
    print(f"Repaired dir  : {args.repaired}")

    print(f"Loading model {args.model} ...")
    client = VLLMClient(args.model, max_model_len=args.context_length)
    print("Model loaded.\n")

    repair_all(
        contracts     = contracts,
        client        = client,
        generated_dir = Path(args.generated),
        repaired_dir  = Path(args.repaired),
        work_dir      = Path(args.work_dir),
        batch_size    = args.batch_size,
    )