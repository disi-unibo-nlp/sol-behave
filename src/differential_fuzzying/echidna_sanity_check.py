"""
echidna_sanity_check.py
=======================

Sanity check: run echidna on the original source contract directly,
bypassing the differential harness entirely.

This helps isolate whether the "decoding slither output failed" error
is caused by:
  (a) the harness/project setup, OR
  (b) echidna's interaction with the contract itself

The script tries three modes in sequence:
  1. Minimal assertion harness (wraps original, adds a trivial echidna_ property)
  2. Raw echidna on original source directly (no harness)
  3. Slither standalone check to see what slither reports

Usage
-----
  python echidna_sanity_check.py --address 0x00000751e8310fe25912afd7b347c2612b400000

  python echidna_sanity_check.py \\
      --address 0x00000751e8310fe25912afd7b347c2612b400000 \\
      --dataset ../dataset/contracts_dataset_1to20 \\
      --work-dir ../dataset/hardhat_workspace_replay \\
      --out-dir  ./sanity_check_results

  # Skip straight to slither-only check:
  python echidna_sanity_check.py --address 0x... --slither-only
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR / ".." / "utils"))

try:
    from source_utils import _load_flat_source, _load_json
    from hardhat_utils import (
        primary_compiler_version, install_npm_deps,
        detect_remappings, hardhat_config,
    )
    from echidna_utils import ECHIDNA_CMD
except ImportError as e:
    print(f"[WARN] Could not import project utils ({e}). Using fallback defaults.")
    ECHIDNA_CMD = "echidna-bin"

    def _load_json(p):
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}

    def _load_flat_source(contract_dir):
        jsn = contract_dir / "source.json"
        if jsn.exists():
            data = json.loads(jsn.read_text())
            return "\n\n".join(
                fc.get("content", "") for fc in data.get("sources", {}).values()
            )
        sol = contract_dir / "source.sol"
        return sol.read_text() if sol.exists() else ""

    def primary_compiler_version(meta, _):
        return meta.get("compiler_version", "0.8.20").lstrip("v").split("+")[0]

    def install_npm_deps(work_dir, texts, compiler_version=""):
        pass

    def detect_remappings(work_dir, texts):
        return []

    def hardhat_config(meta, source_texts=None, remappings=None):
        ver = primary_compiler_version(meta, source_texts or [])
        return (
            'require("@nomicfoundation/hardhat-toolbox");\n\n'
            'module.exports = {\n'
            '  solidity: {\n'
            f'    compilers: [{{ version: "{ver}", settings: {{'
            '      optimizer: { enabled: false }\n'
            '    }}] }\n'
            '};\n'
        )


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_DATASET  = str(_SCRIPT_DIR / ".." / "dataset" / "contracts_dataset_1to20")
DEFAULT_WORK_DIR = str(_SCRIPT_DIR / ".." / "dataset" / "hardhat_workspace_replay")
DEFAULT_OUT_DIR  = str(_SCRIPT_DIR / "sanity_check_results")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd, cwd, timeout=60, label=""):
    """Run a subprocess and return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"TIMEOUT after {timeout}s"
    except FileNotFoundError as exc:
        return -2, "", f"Binary not found: {exc}"


def _section(title):
    bar = "─" * 60
    print(f"\n{bar}")
    print(f"  {title}")
    print(bar)


