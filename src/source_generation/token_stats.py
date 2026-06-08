"""
token_stats.py  —  statistiche token per la pipeline function-level completion.

Nella nuova metodologia:
  - per ogni funzione con body non vuoto nel contratto si genera UN prompt:
      system + sorgente_con_TODO_per_quella_funzione (troncato a max_chars)
  - il ground truth è solo il BODY della funzione target
  - il completion token budget è max_model_len - prompt_tokens

Metriche calcolate (per singola chiamata LLM, non per contratto):
  - prompt_tokens       : system + user (sorgente con TODO)
  - body_tokens         : ground truth body della funzione
  - source_full_tokens  : sorgente completo del contratto
  - fns_per_contract    : numero di funzioni con body per contratto

Uso:
    python token_stats.py
    python token_stats.py --contracts-dir ../dataset/contracts_dataset_pass_08
    python token_stats.py --model Qwen/Qwen3-14B-AWQ --warn-threshold 32768
    python token_stats.py --percentiles 50 75 90 95 99 --show-outliers 10
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
from pathlib import Path
from typing import Callable


# ── Tokenizer ─────────────────────────────────────────────────────────────────

def make_counter(model_name: str | None) -> Callable[[str], int]:
    if model_name:
        try:
            from transformers import AutoTokenizer
            print(f"Loading tokenizer for {model_name} ...")
            tok = AutoTokenizer.from_pretrained(model_name)
            print("Tokenizer loaded.")
            return lambda text: len(tok.encode(text, add_special_tokens=False))
        except Exception as e:
            print(f"  [warn] Could not load tokenizer: {e} — using chars/4 estimate")
    return lambda text: max(1, len(text) // 4)


# ── Source loading ─────────────────────────────────────────────────────────────

def load_source(contract_dir: Path) -> str:
    for name in ("source.sol", "source.json"):
        p = contract_dir / name
        if not p.exists():
            continue
        text = p.read_text()
        if name == "source.json":
            try:
                data = json.loads(text)
                return "\n\n".join(
                    f"// === {fp} ===\n{fc.get('content','')}"
                    for fp, fc in data.get("sources", {}).items()
                )
            except json.JSONDecodeError:
                pass
        return text
    return ""


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


# ── Function extraction (mirrors generate_oneshot._find_functions) ─────────────

def _find_functions(source: str) -> list[dict]:
    results = []
    for m in re.finditer(r'\b(function)\s+\w+\s*\([^)]*\)[^;{]*', source):
        start = m.start()
        rest  = source[m.end():]
        brace = rest.find('{')
        if brace < 0:
            continue
        abs_b = m.end() + brace
        depth, i = 1, abs_b + 1
        while i < len(source) and depth > 0:
            if source[i] == '{': depth += 1
            elif source[i] == '}': depth -= 1
            i += 1
        close = i - 1
        body_content = source[abs_b + 1 : close]
        body_stripped = re.sub(r'//[^\n]*', '', body_content)
        body_stripped = re.sub(r'/\*.*?\*/', '', body_stripped, flags=re.DOTALL)
        if not body_stripped.strip():
            continue
        results.append({
            "fn_sig":     source[start:abs_b].strip(),
            "body_open":  abs_b,
            "body_close": close + 1,
            "body":       body_content,
        })
    return results


def _make_prompt_source(source: str, fn: dict, max_chars: int = 12000) -> str:
    """Mirrors generate_oneshot._truncate_source_for_prompt + comment header."""
    todo_body = "{\n        // TODO: implement this\n    }"
    source_with_todo = (
        source[:fn["body_open"]]
        + todo_body
        + source[fn["body_close"]:]
    )
    # Truncate around the function (same logic as generate_oneshot)
    if len(source_with_todo) <= max_chars:
        truncated = source_with_todo
    else:
        fn_line_start = source[:fn["body_open"]].count('\n')
        fn_line_end   = source[:fn["body_close"]].count('\n')
        lines         = source_with_todo.splitlines()
        context_win   = 60
        keep = set(range(len(lines[:30])))  # always keep top of file
        keep.update(range(max(0, fn_line_start - context_win),
                          fn_line_end + context_win))
        kept_lines = [l for i, l in enumerate(lines) if i in keep]
        truncated  = "\n".join(kept_lines)

    fn_name_m = re.search(r'\bfunction\s+(\w+)\s*\(', fn["fn_sig"])
    fn_name   = fn_name_m.group(1) if fn_name_m else "the function"
    header    = (
        f"// Complete ONLY the body of function `{fn_name}` marked with "
        f"`// TODO: implement this`.\n"
        f"// Output ONLY the body statements (no braces, no signature).\n\n"
    )
    return header + truncated


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


# ── Stats helpers ──────────────────────────────────────────────────────────────

def _stats(values: list[int], percentiles: list[int]) -> dict:
    if not values:
        return {}
    s = sorted(values)
    n = len(s)
    result = {
        "n":      n,
        "min":    s[0],
        "max":    s[-1],
        "mean":   round(statistics.mean(s)),
        "median": round(statistics.median(s)),
        "stdev":  round(statistics.stdev(s)) if n > 1 else 0,
    }
    for pct in percentiles:
        result[f"p{pct}"] = s[min(int(n * pct / 100), n - 1)]
    return result


def _print_stats(label: str, stats: dict) -> None:
    if not stats:
        print(f"  {label}: no data")
        return
    pct_keys = sorted(k for k in stats if k.startswith("p"))
    pct_str  = "  ".join(f"{k}={stats[k]:,}" for k in pct_keys)
    print(f"  {label:<28}  "
          f"n={stats['n']:>5,}  "
          f"min={stats['min']:>5,}  "
          f"mean={stats['mean']:>6,}  "
          f"median={stats['median']:>6,}  "
          f"max={stats['max']:>7,}  "
          f"σ={stats['stdev']:>5,}"
          + (f"  [{pct_str}]" if pct_str else ""))


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--contracts-dir",  type=Path,
                   default=Path(os.getenv("CONTRACTS_DIR",
                                "../dataset/contracts_dataset_pass_08")))
    p.add_argument("--model",          type=str, default=None,
                   help="Model name for exact tokenization. Omit for chars/4 estimate.")
    p.add_argument("--percentiles",    type=int, nargs="+", default=[75, 90, 95, 99])
    p.add_argument("--warn-threshold", type=int, default=None,
                   help="Context window limit (e.g. 32768). Warns if prompt exceeds it.")
    p.add_argument("--show-outliers",  type=int, default=0,
                   help="Show top-N longest prompts.")
    p.add_argument("--max-chars",      type=int, default=12000,
                   help="Max chars for source truncation in prompt (mirrors generate_oneshot).")
    p.add_argument("--output-json",    type=Path, default=None)
    args = p.parse_args()

    count = make_counter(args.model)

    if not args.contracts_dir.exists():
        print(f"contracts-dir not found: {args.contracts_dir}")
        return

    contract_dirs = sorted(
        d for d in args.contracts_dir.iterdir()
        if d.is_dir() and (d / "abi.json").exists()
    )
    print(f"\nContracts found:  {len(contract_dirs)}")
    print(f"Token counting:   "
          f"{'exact (' + args.model + ')' if args.model else 'estimate (chars/4)'}")
    print(f"Source max_chars: {args.max_chars:,}  (truncation window)\n")

    system_tokens = count(_SYSTEM)

    # Per-call buckets (one entry per function, not per contract)
    prompt_toks  = []   # system + user per singola funzione
    body_toks    = []   # ground truth body
    source_toks  = []   # sorgente completo (una entry per contratto)
    fns_per_contract = []

    outliers: list[tuple[int, str, str, str]] = []  # (tokens, addr, contract, fn)
    warnings: list[dict] = []
    skipped = 0

    for contract_dir in contract_dirs:
        meta   = load_json(contract_dir / "metadata.json")
        cname  = meta.get("contract_name", "Contract")
        source = load_source(contract_dir)
        if not source:
            skipped += 1
            continue

        tok_full = count(source)
        source_toks.append(tok_full)

        fns = _find_functions(source)
        if not fns:
            fns_per_contract.append(0)
            continue
        fns_per_contract.append(len(fns))

        for fn in fns:
            user_text = _make_prompt_source(source, fn, args.max_chars)
            tok_prompt = system_tokens + count(user_text)
            tok_body   = count(fn["body"])

            prompt_toks.append(tok_prompt)
            body_toks.append(tok_body)

            if args.show_outliers > 0:
                fn_name_m = re.search(r'\bfunction\s+(\w+)', fn["fn_sig"])
                fn_name   = fn_name_m.group(1) if fn_name_m else "?"
                outliers.append((tok_prompt, contract_dir.name[:16], cname, fn_name))

            if args.warn_threshold and tok_prompt > args.warn_threshold:
                fn_name_m = re.search(r'\bfunction\s+(\w+)', fn["fn_sig"])
                warnings.append({
                    "address":  contract_dir.name,
                    "contract": cname,
                    "function": fn_name_m.group(1) if fn_name_m else "?",
                    "tokens":   tok_prompt,
                })

    n_contracts = len(contract_dirs) - skipped
    n_calls     = len(prompt_toks)
    print(f"Processed:  {n_contracts} contracts  ({skipped} skipped — no source)")
    print(f"LLM calls:  {n_calls} total  "
          f"(avg {n_calls/n_contracts:.1f} functions/contract)\n")

    W = args.warn_threshold

    # ── Stats table ───────────────────────────────────────────────────────────
    sep = "─" * 110
    print(sep)
    print(f"  {'Metric':<28}  {'n':>5}  {'min':>5}  {'mean':>6}  "
          f"{'median':>6}  {'max':>7}  {'σ':>5}"
          + (f"  [percentiles]" if args.percentiles else ""))
    print(sep)

    for label, values in [
        ("prompt_tokens (system+user)",  prompt_toks),
        ("body_tokens (ground truth)",   body_toks),
        ("source_full_tokens",           source_toks),
        ("fns_per_contract",             fns_per_contract),
    ]:
        _print_stats(label, _stats(values, args.percentiles))

    # ── Context window fit ────────────────────────────────────────────────────
    if W and prompt_toks:
        over = sum(1 for v in prompt_toks if v > W)
        pct  = over / len(prompt_toks) * 100
        print(f"\n{sep}")
        print(f"Context window analysis  (threshold = {W:,} tokens)")
        print(f"  system prompt:               {system_tokens:,} tokens")
        print(f"  prompt > threshold:          {over}/{len(prompt_toks)}  ({pct:.1f}% of LLM calls)")
        stats = _stats(prompt_toks, [90, 95, 99])
        for pk in ["p90", "p95", "p99"]:
            if pk in stats:
                budget = W - stats[pk]
                flag   = "  OK" if budget > 0 else "  TRUNCATED"
                print(f"  At {pk} prompt ({stats[pk]:,} tok):  "
                      f"output budget = {budget:,} tok{flag}")

    # ── Histogram ─────────────────────────────────────────────────────────────
    if prompt_toks:
        print(f"\n{sep}")
        print("Prompt length distribution (tokens per LLM call):")
        vals   = sorted(prompt_toks)
        lo, hi = vals[0], vals[-1]
        n_bins = 12
        bin_w  = max(1, (hi - lo) // n_bins)
        bins   = [0] * n_bins
        for v in vals:
            bins[min(int((v - lo) / bin_w), n_bins - 1)] += 1
        max_c = max(bins)
        for i, c in enumerate(bins):
            lo_b = lo + i * bin_w
            hi_b = lo + (i + 1) * bin_w
            bar  = "█" * int(40 * c / max_c) if max_c else ""
            flag = " ⚠" if W and lo_b > W else ""
            print(f"  {lo_b:>7,}–{hi_b:>7,}  {c:>5}  {bar}{flag}")

    # ── Outliers ──────────────────────────────────────────────────────────────
    if args.show_outliers > 0 and outliers:
        print(f"\n{sep}")
        print(f"Top {args.show_outliers} longest prompts (per LLM call):")
        for tok, addr, cname_, fn in sorted(outliers, reverse=True)[:args.show_outliers]:
            flag = " ⚠" if W and tok > W else ""
            print(f"  {addr}…  {cname_:<25}  {fn:<20}  {tok:>7,} tok{flag}")

    # ── Warnings ──────────────────────────────────────────────────────────────
    if warnings:
        print(f"\n{sep}")
        print(f"  {len(warnings)} prompt(s) exceed threshold {W:,}:")
        for w in sorted(warnings, key=lambda x: -x["tokens"])[:20]:
            print(f"  {w['address'][:16]}…  {w['contract']:<25}  "
                  f"{w['function']:<20}  {w['tokens']:>7,} tok")
        if len(warnings) > 20:
            print(f"  ... and {len(warnings)-20} more")

    # ── JSON output ───────────────────────────────────────────────────────────
    if args.output_json:
        result = {
            "model":          args.model or "chars/4 estimate",
            "n_contracts":    n_contracts,
            "n_llm_calls":    n_calls,
            "system_tokens":  system_tokens,
            "warn_threshold": W,
            "stats": {
                "prompt_tokens": _stats(prompt_toks, args.percentiles),
                "body_tokens":   _stats(body_toks,   args.percentiles),
                "source_tokens": _stats(source_toks, args.percentiles),
                "fns_per_contract": _stats(fns_per_contract, args.percentiles),
            },
            "warnings": warnings,
        }
        args.output_json.write_text(json.dumps(result, indent=2))
        print(f"\n  Saved -> {args.output_json}")


if __name__ == "__main__":
    main()