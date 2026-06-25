"""
hardhat_server.py — shared Hardhat server module

Provides a unified HardhatServer class used by all evaluation scripts:
  - make_sequences_hardhat.py  (DCR sequence generation)
  - gt_runner_synthetic_txs.py (ground-truth replay)
  - gt_runner_stateful.py      (ground-truth stateful fuzzing)
  - differential_fuzzer_stateful.py (differential testing)

Key design decisions
--------------------
1. ALWAYS fresh deploy — inject mode is never used.
   Inject caused owner mismatch between generation and replay because the
   saved tx.from (OWNER_ADDR Hardhat) differed from the effective signer
   (MAINNET_OWNER) used during inject execution.

2. Constructor parameter synthesis via LLM.
   When a contract's constructor requires arguments, the LLM inspects the
   ABI constructor entry and the contract source to propose plausible values
   (e.g. zero address for external contract references that are not needed
   for the functions under test). The result is saved to
   DATASET_DIR/<address>/deploy_params.json so it is reused across runs and
   can be manually corrected if needed.

3. Corpus recording.
   Every successful transaction — including the deploy — is appended to a
   per-contract call log (deploy_params.json under "successful_calls"). This
   gives a complete record of which call sequences reached which branches,
   useful for branch coverage analysis.

4. Single source of truth for the Node.js server.
   SERVER_JS is defined once here. All scripts import and use it, so any
   fix to the JS (e.g. getSigner, readState, resetStorage) propagates
   automatically.

Usage
-----
    from hardhat_server import HardhatServer, setup_workspace, pick_fork_block

    setup_workspace(work_dir)
    server = HardhatServer(work_dir)
    addr   = server.deploy(fqn, abi, source_text, address, dataset_dir, llm_client)
    result = server.send({"tx": tx})
    server.close()
"""

import json
import os
import re
import select
import subprocess
from pathlib import Path
from typing import Any

# ── Hardhat accounts (deterministic) ─────────────────────────────────────────

# ── Hardhat accounts (deterministic) ─────────────────────────────────────────
# All 20 Hardhat default accounts, derived from the standard test mnemonic:
# "test test test test test test test test test test test junk"
# These are stable across all Hardhat versions and projects.

_HARDHAT_ACCOUNTS = [
    "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",  # 0  — default deployer / owner
    "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",  # 1
    "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",  # 2
    "0x90F79bf6EB2c4f870365E785982E1f101E93b906",  # 3
    "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65",  # 4
    "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc",  # 5
    "0x976EA74026E726554dB657fA54763abd0C3a0aa9",  # 6
    "0x14dC79964da2C08b23698B3D3cc7Ca32193d9955",  # 7
    "0x23618e81E3f5cdF7f54C3d65f7FBc0aBf5B21E8f",  # 8
    "0xa0Ee7A142d267C1f36714E4a8F75612F20a79720",  # 9
    "0xBcd4042DE499D14e55001CcbB24a551F3b954096",  # 10
    "0x71bE63f3384f5fb98995898A86B02Fb2426c5788",  # 11
    "0xFABB0ac9d68B0B445fB7357272Ff202C5651694a",  # 12
    "0x1CBd3b2770909D4e10f157cABC84C7264073C9Ec",  # 13
    "0xdF3e18d64BC6A983f673Ab319CCaE4f1a57C7097",  # 14
    "0xcd3B766CCDd6AE721141F452C550Ca635964ce71",  # 15
    "0x2546BcD3c84621e976D8185a91A922aE77ECEc30",  # 16
    "0xbDA5747bFD65F08deb54cb465eB87D40e51B197E",  # 17
    "0xdD2FD4581271e230360230F9337D5c0430Bf44C0",  # 18
    "0x8626f6940E2eb28930eFb4CeF49B2d1F2C9C1199",  # 19
]

def hardhat_account(index: int) -> str:
    """
    Return the Hardhat account address at the given index (0-19).
    Index 0 is always the deployer/owner (OWNER_ADDR).
    Raises IndexError if index is out of range.
    """
    return _HARDHAT_ACCOUNTS[index]


def hardhat_accounts(start: int = 0, end: int = 20) -> list[str]:
    """Return a slice of Hardhat accounts [start, end)."""
    return _HARDHAT_ACCOUNTS[start:end]


# Convenience aliases for the most common accounts
OWNER_ADDR = _HARDHAT_ACCOUNTS[0]
USER_ADDR  = _HARDHAT_ACCOUNTS[1]
USER2_ADDR = _HARDHAT_ACCOUNTS[2]

HARDHAT_ACCOUNTS = {a.lower() for a in _HARDHAT_ACCOUNTS}

# ── Node.js server (single source of truth) ───────────────────────────────────

