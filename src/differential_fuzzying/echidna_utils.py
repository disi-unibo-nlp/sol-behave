"""
    filename: echidna_utils.py
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
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR / ".." / "utils"))

from source_utils import _find_functions, _main_source_text, _load_json, _load_flat_source, _load_flat_sample, _find_sample_path
from hardhat_utils import primary_compiler_version, prepare_and_compile, \
                            _state_changing_fns, _solidity_type, _fn_param_list, \
                            _fn_arg_list, _abi_encode_call

ECHIDNA_CMD = "echidna-bin"



# ---------------------------------------------------------------------------
# Harness generation  (diffusc-style: separate files + import aliases)
# ---------------------------------------------------------------------------

_HARNESS_TEMPLATE = """// SPDX-License-Identifier: UNLICENSED
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

    constructor() {{
        orig = new {contract_name}_V1({constructor_args});
        gen  = new {contract_name}_V2({constructor_args});
    }}

{properties}
    function _eq(bytes memory a, bytes memory b) internal pure returns (bool) {{
        return keccak256(a) == keccak256(b);
    }}
}}
"""

_PROPERTY_TEMPLATE = """
    function diff_{fn_name}({params}) public {{
        (bool ok1, bytes memory r1) =
            address(orig).call({encode_call});

        (bool ok2, bytes memory r2) =
            address(gen).call({encode_call});

        assert(ok1 == ok2);

        if (ok1) {{
            assert(_eq(r1, r2));
        }}
    }}
"""

# This is still giving problems, the error reported with 
# python3 differential_fuzzying.py     --n-contracts 1     --force     --verbose
# is the following:
"""
[1/1] 0x00000751e8310fe25912afd7b347c2612b400000
  Generating harness for CG_Contract (orig vs sample_10.sol) ...  ✓
  Running echidna (10 properties, limit=10000, timeout=120s) ...  
ECHIDNA CMD:
echidna-bin /workdir/src/differential_fuzzying/fuzzying_results/0x00000751e8310fe25912afd7b347c2612b400000/hardhat_compile --contract DiffHarness --config /workdir/src/differential_fuzzying/fuzzying_results/0x00000751e8310fe25912afd7b347c2612b400000/echidna.yaml --format json --corpus-dir /workdir/src/differential_fuzzying/fuzzying_results/0x00000751e8310fe25912afd7b347c2612b400000/corpus

✓  17.2s  (rc=1)
echidna-bin: Error running slither:
decoding slither output failed


Results written to /workdir/src/differential_fuzzying/fuzzying_results/fuzzing_results.json

════════════════════════════════════════════════════════════════
  DIFFERENTIAL FUZZING SUMMARY
════════════════════════════════════════════════════════════════
  Contracts processed : 1
  Completed           : 1
  Setup failed        : 0

  Total properties    : 0
  Passed              : 0
  Failed (divergence) : 0
  Total time          : 00:00:17