def _write_workspace(compile_dir, sources_dict, meta, work_dir, extra_texts=None):
    """
    Set up an isolated Hardhat workspace in compile_dir.
    sources_dict: {filename: content}
    Returns (ok, error_message).
    """
    compile_dir.mkdir(parents=True, exist_ok=True)

    # Symlink node_modules from work_dir to avoid re-installing
    nm_src = Path(work_dir) / "node_modules"
    nm_dst = compile_dir / "node_modules"
    if nm_src.exists() and not nm_dst.exists():
        nm_dst.symlink_to(nm_src.resolve())

    (compile_dir / "package.json").write_text(
        '{"name":"sanity-check","version":"1.0.0","private":true}'
    )

    contracts_dir = compile_dir / "contracts"
    if contracts_dir.exists():
        shutil.rmtree(contracts_dir)
    contracts_dir.mkdir()

    for fname, content in sources_dict.items():
        dest = contracts_dir / fname
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)

    all_texts = list(sources_dict.values()) + (extra_texts or [])

    try:
        install_npm_deps(
            compile_dir, all_texts,
            compiler_version=meta.get("compiler_version", ""),
        )
    except Exception as e:
        print(f"  [WARN] install_npm_deps failed: {e}")

    try:
        remaps = detect_remappings(compile_dir, all_texts)
    except Exception:
        remaps = []

    try:
        cfg = hardhat_config(meta, source_texts=all_texts, remappings=remaps or None)
    except Exception:
        ver = primary_compiler_version(meta, all_texts)
        cfg = (
            'require("@nomicfoundation/hardhat-toolbox");\n\n'
            'module.exports = {\n'
            f'  solidity: "{ver}"\n'
            '};\n'
        )

    (compile_dir / "hardhat.config.js").write_text(cfg)
    for stale in ("artifacts", "cache"):
        p = compile_dir / stale
        if p.exists():
            shutil.rmtree(p)

    rc, stdout, stderr = _run(
        ["npx", "hardhat", "compile", "--force"],
        cwd=compile_dir, timeout=120, label="hardhat compile"
    )
    if rc != 0:
        return False, f"Hardhat compile failed (rc={rc}):\n{stderr[-600:]}"
    return True, ""


def _echidna_config_simple(contract, test_limit, solc_ver):
    """Minimal echidna config — no slither-related options."""
    return f"""\
testMode: "assertion"
testLimit: {test_limit}
seqLen: 10
contractAddr: "0x00a329c0648769a73afac7f9381e08fb43dbea72"
deployer:     "0x00a329c0648769a73afac7f9381e08fb43dbea70"
sender:       ["0x00a329c0648769a73afac7f9381e08fb43dbea70"]
coverage: true
stopOnFail: false
quiet: false
# Explicitly disable slither to avoid "decoding slither output failed"
cryticArgs: ["--compile-force-framework", "hardhat", "--hardhat-ignore-compile"]
"""


# ---------------------------------------------------------------------------
# Mode 1: Minimal assertion harness wrapping the original contract
# ---------------------------------------------------------------------------

def _build_minimal_harness(contract_name, source, compiler_ver):
    """
    Build the simplest possible echidna harness:
      - imports the original contract
      - deploys it with no constructor args
      - adds one trivial echidna_alive() property

    This is intentionally simpler than the differential harness to
    maximise the chance of echidna accepting it.
    """
    ver    = compiler_ver.lstrip("v").split("+")[0] or "0.8.0"
    pragma = f">={ver}"

    # Detect if constructor has required args — if so, skip deployment
    has_ctor_args = bool(re.search(
        r'constructor\s*\([^)]+\)', source
    ))

    if has_ctor_args:
        deploy_line = f"// {contract_name} has constructor args — not deployed"
        check_line  = "assert(true);"
    else:
        deploy_line = f"{contract_name} internal _target = new {contract_name}();"
        check_line  = f"assert(address(_target) != address(0));"

    return f"""// SPDX-License-Identifier: UNLICENSED
// Minimal sanity-check harness — auto-generated by echidna_sanity_check.py
pragma solidity {pragma};

import {{{contract_name}}} from "./orig.sol";

contract SanityHarness {{

    {deploy_line}

    /// @notice trivial property: always passes if echidna can reach it
    function echidna_alive() public pure returns (bool) {{
        return true;
    }}

    function echidna_noop() public {{
        {check_line}
    }}
}}
"""