SERVER_JS = r"""
const { ethers, artifacts, network } = require("hardhat");
const readline = require("readline");

const MAX_SLOTS = parseInt(process.env.MAX_SLOTS_PER_TX || "50");
const ZERO      = "0x" + "0".repeat(64);

let CONTRACT_ADDRESS = null;
let DEPLOY_STORAGE   = {};

const HARDHAT_ACCOUNTS = new Set([
  "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
  "0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
  "0x3c44cdddb6a900fa2b585dd299e03d12fa4293bc",
  "0x90f79bf6eb2c4f870365e785982e1f101e93b906",
  "0x15d34aaf54267db7d7c367839aaf71a00a2c6a65",
]);

// ── Storage helpers ───────────────────────────────────────────────────────────

async function fullStorageSnapshot(addr) {
  const snap = {};
  for (let i = 0; i < MAX_SLOTS; i++) {
    const slot  = "0x" + i.toString(16).padStart(64, "0");
    const value = await network.provider.send("eth_getStorageAt", [addr, slot, "latest"]);
    if (value !== ZERO) snap[slot] = value;
  }
  return snap;
}

async function resetStorage(addr, deploySnap) {
  const current = await fullStorageSnapshot(addr);
  for (const slot of Object.keys(current)) {
    if (!(slot in deploySnap))
      await network.provider.send("hardhat_setStorageAt", [addr, slot, ZERO]);
  }
  for (const [slot, val] of Object.entries(deploySnap))
    await network.provider.send("hardhat_setStorageAt", [addr, slot, val]);
}

// ── Signer resolution ─────────────────────────────────────────────────────────
// Never remaps to a mainnet owner — tx.from in the saved corpus always matches
// the signer used during execution, guaranteeing replay consistency.

async function getSigner(from) {
  const fromLow = from.toLowerCase();
  if (HARDHAT_ACCOUNTS.has(fromLow)) {
    // Native Hardhat signer: tx.origin == msg.sender, satisfies callerIsUser().
    const signers = await ethers.getSigners();
    const found   = signers.find(s => s.address.toLowerCase() === fromLow);
    if (found) return { signer: found, impersonated: false };
  }
  await network.provider.request({ method: "hardhat_impersonateAccount", params: [from] });
  await network.provider.send("hardhat_setBalance", [from, "0x56BC75E2D63100000"]);
  return { signer: await ethers.getSigner(from), impersonated: true };
}

// ── State reader ──────────────────────────────────────────────────────────────
// Generic: reads only variables listed in stateVars (extracted from DCR guards).
// ethers.js exposes both view functions and public state variables as getters.

async function readState(signerAddr, contractInstance, stateVars) {
  const state = {};
  try {
    const cb = await network.provider.send("eth_getBalance", [CONTRACT_ADDRESS, "latest"]);
    state.contractBalance = BigInt(cb).toString();
  } catch (_) {}
  try {
    const sb = await network.provider.send("eth_getBalance", [signerAddr, "latest"]);
    state.signerBalance = BigInt(sb).toString();
  } catch (_) {}

  if (!contractInstance || !stateVars) return state;
  for (const [key, spec] of Object.entries(stateVars)) {
    try {
      const args = (spec.args || []).map(a => a === "signer" ? signerAddr : a);
      const r    = await contractInstance[spec.name](...args);
      state[key] = r.toString();
    } catch (_) {}
  }
  return state;
}

// ── Transaction executor ──────────────────────────────────────────────────────

async function executeTx(tx) {
  const result = {
    status: null, return_value: null, logs: [],
    storage_after: {}, revert_reason: null, error: null,
  };
  const from = tx.from || "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266";
  const val  = BigInt(tx.value || 0);
  try {
    const { signer, impersonated } = await getSigner(from);
    try {
      result.return_value = await network.provider.send("eth_call", [{
        to: CONTRACT_ADDRESS, data: tx.input, from,
        value: "0x" + val.toString(16),
      }, "latest"]);
    } catch (_) {}
    try {
      const sent    = await signer.sendTransaction({
        to: CONTRACT_ADDRESS, data: tx.input, value: val,
        gasLimit: BigInt(tx.gas || 500000) * 2n,
      });
      const receipt = await sent.wait();
      result.status = receipt.status === 1 ? "success" : "revert";
      result.logs   = receipt.logs.map(l => ({
        address: l.address.toLowerCase(), topics: l.topics, data: l.data,
      }));
    } catch (err) {
      result.status        = "revert";
      result.revert_reason = err.message || String(err);
    }
    if (impersonated)
      await network.provider.request({ method: "hardhat_stopImpersonatingAccount", params: [from] });
  } catch (err) {
    result.status = "error";
    result.error  = err.message || String(err);
  }
  result.storage_after = await fullStorageSnapshot(CONTRACT_ADDRESS);
  return result;
}

// ── Main loop ─────────────────────────────────────────────────────────────────

let contractInstance = null;

async function main() {
  const rl = readline.createInterface({ input: process.stdin, terminal: false });
  for await (const line of rl) {
    const msg = JSON.parse(line.trim());
    if (msg.exit) break;

    // ── Deploy ────────────────────────────────────────────────────────────────
    if (msg.deploy) {
      try {
        const { fqn, constructor_args } = msg.deploy;
        // Always fresh deploy — no inject mode.
        // constructor_args is an array of ABI-encoded argument values,
        // produced by the Python LLM constructor synthesizer.
        const factory = await ethers.getContractFactory(fqn);
        const args    = constructor_args || [];
        const instance = await factory.deploy(...args);
        await instance.waitForDeployment();
        CONTRACT_ADDRESS = await instance.getAddress();
        contractInstance = instance;
        DEPLOY_STORAGE   = await fullStorageSnapshot(CONTRACT_ADDRESS);
        process.stdout.write(JSON.stringify({
          ok:           true,
          address:      CONTRACT_ADDRESS,
          deploy_slots: Object.keys(DEPLOY_STORAGE).length,
        }) + "\n");
      } catch (err) {
        process.stdout.write(JSON.stringify({ ok: false, error: err.message || String(err) }) + "\n");
      }
      continue;
    }

    // ── Reset storage ─────────────────────────────────────────────────────────
    if (msg.reset_storage) {
      try {
        await resetStorage(CONTRACT_ADDRESS, DEPLOY_STORAGE);
        process.stdout.write(JSON.stringify({ ok: true }) + "\n");
      } catch (err) {
        process.stdout.write(JSON.stringify({ ok: false, error: err.message || String(err) }) + "\n");
      }
      continue;
    }

    // ── Read on-chain state ───────────────────────────────────────────────────
    if (msg.read_state) {
      try {
        const state = await readState(
          msg.read_state.signer     || "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
          contractInstance,
          msg.read_state.state_vars || null,
        );
        process.stdout.write(JSON.stringify({ ok: true, state }) + "\n");
      } catch (err) {
        process.stdout.write(JSON.stringify({ ok: false, error: err.message || String(err) }) + "\n");
      }
      continue;
    }

    // ── Execute transaction ───────────────────────────────────────────────────
    if (msg.tx) {
      if (!CONTRACT_ADDRESS) {
        process.stdout.write(JSON.stringify({
          status: "error", return_value: null, logs: [],
          storage_after: {}, revert_reason: null,
          error: "contract not deployed",
        }) + "\n");
        continue;
      }
      const result = await executeTx(msg.tx);
      process.stdout.write(JSON.stringify(result) + "\n");
      continue;
    }

    // ── Stateless call (execute then reset to pre-call state) ─────────────────
    if (msg.call) {
      if (!CONTRACT_ADDRESS) {
        process.stdout.write(JSON.stringify({ ok: false, error: "not deployed" }) + "\n");
        continue;
      }
      const snap_before = await fullStorageSnapshot(CONTRACT_ADDRESS);
      const result      = await executeTx({
        from:  msg.call.from  || "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        input: msg.call.calldata,
        value: String(msg.call.value || 0),
        gas:   "3000000",
      });
      // Reset state after stateless call
      const current = await fullStorageSnapshot(CONTRACT_ADDRESS);
      for (const slot of Object.keys(current))
        if (!(slot in snap_before))
          await network.provider.send("hardhat_setStorageAt", [CONTRACT_ADDRESS, slot, ZERO]);
      for (const [slot, val] of Object.entries(snap_before))
        await network.provider.send("hardhat_setStorageAt", [CONTRACT_ADDRESS, slot, val]);
      process.stdout.write(JSON.stringify({ ok: true, result }) + "\n");
      continue;
    }

    // ── Stateful step (execute and keep state) ────────────────────────────────
    if (msg.step) {
      if (!CONTRACT_ADDRESS) {
        process.stdout.write(JSON.stringify({ ok: false, error: "not deployed" }) + "\n");
        continue;
      }
      const result = await executeTx({
        from:  msg.step.from  || "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        input: msg.step.calldata,
        value: String(msg.step.value || 0),
        gas:   "3000000",
      });
      process.stdout.write(JSON.stringify({ ok: true, result }) + "\n");
      continue;
    }
  }
  process.exit(0);
}
main();
"""