════════════════════════════════════════════════════════════════
"""
def generate_harness(
    contract_name:    str,
    sample_idx:       int,
    abi:              list,
    constructor_args: list,
    compiler_version: str,
) -> str:
    """
    Generate a differential harness that imports both contracts by relative path
    (diffusc-style), avoiding inline source embedding and name collisions.

    The harness is placed in a workspace directory alongside:
      orig.sol   — original contract source (unchanged)
      gen.sol    — generated sample source (unchanged)
      DiffHarness.sol — this file

    Returns the harness source as a string.
    """
    ver    = compiler_version.lstrip("v").split("+")[0] or "0.8.0"
    pragma = f">={ver}"

    fns = _state_changing_fns(abi)[:10]
    properties = []
    seen_names: set[str] = set()

    for fn in fns:
        name   = fn.get("name", "")
        inputs = fn.get("inputs", [])
        if not name or name in seen_names:
            continue
        # Skip types echidna can't fuzz: dynamic bytes, tuples, string arrays
        types = [inp["type"] for inp in inputs]
        if any(t.startswith("tuple") or
               (t == "bytes") or
               (t.startswith("bytes") and not re.match(r'bytes\d+$', t)) or
               t == "string"
               for t in types):
            continue
        seen_names.add(name)
        properties.append(_PROPERTY_TEMPLATE.format(
            fn_name     = name,
            params      = _fn_param_list(inputs),
            encode_call = _abi_encode_call(name, inputs),
        ))

    if not properties:
        properties = [
    """
        function diff_deployed() public view {
            assert(address(orig) != address(0));
            assert(address(gen) != address(0));
        }
    """
        ]

    return _HARNESS_TEMPLATE.format(
        contract_name    = contract_name,
        sample_idx       = sample_idx,
        orig_filename    = "orig.sol",
        gen_filename     = "gen.sol",
        compiler_version = compiler_version,
        pragma           = pragma,
        constructor_args = _constructor_args_literal(abi, constructor_args),
        properties       = "\n".join(properties),
    )

def _constructor_args_literal(abi: list, args: list) -> str:
    """
    Convert constructor args list to a Solidity constructor call argument string.
    e.g. [\"0xf39F...\", 1000] -> '0xf39F..., 1000'
    """
    if not args:
        return ""
    parts = []
    ctor = next((e for e in abi if e.get("type") == "constructor"), None)
    inputs = ctor.get("inputs", []) if ctor else []
    for i, arg in enumerate(args):
        #t = inputs[i]["type"] if i < len(inputs) else "uint256"
        t = inputs[i].get("type", "uint256")
        if t == "address":
            parts.append(str(arg))
        elif t.startswith("uint") or t.startswith("int"):
            parts.append(str(arg))
        elif t == "bool":
            parts.append("true" if arg else "false")
        elif t == "string":
            escaped = str(arg).replace('"', '\\"')
            parts.append(f'"{escaped}"')
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
testMode: "assertion"
testLimit: {test_limit}
seqLen: {seq_len}
contractAddr: "0x00a329c0648769a73afac7f9381e08fb43dbea72"
deployer: "0x00a329c0648769a73afac7f9381e08fb43dbea70"
sender: ["0x00a329c0648769a73afac7f9381e08fb43dbea70"]
coverage: true
stopOnFail: false
quiet: false
"""
# solcArgs: "--optimize" # this was a parameter, but it broke everything

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
    Write orig.sol, gen.sol, DiffHarness.sol into an isolated workspace,
    compile with prepare_and_compile, then run echidna with
    --hardhat-ignore-compile against the pre-built artifacts.

    Keeping the two contracts in SEPARATE files (diffusc-style) avoids:
      - pragma conflicts between original and generated sources
      - contract name collisions
      - import path issues from inlining

    Returns (returncode, json_stdout, stderr).
    """
    import shutil as _shutil

    # ── Isolated workspace ────────────────────────────────────────────────
    compile_dir = out_dir / "hardhat_compile"
    if compile_dir.exists():
        _shutil.rmtree(compile_dir)
    compile_dir.mkdir(parents=True)

    nm_src = work_dir / "node_modules"
    if not nm_src.exists():
        return 1, "", f"node_modules not found in work_dir: {work_dir}"
    (compile_dir / "node_modules").symlink_to(nm_src.resolve())
    (compile_dir / "package.json").write_text(
        '{"name":"diff-harness","version":"1.0.0","private":true}'
    )

    # ── Write source files into contracts/ ────────────────────────────────
    contracts_dir = compile_dir / "contracts"
    contracts_dir.mkdir()
    (contracts_dir / "orig.sol").write_text(orig_source)
    (contracts_dir / "gen.sol").write_text(gen_source)
    harness_path = contracts_dir / "DiffHarness.sol"
    harness_path.write_text(harness_src)

    # ── Compile with prepare_and_compile ─────────────────────────────────
    # prepare_and_compile normally writes contracts/ itself, but here we've
    # already written all three files. We call it on the harness path so it
    # generates hardhat.config.js with the right solc version; it will clean
    # and re-write contracts/, so we pass the harness content via a temp path
    # that matches what prepare_and_compile expects.
    # Simpler: write hardhat.config.js directly and run hardhat compile.
    from hardhat_utils import (
        primary_compiler_version, install_npm_deps, detect_remappings, hardhat_config
    )
    all_texts = [orig_source, gen_source, harness_src]
    install_npm_deps(compile_dir, all_texts,
                     compiler_version=meta.get("compiler_version", ""))
    remaps = detect_remappings(compile_dir, all_texts)
    cfg    = hardhat_config(meta, source_texts=all_texts,
                            remappings=remaps or None)
    (compile_dir / "hardhat.config.js").write_text(cfg)

    for stale in ("artifacts", "cache"):
        p = compile_dir / stale
        if p.exists():
            _shutil.rmtree(p)

    compile_proc = subprocess.run(
        ["npx", "hardhat", "compile", "--force"],
        cwd=compile_dir, capture_output=True, text=True, timeout=120,
    )
    if compile_proc.returncode != 0:
        err = (
            f"Hardhat compile failed:\n"
            f"STDOUT: {compile_proc.stdout[-400:]}\n"
            f"STDERR: {compile_proc.stderr[-400:]}"
        )
        return compile_proc.returncode, "", err

    # ── Run echidna against pre-built artifacts ───────────────────────────
    corpus_dir = out_dir / "corpus"
    corpus_dir.mkdir(exist_ok=True)
    artifacts_dir = compile_dir / "artifacts"

    crytic_args = (
        "--compile-force-framework hardhat "
        "--hardhat-ignore-compile "
        f"--hardhat-artifacts-directory {artifacts_dir}"
    )
    cmd = [
        ECHIDNA_CMD,
        #str(harness_path),
        str(compile_dir), 
        "--contract",    contract,
        "--config",      str(config_path),
        "--format",      "json",
        "--corpus-dir",  str(corpus_dir),
        #"--crytic-args", crytic_args, # this was a parameter, but it broke everything
    ]
    try:
        print("\nECHIDNA CMD:")
        print(" ".join(map(str, cmd)))
        print()

        proc = subprocess.run(
            cmd,
            capture_output = True,
            text           = True,
            timeout        = timeout,
            cwd            = compile_dir,
        )
        return proc.returncode, proc.stdout, proc.stderr
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


def _extract_property_names(harness_source: str) -> list[str]:
    return re.findall(
        r'\bfunction\s+(diff_\w+)\s*\(',
        harness_source
    )


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------
# The parser is not working properly, we should check the echidna outputs manually, where are they located?
def parse_echidna_output(json_stdout: str, stderr: str,
                         out_dir: Path) -> dict:
    """
    Parse echidna --format json output and corpus directory for:
      - pass/fail status per property
      - functional coverage: distinct functions called with distinct inputs
      - branch coverage: from corpus/coverage/*.txt files
      - counterexample call sequences for failed properties

    JSON output schema (echidna 2.0.2):
    [{"name": "echidna_x", "status": "passed"|"failed"|"error",
      "callSequence": [{"contract": "C", "function": "f(uint256)",
                        "arguments": ["123"], "value": "0x0", ...}]}]
    """
    result: dict = {
        "properties":             {},
        "passed":                 0,
        "failed":                 0,
        "timed_out":              "TIMEOUT" in stderr,
        "echidna_missing":        ECHIDNA_CMD + " not found" in stderr,
        "compile_failed":         "compile failed" in stderr.lower(),
        "total":                  0,
        # Functional coverage
        "fns_called":             [],   # distinct fn signatures called by echidna
        "n_fns_called":           0,
        "fn_call_counts":         {},   # {fn_sig: n_calls}
        "fn_unique_inputs":       {},   # {fn_sig: n_distinct_input_tuples}
        # Branch coverage from corpus/coverage/*.txt
        "branch_coverage_lines":  None,
    }

    # ── Parse JSON output ─────────────────────────────────────────────────
    tests = []
    raw = json_stdout.strip()
    if raw:
        # echidna 2.0.2 outputs one JSON object per line or a JSON array
        try:
            data = json.loads(raw)
            tests = data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            # Try line-by-line (some versions emit one object per line)
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        tests.append(json.loads(line))
                    except Exception:
                        pass

    # Aggregate pass/fail and collect call sequences
    all_calls: list[dict] = []
    for t in tests:
        name   = t.get("name", "unknown")
        status = t.get("status", "unknown")
        seq    = t.get("callSequence") or t.get("call_sequence") or []

        entry: dict = {"status": status}
        if status == "passed":
            result["passed"] += 1
        elif status == "failed":
            result["failed"] += 1
            if seq:
                entry["counterexample"] = seq

        result["properties"][name] = entry
        all_calls.extend(seq)

    result["total"] = len(tests)

    # ── Functional coverage from call sequences ───────────────────────────
    fn_inputs: dict[str, set] = {}   # fn_sig -> set of frozenset(args)
    fn_counts: dict[str, int] = {}

    for call in all_calls:
        fn_sig = call.get("function") or call.get("name") or ""
        if not fn_sig:
            continue
        args = tuple(str(a) for a in (call.get("arguments") or []))
        fn_counts[fn_sig]  = fn_counts.get(fn_sig, 0) + 1
        fn_inputs.setdefault(fn_sig, set()).add(args)

    result["fns_called"]       = sorted(fn_counts.keys())
    result["n_fns_called"]     = len(fn_counts)
    result["fn_call_counts"]   = fn_counts
    result["fn_unique_inputs"] = {fn: len(inp) for fn, inp in fn_inputs.items()}

    # ── Branch coverage from corpus/coverage/*.txt ────────────────────────
    coverage_dir = out_dir / "corpus" / "coverage"
    if coverage_dir.exists():
        covered_lines: set[str] = set()
        for cov_file in coverage_dir.glob("covered.*.txt"):
            try:
                for line in cov_file.read_text().splitlines():
                    line = line.strip()
                    if line:
                        covered_lines.add(line)
            except Exception:
                pass
        result["branch_coverage_lines"] = len(covered_lines)

    return result