def run_mode1_harness(address, contract_name, source, meta, work_dir, out_dir,
                      compiler_ver, test_limit):
    _section("MODE 1: Minimal assertion harness (wraps original contract)")

    compile_dir = out_dir / "mode1_harness"

    harness = _build_minimal_harness(contract_name, source, compiler_ver)
    print(f"  Harness preview (first 20 lines):")
    for line in harness.splitlines()[:20]:
        print(f"    {line}")
    print()

    print("  Setting up Hardhat workspace ...")
    ok, err = _write_workspace(
        compile_dir,
        sources_dict={"orig.sol": source, "SanityHarness.sol": harness},
        meta=meta,
        work_dir=work_dir,
    )
    if not ok:
        print(f"  ✗ Workspace setup failed:\n{err}")
        return {"mode": "harness", "status": "compile_failed", "error": err}

    print("  ✓ Hardhat compile succeeded")

    config_text = _echidna_config_simple("SanityHarness", test_limit, compiler_ver)
    config_path = compile_dir / "echidna.yaml"
    config_path.write_text(config_text)

    corpus_dir = out_dir / "mode1_corpus"
    corpus_dir.mkdir(exist_ok=True)

    cmd = [
        ECHIDNA_CMD,
        str(compile_dir),
        "--contract", "SanityHarness",
        "--config",   str(config_path),
        "--format",   "text",
        "--corpus-dir", str(corpus_dir),
    ]
    print(f"\n  CMD: {' '.join(map(str, cmd))}\n")

    rc, stdout, stderr = _run(cmd, cwd=compile_dir, timeout=60, label="echidna mode1")

    print(f"  Return code: {rc}")
    print(f"\n  --- STDOUT ---")
    print(stdout[:2000] or "  (empty)")
    if stderr:
        print(f"\n  --- STDERR ---")
        print(stderr[:2000])

    # Save raw output
    (out_dir / "mode1_stdout.txt").write_text(stdout)
    (out_dir / "mode1_stderr.txt").write_text(stderr)

    slither_error = "slither" in stderr.lower() and "failed" in stderr.lower()
    if slither_error:
        print("\n  ⚠  Slither error still present — see Mode 3 for diagnosis.")
    elif rc == 0 or "passed" in stdout.lower():
        print("\n  ✓ echidna ran successfully!")
    else:
        print(f"\n  ✗ echidna exited rc={rc}")

    return {
        "mode": "harness", "rc": rc,
        "slither_error": slither_error,
        "stdout_snippet": stdout[:500],
        "stderr_snippet": stderr[:500],
    }


# ---------------------------------------------------------------------------
# Mode 2: echidna directly on original .sol (no harness, no hardhat)
# ---------------------------------------------------------------------------

def run_mode2_raw(address, contract_name, source, meta, out_dir, compiler_ver,
                  test_limit):
    _section("MODE 2: Raw echidna on original .sol (no Hardhat, no harness)")

    raw_dir = out_dir / "mode2_raw"
    raw_dir.mkdir(exist_ok=True)

    sol_path = raw_dir / "orig.sol"
    sol_path.write_text(source)

    # Minimal yaml — point solc at the right version, no hardhat
    ver = compiler_ver.lstrip("v").split("+")[0] or "0.8.0"
    config_text = f"""\
testMode: "assertion"
testLimit: {test_limit}
seqLen: 5
solcArgs: "--optimize"
quiet: false
"""
    config_path = raw_dir / "echidna.yaml"
    config_path.write_text(config_text)

    cmd = [
        ECHIDNA_CMD,
        str(sol_path),
        "--contract", contract_name,
        "--config",   str(config_path),
        "--format",   "text",
    ]
    print(f"  CMD: {' '.join(map(str, cmd))}\n")

    rc, stdout, stderr = _run(cmd, cwd=raw_dir, timeout=60, label="echidna mode2")

    print(f"  Return code: {rc}")
    print(f"\n  --- STDOUT ---")
    print(stdout[:2000] or "  (empty)")
    if stderr:
        print(f"\n  --- STDERR ---")
        print(stderr[:2000])

    (out_dir / "mode2_stdout.txt").write_text(stdout)
    (out_dir / "mode2_stderr.txt").write_text(stderr)

    slither_error = "slither" in stderr.lower() and "failed" in stderr.lower()
    return {
        "mode": "raw", "rc": rc,
        "slither_error": slither_error,
        "stdout_snippet": stdout[:500],
        "stderr_snippet": stderr[:500],
    }