# ── EVM version helpers ───────────────────────────────────────────────────────

_EVM_MAP = {
    "homestead": "homestead", "tangerineWhistle": "tangerineWhistle",
    "spuriousDragon": "spuriousDragon", "byzantium": "byzantium",
    "constantinople": "constantinople", "petersburg": "petersburg",
    "istanbul": "istanbul", "berlin": "berlin", "london": "london",
    "paris": "paris", "shanghai": "paris", "merge": "paris",
    "cancun": "paris", "": "paris",
}

def _evm_version(evm_version: str, compiler_version: str) -> str:
    v = (evm_version or "").lower().strip()
    m = _EVM_MAP.get(v, "paris")
    try:
        parts = compiler_version.lstrip("v").split("+")[0].split(".")
        major, minor = int(parts[0]), int(parts[1])
        patch = int(parts[2]) if len(parts) > 2 else 0
        if major == 0 and minor == 8:
            if patch < 7:              m = "istanbul"
            elif patch < 18 and m == "paris": m = "london"
    except Exception:
        pass
    return m

# ── Hardhat config writer ─────────────────────────────────────────────────────

def write_hardhat_config(
    work_dir: Path,
    meta: dict,
    source_path: Path | None = None,
    fork_block: int | None = None,
) -> None:
    """
    Write hardhat.config.js into work_dir.
    Uses _hardhat_config from differential_fuzzer if available (handles
    ALCHEMY_KEY fork), otherwise writes a local-only config.
    """
    try:
        import sys
        sys.path.insert(0, str(work_dir.parent))
        from differential_fuzzer import _hardhat_config as _df_cfg, _detect_remappings
        remappings = _detect_remappings(source_path) if source_path else []
        cfg = _df_cfg(
            compiler_version=meta.get("compiler_version", "0.8.20"),
            optimization=meta.get("optimization_used", False),
            runs=int(meta.get("runs") or 200),
            evm_version=meta.get("evm_version", "paris"),
            fork_block=fork_block,
            remappings=remappings or None,
        )
        (work_dir / "hardhat.config.js").write_text(cfg)
        return
    except ImportError:
        pass

    ver   = meta.get("compiler_version", "0.8.20").lstrip("v").split("+")[0] or "0.8.20"
    major, minor, *_ = ver.split(".")
    is_old = int(major) == 0 and int(minor) < 6
    opt    = str(meta.get("optimization_used", False)).lower()
    runs   = int(meta.get("runs") or 200)
    evm    = "" if is_old else f'        evmVersion: "{_evm_version(meta.get("evm_version",""), ver)}",\n'

    alchemy_key = os.getenv("ALCHEMY_KEY", "")
    if alchemy_key and fork_block is not None:
        network_name = os.getenv("ALCHEMY_NETWORK", "eth-mainnet")
        fork_url     = f"https://{network_name}.g.alchemy.com/v2/{alchemy_key}"
        fork_section = (
            '    hardhat: {\n      forking: {\n'
            f'        url: "{fork_url}",\n'
            f'        blockNumber: {fork_block},\n'
            '      },\n'
            '      accounts: { accountsBalance: "10000000000000000000000" },\n'
            '      allowUnlimitedContractSize: true,\n    },\n'
        )
    else:
        fork_section = (
            '    hardhat: {\n'
            '      accounts: { accountsBalance: "10000000000000000000000" },\n'
            '      allowUnlimitedContractSize: true,\n    },\n'
        )

    cfg = (
        'require("@nomicfoundation/hardhat-toolbox");\n\n'
        'module.exports = {\n  networks: {\n' + fork_section + '  },\n'
        f'  solidity: {{\n    compilers: [{{\n      version: "{ver}",\n'
        f'      settings: {{\n        optimizer: {{ enabled: {opt}, runs: {runs} }},\n'
        f'{evm}'
        '      }\n    }]\n  }\n};\n'
    )
    (work_dir / "hardhat.config.js").write_text(cfg)

