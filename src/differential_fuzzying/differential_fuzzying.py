"""
differential_fuzzing.py
=======================

Differential fuzzing of original vs LLM-generated Solidity contracts
using Echidna in assertion mode.

For each contract the script:
  1. Picks the best compiled sample from repaired_oneshot (highest
     pass_rate from replay, or first compiled sample as fallback).
  2. Generates a self-contained differential harness .sol that:
       - Embeds both the original and generated source inline.
       - Renames the generated contract class to avoid name collisions.
       - Deploys both with identical constructor arguments.
       - Exposes one echidna_diff_<fn>() property per state-changing ABI
         function: calls both contracts with the same fuzzed inputs and
         asserts that (a) revert status matches and (b) return data matches.
  3. Writes an echidna.yaml config tuned for differential testing.
  4. Runs echidna and captures stdout/stderr.
  5. Parses the results and writes a per-contract JSON report.

Directory layout
----------------
  <diff_dir>/
    <address>/
      harness.sol          generated differential harness
      echidna.yaml         echidna config
      echidna_output.txt   raw echidna stdout+stderr
      result.json          structured result

Usage
-----
  python differential_fuzzing.py [options]

  # Test first 10 contracts (for development / smoke test):
  python differential_fuzzing.py --n-contracts 10

  # Full run:
  python differential_fuzzing.py

  # Custom paths:
  python differential_fuzzing.py \\
      --dataset   ../dataset/contracts_dataset_1to20 \\
      --generated ../source_generation/repaired_oneshot \\
      --diff-dir  ./differential_fuzzing \\
      --n-contracts 10 \\
      --test-limit 10000 \\
      --timeout 120

Dependencies
------------
  echidna   must be on PATH  (https://github.com/crytic/echidna)
  solc      must be on PATH or managed via solc-select
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR / ".." / "utils"))

from source_utils import _find_functions, _main_source_text, _load_json
from hardhat_utils import primary_compiler_version, prepare_and_compile

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# python3 differential_fuzzying.py --from-results ../transaction_replay/generated_replay_results.json --force


DEFAULT_DATASET   = str(_SCRIPT_DIR / ".." / "dataset" / "contracts_dataset_1to20")
DEFAULT_GENERATED = str(_SCRIPT_DIR / ".." / "source_generation" / "generated_oneshot_Qwen3_6-27B-UD-Q4_K_XL_gguf")
DEFAULT_DIFF_DIR  = str(_SCRIPT_DIR / "fuzzying_results_Qwen3_6-27B-UD-Q4_K_XL_gguf")
DEFAULT_WORK_DIR  = str(_SCRIPT_DIR / ".." / "dataset" / "hardhat_workspace_replay")
DEFAULT_TEST_LIMIT = 10_000   # echidna calls per property
DEFAULT_TIMEOUT    = 120      # seconds per contract
DEFAULT_SEQ_LEN    = 20       # echidna sequence length

# echidna binary name — the npm @crytic/echidna package ships a broken wrapper;
# the actual static binary is exposed as echidna-bin after running install_echidna.sh
ECHIDNA_CMD = "echidna-bin"


# ---------------------------------------------------------------------------
# Source loading helpers
# ---------------------------------------------------------------------------

def _load_flat_source(contract_dir: Path) -> str:
    """Return the full flat source (single string) for a contract directory."""
    jsn = contract_dir / "source.json"
    if jsn.exists():
        data = json.loads(jsn.read_text())
        return "\n\n".join(
            fc.get("content", "")
            for fc in data.get("sources", {}).values()
        )
    sol = contract_dir / "source.sol"
    if sol.exists():
        return sol.read_text()
    return ""


def _load_flat_sample(sample_path: Path, contract_name: str) -> str:
    """
    Return flat source for a generated sample.
    For multi-file (sample.json), concatenates all source files.
    """
    if sample_path.suffix == ".json":
        try:
            data = json.loads(sample_path.read_text())
            return "\n\n".join(
                fc.get("content", "")
                for fc in data.get("sources", {}).values()
            )
        except Exception:
            return ""
    return sample_path.read_text()


# ---------------------------------------------------------------------------
# Best-sample selection
# ---------------------------------------------------------------------------

def _pick_best_sample(gen_contract_dir: Path, k: int) -> Optional[int]:
    """
    Return the sample index (1-based) of the best compiled sample.
    Priority:
      1. Sample with highest pass_rate in replay_sample_*_checkpoint_<k>.json
      2. First compiled sample index from result.json
    """
    best_idx  = None
    best_rate = -1.0

    for p in sorted(gen_contract_dir.glob(f"replay_sample_*_checkpoint_{k}.json")):
        m = re.search(r"replay_sample_(\d+)_checkpoint", p.name)
        if not m:
            continue
        try:
            ckpt = json.loads(p.read_text())
            if ckpt.get("state") != "DONE":
                continue
            rate = ckpt.get("pass_rate", 0) or 0
            if rate > best_rate:
                best_rate = rate
                best_idx  = int(m.group(1))
        except Exception:
            pass

    if best_idx is not None:
        return best_idx

    # Fallback: first compiled sample from result.json
    result_path = gen_contract_dir / "result.json"
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text())
            for s in result.get("samples", []):
                if s.get("compiled", False):
                    return s["idx"]
        except Exception:
            pass
    return None



def _all_compiled_sample_indices(gen_contract_dir: Path) -> list[int]:
    """Return all compiled sample indices from result.json, sorted ascending."""
    result_path = gen_contract_dir / "result.json"
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text())
            indices = [s["idx"] for s in result.get("samples", [])
                       if s.get("compiled", False)]
            if indices:
                return sorted(indices)
        except Exception:
            pass
    # Fallback: scan for sample files
    indices = set()
    for ext in (".sol", ".json"):
        for p in gen_contract_dir.glob(f"sample_*{ext}"):
            m = re.match(r"sample_(\d+)", p.stem)
            if m:
                indices.add(int(m.group(1)))
    return sorted(indices)


def _fn_harness(contract_name, sample_idx, fn, abi, constructor_args,
                compiler_version):
    """Generate a single-function differential harness for one ABI function."""
    ver    = compiler_version.lstrip("v").split("+")[0] or "0.8.0"
    inputs = fn.get("inputs", [])
    name   = fn["name"]

    wrapper = _WRAPPER_TEMPLATE.format(
        fn_name     = name,
        params      = _fn_param_list(inputs),
        encode_call = _abi_encode_call(name, inputs),
    )

    return (
        f"// SPDX-License-Identifier: UNLICENSED\n"
        f"// Focused harness: {contract_name}.{name}  (sample_{sample_idx})\n"
        f"pragma solidity >={ver};\n\n"
        f"import {{{contract_name} as {contract_name}_V1}} from \"./orig.sol\";\n"
        f"import {{{contract_name} as {contract_name}_V2}} from \"./gen.sol\";\n\n"
        f"contract DiffHarness {{\n\n"
        f"    {contract_name}_V1 internal orig;\n"
        f"    {contract_name}_V2 internal gen;\n"
        f"    bool internal _diverged;\n\n"
        f"    constructor() {{\n"
        f"        orig = new {contract_name}_V1({_constructor_args_literal(abi, constructor_args)});\n"
        f"        gen  = new {contract_name}_V2({_constructor_args_literal(abi, constructor_args)});\n"
        f"        _diverged = false;\n"
        f"    }}\n\n"
        f"    function echidna_no_divergence() public view returns (bool) {{\n"
        f"        return !_diverged;\n"
        f"    }}\n\n"
        f"{wrapper}"
        f"    function _eq(bytes memory a, bytes memory b) internal pure returns (bool) {{\n"
        f"        return keccak256(a) == keccak256(b);\n"
        f"    }}\n"
        f"}}\n"
    )


def _find_sample_path(gen_contract_dir: Path, idx: int) -> Optional[Path]:
    for ext in (".json", ".sol"):
        p = gen_contract_dir / f"sample_{idx}{ext}"
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------------------
# ABI helpers
# ---------------------------------------------------------------------------

def _state_changing_fns(abi: list) -> list[dict]:
    """Return ABI entries for non-view/pure functions with inputs."""
    return [
        e for e in abi
        if e.get("type") == "function"
        and e.get("stateMutability") not in ("view", "pure")
    ]


def _solidity_type(t: str, components: Optional[list] = None) -> str:
    """Expand tuple types to their canonical Solidity form."""
    if t == "tuple" or t.startswith("tuple"):
        if not components:
            return "()"
        inner = ", ".join(
            _solidity_type(c["type"], c.get("components")) for c in components
        )
        suffix = t[len("tuple"):]
        return f"({inner}){suffix}"
    return t


def _fn_param_list(inputs: list) -> str:
    """Build 'type name, type name' parameter list for a Solidity function."""
    parts = []
    for i, inp in enumerate(inputs):
        t    = _solidity_type(inp["type"], inp.get("components"))
        name = inp.get("name") or f"p{i}"
        # avoid Solidity keyword collisions
        if name in ("from", "to", "value", "data", "type", "address"):
            name = f"_{name}"
        parts.append(f"{t} {name}")
    return ", ".join(parts)


def _fn_arg_list(inputs: list) -> str:
    """Build 'name, name' argument list matching _fn_param_list."""
    parts = []
    for i, inp in enumerate(inputs):
        name = inp.get("name") or f"p{i}"
        if name in ("from", "to", "value", "data", "type", "address"):
            name = f"_{name}"
        parts.append(name)
    return ", ".join(parts)


def _abi_encode_call(fn_name: str, inputs: list) -> str:
    """Generate abi.encodeWithSignature call or abi.encode with selector."""
    if not inputs:
        sig = f"{fn_name}()"
        return f'abi.encodeWithSignature("{sig}")'
    types = ",".join(
        _solidity_type(inp["type"], inp.get("components")) for inp in inputs
    )
    args  = _fn_arg_list(inputs)
    sig   = f"{fn_name}({types})"
    return f'abi.encodeWithSignature("{sig}", {args})'


# ---------------------------------------------------------------------------
# Harness generation  (correct echidna pattern)
# ---------------------------------------------------------------------------
#
# Echidna invariants MUST have no arguments and return bool.
# The fuzzing works as follows:
#   1. Echidna randomly calls the *wrapper* functions (which DO have args)
#      to drive state changes on both contracts simultaneously.
#   2. After each call sequence, echidna checks the no-arg *invariants*
#      to detect any divergence between the two contracts.
#
# Per-function wrapper:  call_<fn>(args) — mirrors the call on both contracts,
#                        records whether they diverged.
# Global invariant:      echidna_no_divergence() — returns false if any
#                        wrapper recorded a divergence.

_HARNESS_TEMPLATE = """\
// SPDX-License-Identifier: UNLICENSED
// Auto-generated differential fuzzing harness
// Original:  {contract_name}_V1  ({orig_filename})
// Generated: {contract_name}_V2  (sample_{sample_idx})
// solc:      {compiler_version}
pragma solidity {pragma};