# ---------------------------------------------------------------------------
# Mode 3: Slither standalone check
# ---------------------------------------------------------------------------

def run_mode3_slither(address, contract_name, source, out_dir, compiler_ver):
    _section("MODE 3: Slither standalone check")

    slither_bin = shutil.which("slither")
    if not slither_bin:
        print("  slither not found on PATH — skipping")
        print("  Install with: pip install slither-analyzer")
        return {"mode": "slither", "status": "not_installed"}

    raw_dir = out_dir / "mode3_slither"
    raw_dir.mkdir(exist_ok=True)
    sol_path = raw_dir / "orig.sol"
    sol_path.write_text(source)

    cmd = [
        "slither", str(sol_path),
        "--print", "contract-summary",
        "--solc-solcs-select", compiler_ver,
    ]
    print(f"  CMD: {' '.join(map(str, cmd))}\n")

    rc, stdout, stderr = _run(cmd, cwd=raw_dir, timeout=60, label="slither")

    print(f"  Return code: {rc}")
    print(f"\n  --- STDOUT ---")
    print(stdout[:2000] or "  (empty)")
    if stderr:
        print(f"\n  --- STDERR ---")
        print(stderr[:2000])

    (out_dir / "mode3_slither_stdout.txt").write_text(stdout)
    (out_dir / "mode3_slither_stderr.txt").write_text(stderr)

    # Common root causes
    if "No contract was analyzed" in stdout or "No contract was analyzed" in stderr:
        print("\n  ⚠  Slither found no contracts — likely a pragma/import issue.")
    elif rc == 0:
        print("\n  ✓ Slither ran cleanly. The issue is in how echidna invokes it.")
    else:
        print(f"\n  ✗ Slither itself fails (rc={rc}). Fix slither first.")

    return {
        "mode": "slither", "rc": rc,
        "stdout_snippet": stdout[:500],
        "stderr_snippet": stderr[:500],
    }


# ---------------------------------------------------------------------------
# Mode 4: echidna with --no-slither flag (echidna 2.1+)
# ---------------------------------------------------------------------------