# ── Workspace setup ───────────────────────────────────────────────────────────

def setup_workspace(work_dir: Path) -> None:
    """Create work_dir, install Hardhat once, write SERVER_JS."""
    work_dir.mkdir(exist_ok=True)
    pkg = work_dir / "package.json"
    if not pkg.exists():
        pkg.write_text(json.dumps({
            "name": "hardhat-workspace", "version": "1.0.0", "private": True,
            "devDependencies": {
                "@nomicfoundation/hardhat-toolbox": "^4.0.0",
                "hardhat": "^2.22.0",
            }
        }, indent=2))
    if not (work_dir / "node_modules").exists():
        print("  Installing Hardhat (one-time)...")
        subprocess.run(["npm", "install"], cwd=work_dir, check=True)
    (work_dir / "hardhat_server.js").write_text(SERVER_JS)

# ── Fork block helper ─────────────────────────────────────────────────────────

def pick_fork_block(address: str, dataset_dir: Path) -> int | None:
    """Return one block before the first tx of the contract, or None."""
    tx_file = dataset_dir / address / "transactions.json"
    if not tx_file.exists():
        return None
    try:
        txs = json.loads(tx_file.read_text())
        if isinstance(txs, dict):
            txs = txs.get("transactions", txs.get("result", []))
        blocks = []
        for tx in txs:
            raw = tx.get("blockNumber")
            if raw is not None:
                try: blocks.append(int(raw))
                except: pass
        return max(0, min(blocks) - 1) if blocks else None
    except Exception:
        return None