import {{{contract_name} as {contract_name}_V1}} from "./{orig_filename}";
import {{{contract_name} as {contract_name}_V2}} from "./{gen_filename}";

contract DiffHarness {{

    {contract_name}_V1 internal orig;
    {contract_name}_V2 internal gen;

    // Set to true the first time a wrapper detects divergence.
    bool internal _diverged;

    constructor() {{
        orig = new {contract_name}_V1({constructor_args});
        gen  = new {contract_name}_V2({constructor_args});
        _diverged = false;
    }}

    // ── Global invariant (no arguments — required by echidna) ─────────────
    /// @notice Fails if any wrapper call produced different outcomes on
    ///         original vs generated contract.
    function echidna_no_divergence() public view returns (bool) {{
        return !_diverged;
    }}

    // ── Per-function wrappers (echidna fuzzes these with random inputs) ────
{wrappers}
    // ── Helper ────────────────────────────────────────────────────────────
    function _eq(bytes memory a, bytes memory b) internal pure returns (bool) {{
        return keccak256(a) == keccak256(b);
    }}
}}
"""

_WRAPPER_TEMPLATE = """\
    /// @notice Wrapper for {fn_name} — echidna calls this with fuzzed inputs.
    ///         Records divergence if status or return data differ.
    function call_{fn_name}({params}) public {{
        (bool ok1, bytes memory r1) = address(orig).call({encode_call});
        (bool ok2, bytes memory r2) = address(gen).call({encode_call});
        if (ok1 != ok2 || (ok1 && !_eq(r1, r2))) {{
            _diverged = true;
        }}
    }}