def run_mode4_no_slither(address, contract_name, source, meta, work_dir,
                         out_dir, compiler_ver, test_limit):
    _section("MODE 4: echidna with slither explicitly disabled (cryticArgs)")

    compile_dir = out_dir / "mode4_noslither"

    # Re-use mode1 harness
    harness = _build_minimal_harness(contract_name, source, compiler_ver)

    print("  Setting up Hardhat workspace ...")
    ok, err = _write_workspace(
        compile_dir,
        sources_dict={"orig.sol": source, "SanityHarness.sol": harness},
        meta=meta,
        work_dir=work_dir,
    )
    if not ok:
        print(f"  ✗ Workspace setup failed:\n{err}")
        return {"mode": "no_slither", "status": "compile_failed", "error": err}
    print("  ✓ Hardhat compile succeeded")

    # Config with slither disabled via cryticArgs
    ver = compiler_ver.lstrip("v").split("+")[0] or "0.8.0"
    config_text = f"""\
testMode: "assertion"
testLimit: {test_limit}
seqLen: 10
contractAddr: "0x00a329c0648769a73afac7f9381e08fb43dbea72"
deployer:     "0x00a329c0648769a73afac7f9381e08fb43dbea70"
sender:       ["0x00a329c0648769a73afac7f9381e08fb43dbea70"]
coverage: false
stopOnFail: false
quiet: false
# Tell crytic-compile to use hardhat and skip re-compilation (echidna will use artifacts)
cryticArgs: ["--compile-force-framework", "hardhat", "--hardhat-ignore-compile"]
"""
    config_path = compile_dir / "echidna.yaml"
    config_path.write_text(config_text)

    corpus_dir = out_dir / "mode4_corpus"
    corpus_dir.mkdir(exist_ok=True)

    cmd = [
        ECHIDNA_CMD,
        str(compile_dir),
        "--contract", "SanityHarness",
        "--config",   str(config_path),
        "--format",   "text",
        "--corpus-dir", str(corpus_dir),
    ]
    print(f"\n  CMD: {' '.join(map(str, cmd))}\n")

    rc, stdout, stderr = _run(cmd, cwd=compile_dir, timeout=60, label="echidna mode4")

    print(f"  Return code: {rc}")
    print(f"\n  --- STDOUT ---")
    print(stdout[:2000] or "  (empty)")
    if stderr:
        print(f"\n  --- STDERR ---")
        print(stderr[:2000])

    (out_dir / "mode4_stdout.txt").write_text(stdout)
    (out_dir / "mode4_stderr.txt").write_text(stderr)

    slither_error = "slither" in stderr.lower() and "failed" in stderr.lower()
    if not slither_error and (rc == 0 or "passed" in stdout.lower()):
        print("\n  ✓ SUCCESS! cryticArgs suppressed the slither error.")
        print("  → Add these cryticArgs to your main echidna.yaml config.")
    elif slither_error:
        print("\n  ✗ Slither error persists even with cryticArgs override.")

    return {
        "mode": "no_slither", "rc": rc,
        "slither_error": slither_error,
        "stdout_snippet": stdout[:500],
        "stderr_snippet": stderr[:500],
    }


# ---------------------------------------------------------------------------
# Diagnosis summary
# ---------------------------------------------------------------------------