# ── Constructor parameter synthesis ──────────────────────────────────────────

def _load_deploy_params(address: str, dataset_dir: Path) -> dict | None:
    """Load previously saved deploy params for this contract, if any."""
    p = dataset_dir / address / "deploy_params.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return None


def _save_deploy_params(address: str, dataset_dir: Path, data: dict) -> None:
    p = dataset_dir / address / "deploy_params.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def synthesize_constructor_args(
    abi: list,
    source_text: str,
    contract_name: str,
    llm_client,
    llm_model: str,
) -> list:
    """
    Use the LLM to propose constructor arguments for contracts whose constructors
    require parameters.

    Strategy: pass the constructor ABI + contract source to the LLM and ask for
    plausible values that allow the contract to be deployed in a test environment.
    For external contract address parameters (e.g. Uniswap router, HEX token),
    the LLM is instructed to use address(0) or a Hardhat account address, since
    the external contracts will not be available in a local deployment.

    Returns a list of argument values in ABI order, ready to pass to factory.deploy().
    Returns [] if the constructor takes no arguments or synthesis fails.
    """
    # Find constructor in ABI
    constructor = next(
        (e for e in abi if e.get("type") == "constructor"),
        None,
    )
    if not constructor or not constructor.get("inputs"):
        return []

    inputs = constructor["inputs"]

    if llm_client is None:
        # No LLM — synthesize safe defaults without calling the API
        return _default_constructor_args(inputs)

    # Build prompt
    inputs_desc = json.dumps(inputs, indent=2)
    # Truncate source if very long
    src = source_text[:8000] + ("\n... [truncated]" if len(source_text) > 8000 else "")

    prompt = f"""You are a Solidity test environment expert.

Contract: {contract_name}
Constructor ABI inputs:
{inputs_desc}

Contract source (truncated):
{src}

Task: propose constructor argument values so this contract can be deployed on a
local Hardhat test network. Rules:
- For address parameters that reference external contracts (e.g. Uniswap, Chainlink,
  HEX token, governance), use "0x0000000000000000000000000000000000000000" (address zero)
  OR one of these Hardhat test addresses if a non-zero address is needed:
    owner:  0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
    user1:  0x70997970C51812dc3A010C7d01b50e0d17dc79C8
    user2:  0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC
- For uint parameters (supply caps, fees, durations): use reasonable small values
  (e.g. 10000 for max supply, 0 for fees, 86400 for 1-day duration).
- For bool parameters: use false unless the name implies it should be true.
- For string/bytes parameters: use "" or "0x".
- The goal is a successful deploy, not realistic mainnet values.

Return ONLY a JSON array of argument values in the same order as the ABI inputs.
Example for inputs [address _token, uint256 _cap, bool _paused]:
["0x0000000000000000000000000000000000000000", 10000, false]

/no_think"""

    try:
        import re as _re
        response = llm_client.chat.completions.create(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
            temperature=0.0,
        )
        raw = response.choices[0].message.content.strip()
        raw = _re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        args = json.loads(raw)
        if not isinstance(args, list):
            raise ValueError("Expected JSON array")
        # Coerce types to match ABI
        coerced = []
        for inp, val in zip(inputs, args):
            t = inp.get("type", "uint256")
            if t == "address":
                coerced.append(str(val))
            elif t.startswith(("uint", "int")):
                coerced.append(int(val))
            elif t == "bool":
                coerced.append(bool(val) if not isinstance(val, str) else val.lower() == "true")
            elif t in ("string", "bytes"):
                coerced.append(str(val))
            else:
                coerced.append(val)
        return coerced
    except Exception as e:
        print(f"    [warn] LLM constructor synthesis failed: {e} — using defaults")
        return _default_constructor_args(inputs)


def _default_constructor_args(inputs: list) -> list:
    """Safe defaults when LLM is not available."""
    import re as _re
    DURATION_RE = _re.compile(r"(duration|period|time|delay|expiry|interval)", _re.IGNORECASE)
    defaults = []
    for inp in inputs:
        t = inp.get("type", "uint256")
        name = inp.get("name", "")
        if t == "address":
            defaults.append(OWNER_ADDR)          # was address(0) — causes mint/receiver failures
        elif t.startswith(("uint", "int")):
            defaults.append(86400 if DURATION_RE.search(name) else 0)
        elif t == "bool":
            defaults.append(False)
        elif t in ("string",):
            defaults.append("")
        elif t in ("bytes",):
            defaults.append("0x")
        else:
            defaults.append(0)
    return defaults