"""


def generate_harness(
    contract_name:    str,
    sample_idx:       int,
    abi:              list,
    constructor_args: list,
    compiler_version: str,
) -> str:
    """
    Generate a differential harness using the correct echidna pattern:
      - No-argument invariant `echidna_no_divergence()` that echidna checks.
      - Per-function wrapper `call_<fn>(args)` that echidna fuzzes as input.
      - Both contracts deployed at construction with identical args.
      - Separate import files (diffusc-style) to avoid naming collisions.
    """
    ver    = compiler_version.lstrip("v").split("+")[0] or "0.8.0"
    pragma = f">={ver}"

    fns = _state_changing_fns(abi)[:10]
    wrappers = []
    seen_names: set[str] = set()

    for fn in fns:
        name   = fn.get("name", "")
        inputs = fn.get("inputs", [])
        if not name or name in seen_names:
            continue
        # Skip types echidna can't fuzz: dynamic bytes, tuples, strings
        types = [inp["type"] for inp in inputs]
        if any(t.startswith("tuple") or
               t == "bytes" or
               (t.startswith("bytes") and not re.match(r'bytes\d+$', t)) or
               t == "string"
               for t in types):
            continue
        seen_names.add(name)
        wrappers.append(_WRAPPER_TEMPLATE.format(
            fn_name     = name,
            params      = _fn_param_list(inputs),
            encode_call = _abi_encode_call(name, inputs),
        ))

    # Always include the invariant even when no wrappers could be generated.
    # Echidna will still verify echidna_no_divergence() holds (trivially true
    # since _diverged is never set), which at least confirms both contracts
    # deploy and the harness compiles correctly.

    return _HARNESS_TEMPLATE.format(
        contract_name    = contract_name,
        sample_idx       = sample_idx,
        orig_filename    = "orig.sol",
        gen_filename     = "gen.sol",
        compiler_version = compiler_version,
        pragma           = pragma,
        constructor_args = _constructor_args_literal(abi, constructor_args),
        wrappers         = "\n".join(wrappers) if wrappers else
                           "    // No fuzzable wrappers (all fns use complex types)\n",
    )


# ---------------------------------------------------------------------------
# Constructor args serialisation
# ---------------------------------------------------------------------------

def _constructor_args_literal(abi: list, args: list) -> str:
    """
    Convert a constructor args list to a Solidity literal argument string.
    e.g. ["0xf39F...", 1000, False] -> '0xf39F..., 1000, false'
    """
    if not args:
        return ""
    ctor   = next((e for e in abi if e.get("type") == "constructor"), None)
    inputs = ctor.get("inputs", []) if ctor else []
    parts  = []
    for i, arg in enumerate(args):
        t = inputs[i]["type"] if i < len(inputs) else "uint256"
        if t == "address":
            parts.append(str(arg))
        elif t.startswith("uint") or t.startswith("int"):
            parts.append(str(arg))
        elif t == "bool":
            parts.append("true" if arg else "false")
        elif t == "string":
            parts.append(f'"{str(arg).replace(chr(34), chr(92)+chr(34))}"')
        elif t.startswith("bytes"):
            parts.append(str(arg) if str(arg).startswith("0x") else f'hex"{arg}"')
        else:
            parts.append(str(arg))
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Echidna config
# ---------------------------------------------------------------------------

def _echidna_config(
    contract:    str,
    test_limit:  int,
    seq_len:     int,
    solc_ver:    str,
) -> str:
    return f"""\