def print_diagnosis(results):
    _section("DIAGNOSIS SUMMARY")

    modes = {r["mode"]: r for r in results}

    # Was echidna found at all?
    any_not_found = any(r.get("rc") == -2 for r in results)
    if any_not_found:
        print("  ✗ CRITICAL: echidna binary not found on PATH.")
        print(f"  Expected binary name: {ECHIDNA_CMD}")
        print("  Fix: ensure echidna-bin (or echidna) is on PATH.")
        return

    slither_everywhere = all(
        r.get("slither_error", False) for r in results
        if r.get("rc") not in (None, -2)
    )
    slither_mode4_fixed = not modes.get("no_slither", {}).get("slither_error", True)

    print()
    if slither_mode4_fixed:
        print("  ✓ ROOT CAUSE: slither is being invoked by crytic-compile and failing.")
        print("  ✓ FIX: add these lines to your echidna.yaml:")
        print()
        print('    cryticArgs: ["--compile-force-framework", "hardhat",')
        print('                 "--hardhat-ignore-compile"]')
        print()
        print("  This tells crytic-compile to use existing Hardhat artifacts,")
        print("  bypassing slither entirely.")
    elif not modes.get("raw", {}).get("slither_error", True):
        print("  ✓ Mode 2 (raw .sol) worked without slither errors.")
        print("  → The issue is specific to the Hardhat project setup.")
        print("  → Check that hardhat.config.js compilers match your source pragmas.")
    elif modes.get("slither", {}).get("rc") not in (0, None):
        print("  ✗ Slither itself is broken in your environment.")
        print("  Fix options:")
        print("    1. pip install slither-analyzer --upgrade")
        print("    2. pip install solc-select && solc-select install <version>")
        print("    3. Set SLITHER_SOLC=/path/to/solc in your environment")
    else:
        print("  ⚠ Could not determine root cause automatically.")
        print("  Check the per-mode output files in the out-dir for details.")

    print()
    print("  Per-mode results:")
    for r in results:
        mode = r.get("mode", "?")
        rc   = r.get("rc", "n/a")
        se   = "slither_err=YES" if r.get("slither_error") else "slither_err=no"
        st   = r.get("status", "")
        print(f"    {mode:<15}  rc={rc:<4}  {se}  {st}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Sanity-check echidna on an original source contract.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--address", default="0x0baeccd651cf4692a8790bcc4f606e79bf7a3b1c ",
                   help="Contract address (directory name in dataset).")
    p.add_argument("--dataset", type=Path, default=Path(DEFAULT_DATASET))
    p.add_argument("--work-dir", type=Path, default=Path(DEFAULT_WORK_DIR),
                   dest="work_dir")
    p.add_argument("--out-dir", type=Path, default=None, dest="out_dir",
                   help="Output directory (default: sanity_check_results/<address>).")
    p.add_argument("--test-limit", type=int, default=500, dest="test_limit",
                   help="Echidna calls per property (keep small for sanity check).")
    p.add_argument("--slither-only", action="store_true", dest="slither_only",
                   help="Only run the slither check (Mode 3).")
    p.add_argument("--mode", type=int, choices=[1, 2, 3, 4], default=None,
                   help="Run only one specific mode (1-4).")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    contract_dir = args.dataset / args.address
    if not contract_dir.exists():
        print(f"ERROR: contract not found: {contract_dir}")
        sys.exit(1)

    out_dir = args.out_dir or (
        Path(DEFAULT_OUT_DIR) / args.address
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load contract data
    meta = _load_json(contract_dir / "metadata.json")
    if not meta:
        print(f"ERROR: no metadata.json in {contract_dir}")
        sys.exit(1)

    contract_name = meta.get("contract_name", "Contract")
    compiler_ver  = primary_compiler_version(meta, [])
    source        = _load_flat_source(contract_dir)

    if not source:
        print(f"ERROR: no source found in {contract_dir}")
        sys.exit(1)

    print(f"""
echidna_sanity_check.py
{'═' * 60}
  address       = {args.address}
  contract_name = {contract_name}
  compiler_ver  = {compiler_ver}
  source_len    = {len(source)} chars
  work_dir      = {args.work_dir}
  out_dir       = {out_dir}
  test_limit    = {args.test_limit}
  echidna_cmd   = {ECHIDNA_CMD}  (found={bool(shutil.which(ECHIDNA_CMD))})
  slither       = {shutil.which('slither') or 'NOT FOUND'}
{'═' * 60}
""")

    results = []

    if args.slither_only or args.mode == 3:
        results.append(run_mode3_slither(
            args.address, contract_name, source, out_dir, compiler_ver
        ))
    elif args.mode == 1:
        results.append(run_mode1_harness(
            args.address, contract_name, source, meta,
            args.work_dir, out_dir, compiler_ver, args.test_limit
        ))
    elif args.mode == 2:
        results.append(run_mode2_raw(
            args.address, contract_name, source, meta,
            out_dir, compiler_ver, args.test_limit
        ))
    elif args.mode == 4:
        results.append(run_mode4_no_slither(
            args.address, contract_name, source, meta,
            args.work_dir, out_dir, compiler_ver, args.test_limit
        ))
    else:
        # Run all modes in sequence, stopping early if one succeeds
        results.append(run_mode1_harness(
            args.address, contract_name, source, meta,
            args.work_dir, out_dir, compiler_ver, args.test_limit
        ))
        results.append(run_mode2_raw(
            args.address, contract_name, source, meta,
            out_dir, compiler_ver, args.test_limit
        ))
        results.append(run_mode3_slither(
            args.address, contract_name, source, out_dir, compiler_ver
        ))
        results.append(run_mode4_no_slither(
            args.address, contract_name, source, meta,
            args.work_dir, out_dir, compiler_ver, args.test_limit
        ))

    print_diagnosis(results)

    report_path = out_dir / "sanity_check_report.json"
    report_path.write_text(json.dumps({
        "address":       args.address,
        "contract_name": contract_name,
        "compiler_ver":  compiler_ver,
        "results":       results,
    }, indent=2))
    print(f"\n  Full report: {report_path}")


if __name__ == "__main__":
    main()