# ── HardhatServer ─────────────────────────────────────────────────────────────

class DeployFailedError(RuntimeError):
    """
    Raised when a contract cannot be deployed even after arg synthesis.
    Distinct from RuntimeError so callers can skip cleanly vs crash on
    unexpected errors (server crash, timeout, etc.).

    The failure is saved to deploy_params.json so subsequent runs skip
    immediately without retrying known-bad args.
    To retry, delete or edit deploy_params.json for that contract.
    """


class HardhatServer:
    """
    Persistent Node.js/Hardhat server subprocess.

    Single interface for all evaluation scripts:
      - deploy(fqn, abi, source_text, address, dataset_dir, llm_client)
      - send(msg)          — raw message, returns parsed response
      - close()

    The deploy method:
      1. Checks dataset_dir/<address>/deploy_params.json for cached args
      2. If not cached: calls synthesize_constructor_args (LLM or defaults)
      3. Sends {deploy: {fqn, constructor_args}} to Node.js
      4. On success: saves deploy_params.json with the args used
      5. On failure: if args were synthesized, retries once with all-zero defaults
    """

    def __init__(self, work_dir: Path, timeout: int = 60):
        self._work_dir = work_dir
        self._timeout  = timeout
        self._fqn: str | None = None
        self._constructor_args: list = []
        self._proc = self._spawn()

    def _spawn(self):
        env = os.environ.copy()
        env["MAX_SLOTS_PER_TX"] = env.get("MAX_SLOTS_PER_TX", "50")
        return subprocess.Popen(
            ["npx", "--yes", "hardhat", "run", "hardhat_server.js", "--network", "hardhat"],
            cwd=self._work_dir,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )

    def _kill(self):
        try:
            self._proc.kill()
            self._proc.wait(timeout=5)
        except Exception:
            pass

    def send(self, msg: dict, timeout: int | None = None) -> dict:
        """Send a message and return the parsed response. Auto-restarts on crash."""
        t = timeout or self._timeout
        try:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
            ready, _, _ = select.select([self._proc.stdout], [], [], t)
            if not ready:
                raise TimeoutError(f"No response after {t}s")
            while True:
                ready, _, _ = select.select([self._proc.stdout], [], [], t)
                if not ready:
                    raise TimeoutError(f"No response after {t}s")
                line = self._proc.stdout.readline()
                if not line:  # EOF — process died
                    err = self._proc.stderr.read() if self._proc.stderr else ""
                    raise RuntimeError(f"Node died:\n{err[-1000:]}")
                line = line.strip()
                if not line:
                    continue  # blank line, keep reading
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON noise (Hardhat warning/log) — log and keep reading
                    print(f"    [hardhat stdout] {line[:120]}")
                    continue
        except (TimeoutError, RuntimeError, BrokenPipeError, OSError):
            self._kill()
            self._proc = self._spawn()
            if self._fqn is not None:
                try:
                    self.send({
                        "deploy": {
                            "fqn":              self._fqn,
                            "constructor_args": self._constructor_args,
                        }
                    }, timeout=90)
                except Exception:
                    pass
            raise

    def deploy(
        self,
        fqn: str,
        abi: list,
        source_text: str,
        address: str,
        dataset_dir: Path,
        llm_client=None,
        llm_model: str = "claude-sonnet-4-20250514",
    ) -> dict:
        """
        Deploy the contract, synthesizing constructor args if needed.

        Returns the deploy response dict with keys:
          ok, address, deploy_slots, constructor_args, args_source
        Raises RuntimeError if deploy fails after all attempts.
        """
        self._fqn = fqn

        # 1. Try to load cached args
        cached = _load_deploy_params(address, dataset_dir)
        if cached:
            if cached.get("deploy_failed"):
                raise DeployFailedError(
                    f"Deploy previously failed for {fqn.split(':')[-1]} "
                    f"({cached.get('deploy_error','?')[:80]}). "
                    f"Edit {dataset_dir}/{address}/deploy_params.json to retry."
                )
            if cached.get("args_verified") and cached.get("constructor_args") is not None:
                # args_verified=true means a previous deploy with these args succeeded —
                # use them unconditionally without LLM re-synthesis, making re-runs
                # fully deterministic regardless of LLM non-determinism.
                args        = cached["constructor_args"]
                args_source = "cached_verified"
                print(f"    Using verified cached constructor args: {args}")
            elif cached.get("constructor_args") is not None:
                args        = cached["constructor_args"]
                args_source = "cached"
                print(f"    Using cached constructor args: {args}")
            else:
                cached = None  # treat as missing

        if not cached:
            # 2. Check if constructor needs args at all
            constructor = next(
                (e for e in abi if e.get("type") == "constructor"), None
            )
            if not constructor or not constructor.get("inputs"):
                args        = []
                args_source = "no_args"
            else:
                # 3. Synthesize args via LLM (or defaults)
                print(f"    Synthesizing constructor args for {fqn.split(':')[-1]} ...")
                args = synthesize_constructor_args(
                    abi, source_text, fqn.split(":")[-1],
                    llm_client, llm_model,
                )
                args_source = "llm_synthesis" if llm_client else "defaults"
                print(f"    Args ({args_source}): {args}")

        self._constructor_args = args

        # 4. Attempt deploy
        res = self.send({
            "deploy": {
                "fqn":              fqn,
                "constructor_args": args,
            }
        }, timeout=90)

        # 5a. If cached args failed, clear cache and retry with fresh LLM synthesis
        #     Skip this step for verified args — if verified args fail it means the
        #     contract genuinely cannot be deployed (e.g. fork not available, external
        #     dependency), so go straight to failure.
        if not res.get("ok") and args_source == "cached":
            print(f"    [warn] cached args failed ({res.get('error','')[:60]}), "
                  f"re-synthesizing ...")
            constructor = next((e for e in abi if e.get("type") == "constructor"), None)
            if constructor and constructor.get("inputs"):
                args = synthesize_constructor_args(
                    abi, source_text, fqn.split(":")[-1], llm_client, llm_model,
                )
                args_source = "llm_synthesis_retry" if llm_client else "defaults_retry"
                print(f"    Args ({args_source}): {args}")
                self._constructor_args = args
                res = self.send({
                    "deploy": {"fqn": fqn, "constructor_args": args}
                }, timeout=90)

        # 5b. If failed and args were synthesized, retry with all-zero defaults
        if not res.get("ok") and args_source in ("llm_synthesis", "llm_synthesis_retry",
                                                   "defaults", "defaults_retry") and args:
            constructor = next((e for e in abi if e.get("type") == "constructor"), None)
            if constructor and constructor.get("inputs"):
                fallback_args = _default_constructor_args(constructor["inputs"])
                if fallback_args != args:
                    print(f"    [warn] deploy failed ({res.get('error','')[:60]}), "
                          f"retrying with zero defaults")
                    self._constructor_args = fallback_args
                    res = self.send({
                        "deploy": {
                            "fqn":              fqn,
                            "constructor_args": fallback_args,
                        }
                    }, timeout=90)
                    if res.get("ok"):
                        args        = fallback_args
                        args_source = "defaults_fallback"

        if not res.get("ok"):
            # Save the failure so subsequent runs skip immediately instead of
            # retrying the same args that are known to fail.
            error_msg = res.get("error", "unknown error")
            _save_deploy_params(address, dataset_dir, {
                "fqn":              fqn,
                "constructor_args": args,
                "args_source":      args_source,
                "deploy_failed":    True,
                "deploy_error":     error_msg,
                "successful_calls": [],
            })
            raise DeployFailedError(
                f"Deploy failed: {error_msg}\n"
                f"  contract: {fqn.split(':')[-1]}\n"
                f"  args ({args_source}): {args}\n"
                f"  hint: edit {dataset_dir}/{address}/deploy_params.json "
                f"to supply correct constructor args and re-run."
            )

        # 6. Save successful args + record deploy call
        deploy_record = {
            "fqn":              fqn,
            "constructor_args": args,
            "args_source":      args_source,
            "args_verified":    True,   # deploy succeeded with these args — use on re-run
            "deployed_address": res.get("address"),
            "successful_calls": [
                {
                    "call_type":  "constructor",
                    "from":       OWNER_ADDR,
                    "args":       args,
                    "args_source": args_source,
                    "fqn":        fqn,
                }
            ],
        }
        _save_deploy_params(address, dataset_dir, deploy_record)

        res["constructor_args"] = args
        res["args_source"]      = args_source
        return res

    def record_successful_call(
        self,
        address: str,
        dataset_dir: Path,
        fn_name: str,
        fn_sig: str,
        from_addr: str,
        params: list,
        value_wei: int,
        param_source: str,
        event_id: str | None = None,
        branch_guard: str = "",
        sequence_idx: int | None = None,
        seed: int | None = None,
    ) -> None:
        """
        Append a successful call to the deploy_params.json corpus.

        Fields recorded per call:
          - fn_name, fn_sig      — function identifier
          - from                 — signer address
          - params               — decoded parameter values
          - value_wei            — ETH value sent
          - param_source         — where params came from:
                                   dcr_call_params | guard_synthesis |
                                   llm_synthesis | historical
          - event_id             — DCR event id that triggered this call
          - branch_guard         — guard string of the branch being explored
                                   (empty string if no branch or drain phase)
          - sequence_idx         — which sequence (0-based) this call belongs to
          - seed                 — random seed used for this sequence

        The resulting deploy_params.json["successful_calls"] is an ordered list:
          [constructor, tx_seq0_step0, tx_seq0_step1, ..., tx_seq1_step0, ...]
        This gives a complete branch coverage record for the contract.
        """
        cached = _load_deploy_params(address, dataset_dir) or {}
        calls  = cached.get("successful_calls", [])
        calls.append({
            "call_type":    "tx",
            "fn_name":      fn_name,
            "fn_sig":       fn_sig,
            "from":         from_addr,
            "params":       params,
            "value_wei":    value_wei,
            "param_source": param_source,
            "event_id":     event_id,
            "branch_guard": branch_guard,
            "sequence_idx": sequence_idx,
            "seed":         seed,
        })
        cached["successful_calls"] = calls
        _save_deploy_params(address, dataset_dir, cached)

    def close(self):
        try:
            self._proc.stdin.write(json.dumps({"exit": True}) + "\n")
            self._proc.stdin.flush()
            self._proc.wait(timeout=10)
        except Exception:
            self._kill()