# Auto-generated echidna config for differential fuzzing
testMode: "property"
testLimit: {test_limit}
seqLen: {seq_len}
contractAddr: "0x00a329c0648769a73afac7f9381e08fb43dbea72"
deployer: "0x00a329c0648769a73afac7f9381e08fb43dbea70"
sender: ["0x00a329c0648769a73afac7f9381e08fb43dbea70"]
coverage: true
stopOnFail: false
quiet: false
"""



# ---------------------------------------------------------------------------
# Output parsing and property extraction
# ---------------------------------------------------------------------------

def _extract_property_names(harness_source: str) -> list[str]:
    """
    Extract echidna invariant names (no-arg echidna_* functions) from harness.
    Also returns wrapper count for display purposes via the list length heuristic.
    """
    return re.findall(r'\bfunction\s+(echidna_\w+)\s*\(\s*\)', harness_source)


def _extract_wrapper_count(harness_source: str) -> int:
    """Count call_* wrapper functions in the harness."""
    return len(re.findall(r'\bfunction\s+call_\w+\s*\(', harness_source))


def parse_echidna_output(json_stdout: str, stderr: str, out_dir: Path) -> dict:
    """
    Parse echidna --format text output.

    Text format (stable across echidna versions):
      echidna_no_divergence: passed! 🎉
      echidna_no_divergence: failed!💥
        Call sequence:
          DiffHarness.call_transfer(...)
          ...

    This is simpler and more reliable than --format json whose schema
    varies between echidna 2.0.x versions.
    """
    stdout = json_stdout or ""   # we capture stdout in json_stdout regardless of format

    result: dict = {
        "properties":             {},
        "passed":                 0,
        "failed":                 0,
        "timed_out":              "TIMEOUT" in stderr,
        "echidna_missing":        ECHIDNA_CMD + " not found" in stderr,
        "compile_failed":         "compile failed" in stderr.lower(),
        "total":                  0,
        "fns_called":             [],
        "n_fns_called":           0,
        "fn_call_counts":         {},
        "fn_unique_inputs":       {},
        "branch_coverage_lines":  None,
    }

    # Parse pass/fail lines — only echidna_* invariants appear here in property mode
    # call_* wrappers are state drivers, not properties
    for m in re.finditer(r'(echidna_\w+):\s+(passed|failed)', stdout):
        name   = m.group(1)
        status = m.group(2)
        entry: dict = {"status": status}
        if status == "passed":
            result["passed"] += 1
        else:
            result["failed"] += 1
            # Extract call sequence that follows the failed! line
            fail_pos = m.end()
            snippet  = stdout[fail_pos:fail_pos + 2000]
            seq_m = re.search(r'Call sequence:(.*?)(?=\nechidna_|\nUnique |\Z)',
                              snippet, re.DOTALL)
            if seq_m:
                # Strip trailing blank lines and stats footer
                cex = seq_m.group(1).strip()
                cex = re.sub(r'\n+(Unique instructions|Unique codehashes|Corpus size|Seed):.*', '', cex)
                entry["counterexample"] = cex.strip()
        result["properties"][name] = entry

    result["total"] = len(result["properties"])

    # Extract wrappers called from any call sequences printed in output
    # (echidna prints sequences for failed properties)
    fn_counts: dict[str, int] = {}
    for m in re.finditer(r'\b(call_\w+)\s*\(', stdout):
        fn = m.group(1)
        fn_counts[fn] = fn_counts.get(fn, 0) + 1

    # Also count wrappers echidna listed as run (they appear as "call_x(...): passed!")
    # in assertion mode — in property mode this won't appear, but handle both
    for m in re.finditer(r'(call_\w+)\(.*?\):\s+(?:passed|failed)', stdout):
        fn = m.group(1)
        fn_counts[fn] = fn_counts.get(fn, 0) + 1

    result["fns_called"]     = sorted(fn_counts.keys())
    result["n_fns_called"]   = len(fn_counts)
    result["fn_call_counts"] = fn_counts

    coverage_dir = out_dir / "corpus" / "coverage"
    if coverage_dir.exists():
        covered: set[str] = set()
        for cov_file in coverage_dir.glob("covered.*.txt"):
            try:
                for line in cov_file.read_text().splitlines():
                    if line.strip():
                        covered.add(line.strip())
            except Exception:
                pass
        result["branch_coverage_lines"] = len(covered)

    return result


# ---------------------------------------------------------------------------
# Echidna runner
# ---------------------------------------------------------------------------

def run_echidna(
    orig_source:  str,
    gen_source:   str,
    harness_src:  str,
    contract:     str,
    config_path:  Path,
    work_dir:     Path,
    out_dir:      Path,
    timeout:      int,
    meta:         dict,
) -> tuple[int, str, str]:
    """
    Compile and fuzz in an isolated subdirectory so concurrent runs never
    conflict on work_dir/contracts/ or work_dir/artifacts/.

    Each run gets:
      <out_dir>/run/contracts/  — orig.sol, gen.sol, DiffHarness.sol
      <out_dir>/run/artifacts/  — compiled by npx hardhat compile
      <out_dir>/run/node_modules -> symlink to work_dir/node_modules
      <out_dir>/run/package.json  — copied from work_dir
      <out_dir>/run/hardhat.config.js — generated for this compiler version

    This is safe for parallel execution: every run has its own filesystem
    subtree; the only shared resource is work_dir/node_modules which is
    read-only during compilation.
    """
    import shutil as _shutil
    from hardhat_utils import (
        primary_compiler_version, install_npm_deps,
        detect_remappings, hardhat_config,
    )

    run_dir = out_dir / "run"
    if run_dir.exists():
        _shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    # Symlink node_modules from work_dir (read-only — safe for concurrency)
    nm_src = work_dir / "node_modules"
    if not nm_src.exists():
        return 1, "", f"node_modules not found in work_dir: {work_dir}"
    (run_dir / "node_modules").symlink_to(nm_src.resolve())

    # Copy package.json from work_dir so Hardhat uses the same project identity
    pkg_src = work_dir / "package.json"
    if pkg_src.exists():
        _shutil.copy2(pkg_src, run_dir / "package.json")
    else:
        (run_dir / "package.json").write_text(
            '{"name":"diff-harness","version":"1.0.0","private":true}'
        )

    # Write source files
    contracts_dir = run_dir / "contracts"
    contracts_dir.mkdir()
    (contracts_dir / "orig.sol").write_text(orig_source)
    (contracts_dir / "gen.sol").write_text(gen_source)
    harness_path = contracts_dir / "DiffHarness.sol"
    harness_path.write_text(harness_src)

    # Generate hardhat.config.js
    all_texts = [orig_source, gen_source, harness_src]
    # Don't call install_npm_deps — all packages are already in the symlinked node_modules
    remaps = detect_remappings(run_dir, all_texts)
    cfg    = hardhat_config(meta, source_texts=all_texts, remappings=remaps or None)
    (run_dir / "hardhat.config.js").write_text(cfg)

    # Compile
    compile_proc = subprocess.run(
        ["npx", "hardhat", "compile", "--force"],
        cwd=run_dir, capture_output=True, text=True, timeout=120,
    )
    if compile_proc.returncode != 0:
        err = (
            f"Hardhat compile failed:\n"
            f"STDOUT: {compile_proc.stdout[-400:]}\n"
            f"STDERR: {compile_proc.stderr[-400:]}"
        )
        return compile_proc.returncode, "", err

    artifacts_dir = run_dir / "artifacts"
    corpus_dir    = out_dir / "corpus"
    corpus_dir.mkdir(exist_ok=True)

    crytic_args = (
        "--compile-force-framework hardhat "
        "--hardhat-ignore-compile "
        f"--hardhat-artifacts-directory {artifacts_dir}"
    )
    cmd = [
        ECHIDNA_CMD,
        str(harness_path),
        "--contract",    contract,
        "--config",      str(config_path),
        "--format",      "text",
        "--corpus-dir",  str(corpus_dir),
        "--crytic-args", crytic_args,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output = True,
            text           = True,
            timeout        = timeout,
            cwd            = run_dir,
        )
        return proc.returncode, proc.stdout + proc.stderr, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"TIMEOUT after {timeout}s"
    except FileNotFoundError:
        return -2, "", (
            f"{ECHIDNA_CMD} not found on PATH.\n"
            "Install: npm install -g @crytic/echidna && "
            "cp $(npm root -g)/@crytic/echidna/bin/echidna/2.0.2/Ubuntu-18.04/echidna-test "
            f"/usr/local/bin/{ECHIDNA_CMD}"
        )
    except Exception as e:
        return -3, "", str(e)


# ---------------------------------------------------------------------------
# Per-contract orchestration (parallel per-sample, per-function)
# ---------------------------------------------------------------------------

def _fail(address: str, reason: str, result_path: Path) -> dict:
    result = {"address": address, "state": "FAILED", "reason": reason}
    result_path.write_text(json.dumps(result, indent=2))
    return result


def fuzz_contract(
    address:      str,
    dataset_dir:  Path,
    gen_dir:      Path,
    diff_dir:     Path,
    work_dir:     Path,
    replay_k:     int,
    test_limit:   int,
    seq_len:      int,
    timeout:      int,
    force:        bool = False,
    verbose:      bool = False,
    workers:      int  = 4,
) -> dict:
    """
    Run per-sample × per-function differential fuzzing for one contract.

    For each compiled sample and each fuzzable function echidna is run once
    with a focused single-function harness.  (sample, function) pairs are
    dispatched to a ThreadPoolExecutor so `workers` runs proceed concurrently.
    Each run compiles in its own isolated <out_dir>/sample_N/fn/run/ directory
    so there are no filesystem races.

    Metrics:
      fn_pass_rate[f]    fraction of k samples with no counterexample for f
      contract_pass_rate fraction of k samples where ALL functions pass
      pass_at_k          True if any sample had all functions pass
      fn_coverage        fuzzable fns / total state-changing fns
    """
    out_dir = diff_dir / address
    out_dir.mkdir(parents=True, exist_ok=True)

    result_path = out_dir / "result.json"
    if result_path.exists() and not force:
        try:
            return json.loads(result_path.read_text())
        except Exception:
            pass

    original_dir     = dataset_dir / address
    gen_contract_dir = gen_dir / address

    meta = _load_json(original_dir / "metadata.json")
    if not meta:
        return _fail(address, "no metadata.json", result_path)

    contract_name = meta.get("contract_name", "Contract")
    compiler_ver  = primary_compiler_version(meta, [])

    abi: list = []
    abi_path = original_dir / "abi.json"
    if abi_path.exists():
        try:
            abi = json.loads(abi_path.read_text())
        except Exception:
            pass

    # Fuzzable functions: state-changing, no complex input types
    seen_names: set[str] = set()
    fuzzable_fns = []
    for fn in _state_changing_fns(abi):
        name = fn.get("name", "")
        if not name or name in seen_names:
            continue
        types = [inp["type"] for inp in fn.get("inputs", [])]
        if any(t.startswith("tuple") or t == "bytes" or
               (t.startswith("bytes") and not re.match(r"bytes\d+$", t)) or
               t == "string" for t in types):
            continue
        seen_names.add(name)
        fuzzable_fns.append(fn)

    if not fuzzable_fns:
        return _fail(address, "no fuzzable functions (all use complex types)",
                     result_path)

    all_indices = _all_compiled_sample_indices(gen_contract_dir)
    if not all_indices:
        return _fail(address, "no compiled samples found", result_path)

    ckpt_path = original_dir / f"replay_checkpoint_{replay_k}.json"
    constructor_args: list = []
    if ckpt_path.exists():
        try:
            ckpt = json.loads(ckpt_path.read_text())
            constructor_args = ckpt.get("constructor_args") or []
        except Exception:
            pass

    original_source = _load_flat_source(original_dir)
    if not original_source:
        return _fail(address, "could not load original source", result_path)

    fn_names = [fn["name"] for fn in fuzzable_fns]
    k        = len(all_indices)
    total_runs = k * len(fuzzable_fns)
    print(f"  {contract_name}: {k} samples × {len(fuzzable_fns)} fns "
          f"= {total_runs} runs  (workers={workers})")

    # ── Define the unit of work ───────────────────────────────────────────
    def _run_one(sample_idx: int, fn: dict) -> tuple[int, str, bool, float]:
        """Run echidna for one (sample, function) pair. Returns (sample_idx, fn_name, passed, elapsed)."""
        fn_name     = fn["name"]
        sample_path = _find_sample_path(gen_contract_dir, sample_idx)
        fn_out      = out_dir / f"sample_{sample_idx}" / fn_name
        fn_out.mkdir(parents=True, exist_ok=True)
        fn_ckpt     = fn_out / "result.json"

        # Per-(sample, fn) checkpoint
        if fn_ckpt.exists() and not force:
            try:
                cached = json.loads(fn_ckpt.read_text())
                return sample_idx, fn_name, cached.get("passed", False), 0.0
            except Exception:
                pass

        if sample_path is None:
            fn_ckpt.write_text(json.dumps({"passed": False, "reason": "sample file missing"}))
            return sample_idx, fn_name, False, 0.0

        generated_source = _load_flat_sample(sample_path, contract_name)
        if not generated_source:
            fn_ckpt.write_text(json.dumps({"passed": False, "reason": "could not load sample"}))
            return sample_idx, fn_name, False, 0.0

        harness     = _fn_harness(contract_name, sample_idx, fn, abi,
                                  constructor_args, compiler_ver)
        config      = _echidna_config("DiffHarness", test_limit, seq_len, compiler_ver)
        config_path = fn_out / "echidna.yaml"
        config_path.write_text(config)
        (fn_out / "harness.sol").write_text(harness)

        t0 = time.time()
        rc, stdout, stderr = run_echidna(
            orig_source  = original_source,
            gen_source   = generated_source,
            harness_src  = harness,
            contract     = "DiffHarness",
            config_path  = config_path,
            work_dir     = work_dir,
            out_dir      = fn_out,
            timeout      = timeout,
            meta         = meta,
        )
        elapsed = round(time.time() - t0, 1)
        (fn_out / "echidna_output.txt").write_text(stdout)

        parsed  = parse_echidna_output(stdout, stderr, fn_out)
        prop    = parsed["properties"].get("echidna_no_divergence", {})
        passed  = (prop.get("status") == "passed" or
                   (rc == 0 and parsed["failed"] == 0 and parsed["passed"] > 0))

        fn_ckpt.write_text(json.dumps({
            "fn_name":        fn_name,
            "sample_idx":     sample_idx,
            "passed":         passed,
            "elapsed_s":      elapsed,
            "rc":             rc,
            "counterexample": prop.get("counterexample"),
        }, indent=2))
        return sample_idx, fn_name, passed, elapsed

    # ── Dispatch all (sample, fn) pairs ───────────────────────────────────
    # Results matrix: fn_name -> {sample_idx: bool}
    fn_results: dict[str, dict[int, bool]] = {fn["name"]: {} for fn in fuzzable_fns}

    jobs = [(s, fn) for s in all_indices for fn in fuzzable_fns]

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_run_one, s, fn): (s, fn) for s, fn in jobs}
            completed = 0
            for fut in as_completed(futures):
                completed += 1
                try:
                    s_idx, fn_name, passed, elapsed = fut.result()
                except Exception as e:
                    s_idx, fn   = futures[fut]
                    fn_name     = fn["name"]
                    passed, elapsed = False, 0.0
                    print(f"    sample_{s_idx} {fn_name}: ERROR {e}")
                fn_results[fn_name][s_idx] = passed
                icon = "✓" if passed else "✗"
                if verbose or not passed:
                    print(f"    sample_{s_idx} {fn_name}: {icon}  ({elapsed}s)")
                if completed % 10 == 0 or completed == len(jobs):
                    done_count = sum(1 for fr in fn_results.values()
                                     for v in fr.values() if v)
                    print(f"    [{completed}/{len(jobs)}]  passed={done_count}")
    else:
        for s_idx, fn in jobs:
            _, fn_name, passed, elapsed = _run_one(s_idx, fn)
            fn_results[fn_name][s_idx] = passed
            icon = "✓" if passed else "✗"
            if verbose or not passed:
                print(f"    sample_{s_idx} {fn_name}: {icon}  ({elapsed}s)")

    # ── Aggregate ─────────────────────────────────────────────────────────
    fn_pass_rates = {
        fn_name: round(sum(fn_results[fn_name].get(s, False) for s in all_indices) / k, 4)
        for fn_name in fn_names
    }
    fn_pass_at_k = {
        fn_name: any(fn_results[fn_name].get(s, False) for s in all_indices)
        for fn_name in fn_names
    }
    sample_all_pass = [
        all(fn_results[fn_name].get(s, False) for fn_name in fn_names)
        for s in all_indices
    ]
    contract_pass_rate = round(sum(sample_all_pass) / k, 4) if k else 0.0
    pass_at_k          = any(sample_all_pass)
    fns_fuzzed         = len(fuzzable_fns)
    total_fns_abi      = len(_state_changing_fns(abi))
    fn_coverage        = round(fns_fuzzed / total_fns_abi, 4) if total_fns_abi else 0.0

    print(f"  → {contract_name}  "
          f"contract_pass@{k}={contract_pass_rate:.0%}  "
          f"pass_at_k={'✓' if pass_at_k else '✗'}")

    result = {
        "address":             address,
        "contract_name":       contract_name,
        "compiler_ver":        compiler_ver,
        "state":               "DONE",
        "timestamp":           datetime.utcnow().isoformat(),
        "k":                   k,
        "sample_indices":      all_indices,
        "fns_fuzzed":          fn_names,
        "n_fns_fuzzed":        fns_fuzzed,
        "n_fns_total":         total_fns_abi,
        "fn_coverage":         fn_coverage,
        "fn_pass_rates":       fn_pass_rates,
        "fn_pass_at_k":        fn_pass_at_k,
        "contract_pass_rate":  contract_pass_rate,
        "pass_at_k":           pass_at_k,
        "sample_all_pass":     sample_all_pass,
    }
    result_path.write_text(json.dumps(result, indent=2))
    return result



    result = {"address": address, "state": "FAILED", "reason": reason}
    result_path.write_text(json.dumps(result, indent=2))
    return result


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

def _format_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def print_summary(results: list[dict], total_elapsed: float) -> None:
    done   = [r for r in results if r.get("state") == "DONE"]
    failed = [r for r in results if r.get("state") == "FAILED"]

    contract_rates = [r["contract_pass_rate"] for r in done
                      if r.get("contract_pass_rate") is not None]
    pass_at_k_count = sum(1 for r in done if r.get("pass_at_k"))

    # Aggregate fn_pass_rates across all contracts
    all_fn_rates = [v for r in done
                    for v in r.get("fn_pass_rates", {}).values()]

    print("\n" + "═" * 64)
    print("  DIFFERENTIAL FUZZING SUMMARY")
    print("═" * 64)
    print(f"  Contracts processed    : {len(results)}")
    print(f"  Completed              : {len(done)}")
    print(f"  Setup failed           : {len(failed)}")
    print()
    if done:
        k_vals = [r.get("k", 0) for r in done]
        print(f"  Samples per contract   : {min(k_vals)}–{max(k_vals)}")
        print(f"  Mean fn pass rate      : "
              f"{sum(all_fn_rates)/len(all_fn_rates):.1%}" if all_fn_rates else "  n/a")
        print(f"  Mean contract pass@k   : "
              f"{sum(contract_rates)/len(contract_rates):.1%}" if contract_rates else "  n/a")
        print(f"  pass_at_k (≥1 sample)  : "
              f"{pass_at_k_count}/{len(done)}  ({pass_at_k_count/len(done):.0%})")
    print(f"  Total time             : {_format_elapsed(total_elapsed)}")

    if failed:
        from collections import Counter
        reasons = Counter(r.get("reason", "?")[:50] for r in failed)
        print(f"\n  Setup failure reasons:")
        for reason, count in reasons.most_common(5):
            print(f"    {count:>4}×  {reason}")
    print("═" * 64)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Differential fuzzing of original vs generated contracts with Echidna.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--dataset", type=Path, default=Path(DEFAULT_DATASET),
        help="Original dataset directory.",
    )
    p.add_argument(
        "--generated", type=Path, default=Path(DEFAULT_GENERATED),
        help="Generated/repaired contracts directory.",
    )
    p.add_argument(
        "--diff-dir", type=Path, default=Path(DEFAULT_DIFF_DIR),
        dest="diff_dir",
        help="Output directory for harnesses and results.",
    )
    p.add_argument(
        "--work-dir", type=Path, default=Path(DEFAULT_WORK_DIR),
        dest="work_dir",
        help="Hardhat workspace directory (reused from replay_transactions.py). "
             "The harness is compiled here so echidna can use the existing "
             "node_modules without a fresh npm install.",
    )
    p.add_argument(
        "--addresses", nargs="*", metavar="ADDR",
        help="Process only these contract addresses. Default: all.",
    )
    p.add_argument(
        "--from-results", type=Path, dest="from_results", default=None,
        metavar="JSON",
        help="Filter to contracts present in a replay results file "
             "(replay_results.json or generated_replay_results.json). "
             "Replicates the same 200-contract evaluated set used in "
             "replay_generated.py. Implies only contracts with replayed>0.",
    )
    p.add_argument(
        "--replay-k", type=int, default=1000, dest="replay_k",
        help="K value used in replay_transactions.py (to find constructor args).",
    )
    p.add_argument(
        "--n-contracts", type=int, default=None, dest="n_contracts",
        metavar="N",
        help="Limit to the first N contracts (alphabetical by address). "
             "Useful for smoke tests (e.g. --n-contracts 10).",
    )
    p.add_argument(
        "--test-limit", type=int, default=DEFAULT_TEST_LIMIT, dest="test_limit",
        help="Number of echidna calls per property.",
    )
    p.add_argument(
        "--seq-len", type=int, default=DEFAULT_SEQ_LEN, dest="seq_len",
        help="Echidna sequence length.",
    )
    p.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        help="Seconds before echidna is killed per contract.",
    )
    p.add_argument(
        "--workers", type=int, default=4, dest="workers",
        help="Parallel echidna workers per contract (one per (sample,fn) pair). "
             "Default 4. Each worker needs its own Hardhat compile dir.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Re-run even if result.json already exists.",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print echidna output for each contract.",
    )
    p.add_argument(
        "--results-file", type=Path,
        default=Path(DEFAULT_DIFF_DIR) / "fuzzing_results.json",
        dest="results_file",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    for d, label in [(args.dataset, "--dataset"), (args.generated, "--generated"),
                     (args.work_dir, "--work-dir")]:
        if not d.exists():
            print(f"ERROR: {label} not found: {d}")
            sys.exit(1)

    args.diff_dir.mkdir(parents=True, exist_ok=True)

    # Discover contracts: only those that have at least one compiled sample
    # ── Resolve address filter ────────────────────────────────────────────
    requested: set[str] | None = None

    if args.from_results is not None:
        if not args.from_results.exists():
            print(f"ERROR: --from-results file not found: {args.from_results}")
            sys.exit(1)
        data = json.loads(args.from_results.read_text())
        # Support replay_results.json (key "results") and
        # generated_replay_results.json (key "contracts")
        if "results" in data:
            addrs = {r["address"].lower() for r in data["results"]
                     if r.get("state") == "DONE" and r.get("address")}
        else:
            addrs = {c["address"].lower() for c in data.get("contracts", [])
                     if c.get("replayed", 0) > 0 and c.get("address")}
        print(f"  Loaded {len(addrs)} addresses from {args.from_results.name}")
        requested = addrs

    if args.addresses:
        extra = {a.lower() for a in args.addresses}
        requested = (requested & extra) if requested else extra

    gen_dirs = sorted([
        d for d in args.generated.iterdir()
        if d.is_dir()
        and any(re.match(r"sample_\d+\.(sol|json)$", f.name) for f in d.iterdir())
        and (args.dataset / d.name).exists()
        and (requested is None or d.name.lower() in requested)
    ])

    if args.n_contracts is not None:
        gen_dirs = gen_dirs[:args.n_contracts]

    print(f"""