# ── Source helpers ────────────────────────────────────────────────────────────

def write_source(work_dir: Path, source_path: Path, contract_name: str,
                 prefix: str = "") -> tuple[str, str]:
    """
    Copy source into work_dir/contracts/ and return (fqn, source_text).
    prefix is prepended to the filename to avoid naming conflicts between
    original and generated contracts in the same workspace.
    """
    contracts_dir = work_dir / "contracts"
    if contracts_dir.exists():
        import shutil
        shutil.rmtree(contracts_dir)
    contracts_dir.mkdir(parents=True)

    p = prefix + "_" if prefix else ""

    if source_path.suffix == ".sol":
        dest = contracts_dir / f"{p}{source_path.name}"
        text = source_path.read_text()
        dest.write_text(text)
        return f"contracts/{p}{source_path.name}:{contract_name}", text

    data      = json.loads(source_path.read_text())
    first_fp  = None
    all_text  = []
    for fp, content in data.get("sources", {}).items():
        dest = contracts_dir / (p + fp if p else fp)
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = content.get("content", "")
        dest.write_text(text)
        all_text.append(text)
        if first_fp is None:
            first_fp = (p + fp if p else fp)
    return f"contracts/{first_fp}:{contract_name}", "\n".join(all_text)


def install_npm_deps(work_dir: Path, source_path: Path) -> None:
    """Install npm packages required by the contract source."""
    if source_path.suffix == ".sol":
        texts = [source_path.read_text()]
    else:
        data  = json.loads(source_path.read_text())
        texts = [v.get("content", "") for v in data.get("sources", {}).values()]

    raw_imports: set[str] = set()
    for text in texts:
        for m in re.finditer(
            r'''import\s+(?:[^"']*\s+from\s+)?["']([^"']+)["']''', text, re.MULTILINE
        ):
            imp = m.group(1).strip()
            if not imp.startswith(".") and not imp.startswith("/"):
                raw_imports.add(imp)

    packages: set[str] = set()
    for imp in raw_imports:
        pkg = "/".join(imp.split("/")[:2]) if imp.startswith("@") else imp.split("/")[0]
        packages.add(pkg)

    _SKIP  = {"permit2", "ds-test", "forge-std"}
    _REMAP = {
        "@openzeppelin/contracts": ("@openzeppelin/contracts", "@openzeppelin/contracts"),
        "openzeppelin-contracts":  ("@openzeppelin/contracts", "@openzeppelin/contracts"),
        "@uniswap/v2-core":        ("@uniswap/v2-core",        "@uniswap/v2-core"),
        "@uniswap/v3-core":        ("@uniswap/v3-core",        "@uniswap/v3-core"),
        "@uniswap/v3-periphery":   ("@uniswap/v3-periphery",   "@uniswap/v3-periphery"),
        "solmate":                 ("solmate",                  "solmate"),
        "@rari-capital/solmate":   ("@rari-capital/solmate",    "@rari-capital/solmate"),
    }
    node_modules = work_dir / "node_modules"
    to_install   = []
    for pkg in packages:
        if pkg in _SKIP: continue
        npm_pkg, check_dir = _REMAP.get(pkg, (pkg, pkg))
        if not (node_modules / check_dir).exists():
            to_install.append(npm_pkg)
    if to_install:
        subprocess.run(
            ["npm", "install", "--save-dev"] + list(dict.fromkeys(to_install)),
            cwd=work_dir, capture_output=True, text=True,
        )