differential_fuzzing.py
══════════════════════════════════════════════════════════
  dataset       = {args.dataset}
  generated     = {args.generated}
  diff_dir      = {args.diff_dir}
  work_dir      = {args.work_dir}
  n_contracts   = {args.n_contracts or 'all'}  ({len(gen_dirs)} found)
  from_results  = {args.from_results or 'all contracts'}
  test_limit    = {args.test_limit}
  seq_len       = {args.seq_len}
  timeout       = {args.timeout}s
  workers       = {args.workers}
  force         = {args.force}
══════════════════════════════════════════════════════════
""")

    if not shutil.which(ECHIDNA_CMD):
        print(f"WARNING: {ECHIDNA_CMD} not found on PATH.")
        print("  Install via npm:")
        print("    npm install -g @crytic/echidna")
        print("    ECHIDNA_BIN=$(npm root -g)/@crytic/echidna/bin/echidna/2.0.2/Ubuntu-18.04/echidna-test")
        print(f"    cp $ECHIDNA_BIN /usr/local/bin/{ECHIDNA_CMD}")
        print("  Harnesses will still be generated but fuzzing will be skipped.\n")

    all_results: list[dict] = []
    start_time = time.time()

    try:
        for i, gen_contract_dir in enumerate(gen_dirs, 1):
            address = gen_contract_dir.name
            print(f"\n[{i}/{len(gen_dirs)}] {address}")
            try:
                result = fuzz_contract(
                    address      = address,
                    dataset_dir  = args.dataset,
                    gen_dir      = args.generated,
                    diff_dir     = args.diff_dir,
                    work_dir     = args.work_dir,
                    replay_k     = args.replay_k,
                    test_limit   = args.test_limit,
                    seq_len      = args.seq_len,
                    timeout      = args.timeout,
                    force        = args.force,
                    verbose      = args.verbose,
                    workers      = args.workers,
                )
                all_results.append(result)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  Unexpected error: {e}")
                all_results.append({
                    "address": address,
                    "state":   "FAILED",
                    "reason":  f"unexpected error: {e}",
                })
    except KeyboardInterrupt:
        print("\nInterrupted — saving partial results ...")

    elapsed = time.time() - start_time
    args.results_file.write_text(json.dumps({
        "timestamp":  datetime.utcnow().isoformat(),
        "config": {
            "test_limit":  args.test_limit,
            "seq_len":     args.seq_len,
            "timeout":     args.timeout,
            "n_contracts": args.n_contracts,
        },
        "duration_seconds": round(elapsed, 1),
        "results": all_results,
    }, indent=2))
    print(f"\nResults written to {args.results_file}")

    print_summary(all_results, elapsed)


if __name__ == "__main__":
    main()