"""
deploy_replay.py

Compila un contratto Solidity su Hardhat, lo deploya fresh (mai inject),
e riproduce le prime k transazioni storiche confrontando il risultato
con quello atteso (pass/revert).

Fresh deploy invece di inject
──────────────────────────────
inject mode (hardhat_setCode) riusa il bytecode mainnet all'indirizzo
originale: lo storage è quello del fork, ma tx.from nei log storici
sono indirizzi mainnet che non corrispondono all'account Hardhat.
Con fresh deploy l'owner è sempre 0xf39F... (primo account Hardhat),
lo storage parte da zero, e il replay è riproducibile.
Se il costruttore richiede argomenti, vengono sintetizzati dall'LLM
in base alla firma del costruttore e al sorgente del contratto.

Checkpoint granulare per contratto
────────────────────────────────────
Ogni contratto passa attraverso gli stati:
  NOT_STARTED → COMPILED → DEPLOYED → DONE | FAILED

Se lo script viene interrotto durante REPLAYING, alla ripresa si
torna a COMPILED (recompile + fresh deploy + replay da tx 0).
Questo è corretto perché il fresh deploy è deterministico.
Il checkpoint è salvato in <output_dir>/<address>/checkpoint.json.
"""

import os
import re
import json
import select
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from eth_abi import decode as abi_decode

# ── Costanti Hardhat ──────────────────────────────────────────────────────────

OWNER_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

_EVM_MAP = {
    "homestead": "homestead", "tangerineWhistle": "tangerineWhistle",
    "spuriousDragon": "spuriousDragon", "byzantium": "byzantium",
    "constantinople": "constantinople", "petersburg": "petersburg",
    "istanbul": "istanbul", "berlin": "berlin", "london": "london",
    "paris": "paris", "shanghai": "paris", "merge": "paris",
    "cancun": "paris", "default": "paris", "": "paris",
}


def _evm_version(evm_str: str, compiler_version: str) -> str:
    m = _EVM_MAP.get((evm_str or "").lower().strip(), "paris")
    try:
        parts = compiler_version.lstrip("v").split("+")[0].split(".")
        major, minor = int(parts[0]), int(parts[1])
        patch = int(parts[2]) if len(parts) > 2 else 0
        if major == 0 and minor == 8:
            if patch < 7:
                m = "istanbul"
            elif patch < 18 and m == "paris":
                m = "london"
    except Exception:
        pass
    return m


def _get_deploy_tx(address: str, deploy_block: int):
    from etherscan_utils import get_block
    from etherscan_utils import _rpc_alchemy  # già lo usi altrove

    block = get_block(deploy_block)
    if not block:
        return None

    for tx in block.get("transactions", []):
        if tx.get("to") is None:
            try:
                receipt = _rpc_alchemy("eth_getTransactionReceipt", [tx["hash"]])
                if receipt.get("contractAddress", "").lower() == address.lower():
                    return tx
            except Exception:
                continue
    return None


def _decode_constructor_args(args_data: str, abi: list):
    for entry in abi:
        if entry.get("type") == "constructor":
            types = [inp["type"] for inp in entry.get("inputs", [])]
            if not types:
                return []
            try:
                decoded = abi_decode(types, bytes.fromhex(args_data[2:]))
                return list(decoded)
            except Exception:
                return None
    return []


def _try_etherscan_args(source_data: dict, abi: list):
    raw = source_data.get("ConstructorArguments")
    if not raw:
        return None

    args_data = "0x" + raw
    return _decode_constructor_args(args_data, abi)


def _extract_args_from_tx(tx_input: str, abi: list):
    if not tx_input or tx_input == "0x":
        return None

    data = tx_input[2:]

    # prova split a multipli di 32 byte
    for i in range(0, len(data), 64):
        candidate = "0x" + data[i:]
        if len(candidate) <= 2:
            continue

        decoded = _decode_constructor_args(candidate, abi)
        if decoded is not None:
            return decoded

    return None


# ── Server Node.js ────────────────────────────────────────────────────────────
# Unico server condiviso per compile + deploy + replay.
# Messaggi:
#   {deploy: {fqn, constructor_args}}  → {ok, address} | {ok:false, error}
#   {tx: <tx_object>}                  → {hash, actual_status, events_match, ...}
#   {exit: true}

_SERVER_JS = r"""
const { ethers, artifacts } = require("hardhat");
const readline = require("readline");

async function deployContract(fqn, args) {
  let factory;
  try {
    factory = await ethers.getContractFactory(fqn);
  } catch (_) {
    const all   = await artifacts.getAllFullyQualifiedNames();
    const short = fqn.includes(":") ? fqn.split(":").pop() : fqn;
    const match = all.find(n => n === fqn || n.endsWith(":" + short));
    if (!match) throw new Error("Contract not found: " + fqn +
      ". Available: " + all.slice(0, 8).join(", "));
    factory = await ethers.getContractFactory(match);
  }
  const instance = await factory.deploy(...(args || []));
  await instance.waitForDeployment();
  return await instance.getAddress();
}

async function replayTx(tx, contractAddress) {
  const result = {
    hash:            tx.hash,
    block:           tx.blockNumber,
    from:            tx.from,
    expected_status: tx.txreceipt_status === "1" ? "success" : "revert",
    actual_status:   null,
    events_match:    null,
    revert_reason:   null,
    error:           null,
  };
  try {
    await network.provider.request({
      method: "hardhat_impersonateAccount", params: [tx.from]
    });
    await network.provider.send("hardhat_setBalance",
      [tx.from, "0x56BC75E2D63100000"]);
    const signer = await ethers.getSigner(tx.from);
    try {
      const sent    = await signer.sendTransaction({
        to: contractAddress, data: tx.input,
        value: BigInt(tx.value), gasLimit: BigInt(tx.gas) * 2n,
      });
      const receipt = await sent.wait();
      result.actual_status = receipt.status === 1 ? "success" : "revert";
    } catch (err) {
      result.actual_status = "revert";
      result.revert_reason = err.message || String(err);
    }
    result.events_match = result.actual_status === result.expected_status;
    await network.provider.request({
      method: "hardhat_stopImpersonatingAccount", params: [tx.from]
    });
  } catch (err) {
    result.error         = err.message || String(err);
    result.actual_status = "error";
    result.events_match  = false;
  }
  return result;
}

let contractAddress = null;

async function main() {
  const rl = readline.createInterface({ input: process.stdin, terminal: false });
  for await (const line of rl) {
    const msg = JSON.parse(line.trim());
    if (msg.exit) break;

    if (msg.deploy) {
      try {
        contractAddress = await deployContract(
          msg.deploy.fqn, msg.deploy.constructor_args || []
        );
        process.stdout.write(JSON.stringify({ ok: true, address: contractAddress }) + "\n");
      } catch (err) {
        process.stdout.write(JSON.stringify({ ok: false, error: err.message || String(err) }) + "\n");
      }
      continue;
    }

    if (msg.tx) {
      if (!contractAddress) {
        process.stdout.write(JSON.stringify({
          hash: msg.tx.hash, events_match: false,
          actual_status: "error", error: "not deployed"
        }) + "\n");
        continue;
      }
      const result = await replayTx(msg.tx, contractAddress);
      process.stdout.write(JSON.stringify(result) + "\n");
      continue;
    }
  }
  process.exit(0);
}
main();
"""


# ── Hardhat config ────────────────────────────────────────────────────────────

def _extract_pragma_version(source_text: str) -> str | None:
    """
    Estrae la versione Solidity dal pragma del sorgente.
    Restituisce la versione esatta se specificata (es. '0.8.17'),
    oppure la versione minima se è un range (es. '^0.8.0' → '0.8.0').
    """
    m = re.search(r'pragma\s+solidity\s+([^;]+);', source_text)
    if not m:
        return None
    spec = m.group(1).strip()
    # Versione esatta: =0.8.17 o 0.8.17
    exact = re.search(r'(?:^|=)(\d+\.\d+\.\d+)', spec)
    if exact:
        return exact.group(1)
    # Range con caret o tilde: ^0.8.0, ~0.8.0 → usa la versione minima
    lower = re.search(r'[\^~>=]+\s*(\d+\.\d+\.\d+)', spec)
    if lower:
        return lower.group(1)
    # Solo major.minor: 0.8 → 0.8.0
    mm = re.search(r'(\d+\.\d+)', spec)
    if mm:
        return mm.group(1) + ".0"
    return None


def _hardhat_config(
    meta: dict,
    work_dir: Path,
    remappings: Optional[list] = None,
    source_text: str = "",
) -> str:

    meta_ver = meta.get("compiler_version", "").lstrip("v").split("+")[0].strip()
    pragma_ver = _extract_pragma_version(source_text) if source_text else None

    if meta_ver and re.match(r"^\d+\.\d+\.\d+$", meta_ver):
        ver = meta_ver
    elif pragma_ver:
        ver = pragma_ver
    else:
        ver = "0.8.20"

    major, minor, *_ = ver.split(".")
    is_old = int(major) == 0 and int(minor) < 6

    opt = str(meta.get("optimization_used", False)).lower()
    runs = int(meta.get("runs") or 200)

    evm = ""
    if not is_old:
        evm = (
            f'        evmVersion: "{_evm_version(meta.get("evm_version", ""), ver)}",\n'
        )

    remap = ""
    if remappings:
        rj = "[" + ", ".join(f'"{r}"' for r in remappings) + "]"
        remap = f"        remappings: {rj},\n"

    overrides = "    overrides: {},\n" if not is_old else ""

    return (
        'require("@nomicfoundation/hardhat-toolbox");\n\n'
        "module.exports = {\n"
        "  networks: {\n"
        "    hardhat: {\n"
        '      accounts: { accountsBalance: "10000000000000000000000" },\n'
        "      allowUnlimitedContractSize: true,\n"
        "    },\n"
        "  },\n"
        "  solidity: {\n"
        "    compilers: [{\n"
        f'      version: "{ver}",\n'
        "      settings: {\n"
        f"        optimizer: {{ enabled: {opt}, runs: {runs} }},\n"
        + evm
        + remap
        + "      }\n"
        "    }],\n"
        + overrides
        + "  }\n"
        "};\n"
    )


# ── Workspace helpers ─────────────────────────────────────────────────────────

def setup_workspace(work_dir: Path) -> None:
    work_dir.mkdir(exist_ok=True)
    pkg = work_dir / "package.json"
    if not pkg.exists():
        pkg.write_text(json.dumps({
            "name": "replay-workspace", "version": "1.0.0", "private": True,
            "devDependencies": {
                "@nomicfoundation/hardhat-toolbox": "^4.0.0",
                "hardhat": "^2.22.0",
            }
        }, indent=2))
    if not (work_dir / "node_modules").exists():
        print("  Installing Hardhat (one-time) ...")
        subprocess.run(["npm", "install"], cwd=work_dir, check=True,
                       capture_output=True)
    (work_dir / "deploy_server.js").write_text(_SERVER_JS)


def _write_source(work_dir: Path, source_path: Path) -> None:
    contracts_dir = work_dir / "contracts"
    if contracts_dir.exists():
        shutil.rmtree(contracts_dir)
    contracts_dir.mkdir(parents=True)
    if source_path.suffix == ".sol":
        (contracts_dir / source_path.name).write_text(source_path.read_text())
    else:
        data = json.loads(source_path.read_text())
        for fp, content in data.get("sources", {}).items():
            dest = contracts_dir / fp
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content.get("content", ""))


def _install_npm_deps(work_dir: Path, source_path: Path) -> None:
    texts = (
        [source_path.read_text()] if source_path.suffix == ".sol"
        else [v.get("content", "") for v in
              json.loads(source_path.read_text()).get("sources", {}).values()]
    )
    raw: set[str] = set()
    pat = re.compile(r'''import\s+(?:[^"']*\s+from\s+)?["']([^"']+)["']''', re.MULTILINE)
    for text in texts:
        for m in pat.finditer(text):
            imp = m.group(1).strip()
            if not imp.startswith(".") and not imp.startswith("/"):
                raw.add(imp)

    packages: set[str] = {
        "/".join(i.split("/")[:2]) if i.startswith("@") else i.split("/")[0]
        for i in raw
    }
    _SKIP  = {"permit2", "ds-test", "forge-std"}
    _REMAP = {
        "@openzeppelin/contracts": "@openzeppelin/contracts",
        "openzeppelin-contracts":  "@openzeppelin/contracts",
        "@uniswap/v2-core":        "@uniswap/v2-core",
        "@uniswap/v3-core":        "@uniswap/v3-core",
        "@uniswap/v3-periphery":   "@uniswap/v3-periphery",
        "solmate":                 "solmate",
        "@rari-capital/solmate":   "@rari-capital/solmate",
        "erc721a":                 "erc721a",
    }
    nm = work_dir / "node_modules"
    to_install = [
        _REMAP.get(p, p) for p in packages
        if p not in _SKIP and not (nm / _REMAP.get(p, p)).exists()
    ]
    to_install = list(dict.fromkeys(to_install))
    if to_install:
        subprocess.run(
            ["npm", "install", "--save-dev"] + to_install,
            cwd=work_dir, capture_output=True, text=True,
        )


def _detect_remappings(work_dir: Path, source_path: Path) -> list[str]:
    nm = work_dir / "node_modules"
    texts = (
        [source_path.read_text()] if source_path.suffix == ".sol"
        else [v.get("content", "") for v in
              json.loads(source_path.read_text()).get("sources", {}).values()]
    )
    imports: set[str] = set()
    for text in texts:
        for m in re.finditer(r'''import\s+(?:[^"']*\s+from\s+)?["']([^"']+)["']''', text):
            imports.add(m.group(1).strip())

    remappings = []
    for pkg in ("solmate", "@rari-capital/solmate"):
        pkg_dir = nm / pkg
        if not (pkg_dir / "src").is_dir():
            continue
        bare = pkg + "/"
        src  = pkg + "/src/"
        if any(i.startswith(bare) and not i.startswith(src) for i in imports):
            remappings.append(f"{bare}=node_modules/{pkg}/src/")
    return remappings


# ── LLM constructor synthesis ─────────────────────────────────────────────────

_OWNER_PARAM_RE = re.compile(
    r"(owner|admin|deployer|governance|treasury|multisig|operator|manager)",
    re.IGNORECASE,
)


def _default_args(inputs: list) -> list:
    """Defaults deterministici: parametri owner-like ricevono OWNER_ADDR."""
    out = []
    for inp in inputs:
        t    = inp.get("type", "uint256")
        name = inp.get("name", "")
        if t == "address":
            out.append(OWNER_ADDR if _OWNER_PARAM_RE.search(name)
                       else "0x0000000000000000000000000000000000000000")
        elif t.startswith(("uint", "int")): out.append(0)
        elif t == "bool":                   out.append(False)
        elif t in ("string",):              out.append("")
        elif t in ("bytes",):               out.append("0x")
        else:                               out.append(0)
    return out


def synthesize_constructor_args(
    abi: list,
    source_text: str,
    contract_name: str,
    llm_client,
    llm_model: str,
) -> list:
    ctor = next((e for e in abi if e.get("type") == "constructor"), None)
    if not ctor or not ctor.get("inputs"):
        return []
    inputs = ctor["inputs"]

    if llm_client is None:
        return _default_args(inputs)

    # Pre-classifica i parametri in modo deterministico prima di chiamare l'LLM:
    # nomi che chiaramente indicano owner/admin ricevono sempre OWNER_ADDR.
    # Questo evita che l'LLM scelga address(0) per parametri critici.
    _TOKEN_PARAMS = re.compile(
        r"(token|asset|currency|collateral|underlying|want)",
        re.IGNORECASE,
    )

    quick_args = []
    all_resolved = True
    for inp in inputs:
        t    = inp.get("type", "")
        name = inp.get("name", "")
        if t == "address":
            if _OWNER_PARAM_RE.search(name):
                quick_args.append(OWNER_ADDR)
            elif _TOKEN_PARAMS.search(name):
                # Indirizzo token esterno: usa address(0), il contratto non
                # chiamerà il token durante il deploy
                quick_args.append("0x0000000000000000000000000000000000000000")
            else:
                all_resolved = False
                quick_args.append(None)
        else:
            all_resolved = False
            quick_args.append(None)

    # Se tutti i parametri sono stati risolti deterministicamente, non serve l'LLM
    if all_resolved:
        return quick_args

    # Altrimenti chiedi all'LLM solo per i parametri non risolti
    src_trunc = source_text[:8000] + ("\n...[truncated]" if len(source_text) > 8000 else "")
    inputs_desc = json.dumps(inputs, indent=2)

    prompt = f"""You are a Solidity test environment expert.

Contract: {contract_name}
Constructor ABI inputs:
{inputs_desc}

Contract source (truncated):
{src_trunc}

Task: propose constructor argument values for deployment on a local Hardhat
test network. Follow these rules strictly:

ADDRESS PARAMETERS:
- If the parameter name contains "owner", "admin", "deployer", "governance",
  "treasury", "multisig", "operator", or "manager": use EXACTLY
  "{OWNER_ADDR}"
- If the parameter name refers to an external token/asset contract: use
  "0x0000000000000000000000000000000000000000"
- For any other address parameter: use "{OWNER_ADDR}"
- NEVER use "0x0000000000000000000000000000000000000000" for owner/admin params

NUMERIC PARAMETERS:
- Supply caps, max amounts: 10000
- Fees in basis points: 0
- Durations in seconds: 86400
- Prices/rates: 0

OTHER PARAMETERS:
- bool: false (unless name implies true like "paused" → false, "active" → true)
- string: ""
- bytes: "0x"

Return ONLY a JSON array of values in the same order as the ABI inputs.
Example for [address _owner, address _token, uint256 _cap, bool _paused]:
["{OWNER_ADDR}", "0x0000000000000000000000000000000000000000", 10000, false]

/no_think"""

    try:
        resp = llm_client.chat.completions.create(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256, temperature=0.0,
        )
        raw  = resp.choices[0].message.content.strip()
        raw  = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        args = json.loads(raw)
        if not isinstance(args, list):
            raise ValueError("not a list")

        # Applica override deterministici: i parametri già risolti
        # sovrascrivono la risposta LLM
        coerced = []
        for i, (inp, val) in enumerate(zip(inputs, args)):
            if quick_args[i] is not None:
                coerced.append(quick_args[i])
                continue
            t = inp.get("type", "uint256")
            if t == "address":
                coerced.append(str(val))
            elif t.startswith(("uint", "int")):
                coerced.append(int(val))
            elif t == "bool":
                coerced.append(bool(val) if not isinstance(val, str)
                               else val.lower() == "true")
            else:
                coerced.append(val)
        return coerced

    except Exception as e:
        print(f"    [warn] LLM constructor synthesis failed: {e} — using defaults")
        # Applica comunque gli override deterministici sui defaults
        defaults = _default_args(inputs)
        return [
            quick_args[i] if quick_args[i] is not None else defaults[i]
            for i in range(len(inputs))
        ]


# ── Hardhat server process ────────────────────────────────────────────────────

class DeployServer:
    """Processo Node.js persistente per compile + deploy + replay."""

    def __init__(self, work_dir: Path, tx_timeout: int = 60):
        self._work_dir   = work_dir
        self._tx_timeout = tx_timeout
        self._proc       = self._spawn()
        self._address: Optional[str] = None

    def _spawn(self):
        env = os.environ.copy()
        return subprocess.Popen(
            ["npx", "hardhat", "run", "deploy_server.js", "--network", "hardhat"],
            cwd=self._work_dir,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )

    def _send(self, msg: dict, timeout: int) -> dict:
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()
        ready, _, _ = select.select([self._proc.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError(f"No response after {timeout}s")
        line = self._proc.stdout.readline()
        if not line.strip():
            raise RuntimeError(f"Server died:\n{self._proc.stderr.read()[-500:]}")
        return json.loads(line)

    def deploy(self, fqn: str, constructor_args: list) -> str:
        """Deploya il contratto e restituisce l'indirizzo. Raise su errore."""
        res = self._send(
            {"deploy": {"fqn": fqn, "constructor_args": constructor_args}},
            timeout=120,
        )
        if not res.get("ok"):
            raise RuntimeError(f"Deploy failed: {res.get('error','?')}")
        self._address = res["address"]
        return self._address

    def replay_tx(self, tx: dict) -> dict:
        return self._send({"tx": tx}, timeout=self._tx_timeout)

    def close(self):
        try:
            self._proc.stdin.write(json.dumps({"exit": True}) + "\n")
            self._proc.stdin.flush()
            self._proc.wait(timeout=10)
        except Exception:
            self._proc.kill()


# ── Checkpoint helpers ────────────────────────────────────────────────────────
# Checkpoint per contratto: salvato in <contract_dir>/checkpoint.json
# Stati: NOT_STARTED | COMPILED | DEPLOYED | DONE | FAILED

def _ckpt_path(contract_dir: Path) -> Path:
    return contract_dir / "checkpoint.json"


def _load_ckpt(contract_dir: Path) -> dict:
    p = _ckpt_path(contract_dir)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"state": "NOT_STARTED"}


def _save_ckpt(contract_dir: Path, data: dict) -> None:
    _ckpt_path(contract_dir).write_text(json.dumps(data, indent=2))


# ── Main entry point ──────────────────────────────────────────────────────────

def run_fresh_deploy_replay(
    address: str,
    source_path: Path,
    meta: dict,
    source_data: dict,
    abi: list,
    callable_txs: list,
    k: int,
    work_dir: Path,
    output_dir: Path,
    pass_threshold: float,
    tx_timeout: int,
    llm_client,
    llm_model: str,
) -> dict:
    """
    Compila, deploya fresh e riproduce le prime min(k, len(callable_txs))
    transazioni.  Gestisce il checkpoint granulare per contratto.

    Checkpoint behaviour:
      - Se il contratto è già DONE o FAILED nel checkpoint, ritorna subito.
      - Se è COMPILED o DEPLOYED (interruzione precedente), riparte da
        COMPILED (recompile + fresh deploy) perché il deploy su Hardhat
        non sopravvive al riavvio del processo.
      - Durante REPLAYING salva i risultati parziali ma non aggiorna lo
        stato a DONE finché non è completo: una nuova interruzione fa
        ripetere il replay dall'inizio (corretto perché deterministico).
    """
    contract_dir = output_dir / address
    contract_dir.mkdir(parents=True, exist_ok=True)

    ckpt = _load_ckpt(contract_dir)
    if ckpt.get("state") in ("DONE", "FAILED"):
        print(f"  [{address[:10]}] already {ckpt['state']} — skip")
        return ckpt

    txs_to_replay = callable_txs[:k]
    if not txs_to_replay:
        result = {"state": "FAILED", "reason": "no callable txs", "address": address}
        _save_ckpt(contract_dir, result)
        return result

    contract_name = meta.get("contract_name", "Contract")

    # ── 1. Compile ────────────────────────────────────────────────────────
    print(f"  Compiling {contract_name} ...", end="  ", flush=True)
    _write_source(work_dir, source_path)
    _install_npm_deps(work_dir, source_path)
    remappings = _detect_remappings(work_dir, source_path)
    # Leggi source_text qui se non ancora disponibile (serve per pragma detection)
    _src_for_config = source_path.read_text() if source_path.suffix == ".sol" else "\n".join(
        v.get("content", "") for v in
        json.loads(source_path.read_text()).get("sources", {}).values()
    )
    (work_dir / "hardhat.config.js").write_text(
        _hardhat_config(meta, work_dir, remappings or None, source_text=_src_for_config)
    )
    cp = subprocess.run(
        ["npx", "hardhat", "compile", "--force"],
        cwd=work_dir, capture_output=True, text=True,
    )
    if cp.returncode != 0:
        print("✗")
        result = {"state": "FAILED", "reason": "compilation failed",
                  "stderr": cp.stderr[-500:], "address": address}
        _save_ckpt(contract_dir, result)
        return result
    print("✓")
    _save_ckpt(contract_dir, {"state": "COMPILED", "address": address})

    # ── 2. Synthesize constructor args ────────────────────────────────────
    # Leggi il sorgente testuale per il prompt LLM
    if source_path.suffix == ".sol":
        source_text = source_path.read_text()
    else:
        data = json.loads(source_path.read_text())
        source_text = "\n".join(
            v.get("content", "") for v in data.get("sources", {}).values()
        )

    """
    constructor_args = synthesize_constructor_args(
        abi, source_text, contract_name, llm_client, llm_model
    )
    
    

    if constructor_args:
        print(f"  Constructor args (LLM): {constructor_args}")
    else:
        print(f"  Constructor: no args")
    """

    constructor_args = None

    # 1. Etherscan
    #constructor_args = _try_etherscan_args(meta, abi)
    constructor_args = _try_etherscan_args(source_data, abi)
    if constructor_args is not None:
        print(f"  Constructor args (etherscan): {constructor_args}")

    # 2. Deploy tx
    if constructor_args is None:
        try:
            deploy_block = meta.get("deploy_block")
            if deploy_block:
                tx = _get_deploy_tx(address, deploy_block)
                if tx:
                    constructor_args = _extract_args_from_tx(tx.get("input"), abi)
                    if constructor_args is not None:
                        print(f"  Constructor args (tx decode): {constructor_args}")
        except Exception as e:
            print(f"  [warn] deploy tx decode failed: {e}")

    # 3. LLM fallback
    if constructor_args is None:
        constructor_args = synthesize_constructor_args(
            abi, source_text, contract_name, llm_client, llm_model
        )
        print(f"  Constructor args (LLM): {constructor_args}")

    if not constructor_args:
        print(f"  Constructor: no args")

    # Determina il FQN del contratto
    if source_path.suffix == ".sol":
        fqn = f"contracts/{source_path.name}:{contract_name}"
    else:
        data   = json.loads(source_path.read_text())
        first  = next(iter(data.get("sources", {})), None)
        fqn    = f"contracts/{first}:{contract_name}" if first else contract_name

    # ── 3. Deploy ─────────────────────────────────────────────────────────
    server = DeployServer(work_dir, tx_timeout=tx_timeout)
    try:
        print(f"  Deploying {fqn} ...", end="  ", flush=True)
        try:
            deployed_address = server.deploy(fqn, constructor_args)
        except RuntimeError as e:
            print(f"✗  {e}")
            # Retry once with zero defaults
            fallback = _default_args(
                next((e for e in abi if e.get("type") == "constructor"), {}).get("inputs", [])
            )
            if fallback != constructor_args:
                print(f"  Retrying with defaults: {fallback} ...", end="  ", flush=True)
                try:
                    deployed_address = server.deploy(fqn, fallback)
                    constructor_args = fallback
                except RuntimeError as e2:
                    print(f"✗  {e2}")
                    server.close()
                    result = {"state": "FAILED", "reason": f"deploy failed: {e2}",
                              "address": address}
                    _save_ckpt(contract_dir, result)
                    return result
            else:
                server.close()
                result = {"state": "FAILED", "reason": f"deploy failed: {e}",
                          "address": address}
                _save_ckpt(contract_dir, result)
                return result

        print(f"✓  {deployed_address[:14]}…")
        _save_ckpt(contract_dir, {
            "state": "DEPLOYED", "address": address,
            "deployed_address": deployed_address,
            "constructor_args": constructor_args,
        })

        # ── 4. Replay ─────────────────────────────────────────────────────
        print(f"  Replaying {len(txs_to_replay)} txs ...")
        tx_results, passed, failed = [], 0, 0

        for i, tx in enumerate(txs_to_replay, 1):
            short    = tx.get("hash", "")[:12]
            expected = "✓" if tx.get("txreceipt_status") == "1" else "✗"
            try:
                res = server.replay_tx(tx)
                ok  = res.get("events_match", False)
                if ok:
                    passed += 1
                    marker = "✓"
                else:
                    failed += 1
                    marker = "✗"
                    reason = (res.get("revert_reason") or res.get("error") or "")[:80]
                tx_results.append(res)
            except TimeoutError:
                failed += 1
                marker = "T"
                tx_results.append({"hash": tx.get("hash"), "actual_status": "timeout",
                                   "events_match": False})
            except Exception as e:
                failed += 1
                marker = "E"
                tx_results.append({"hash": tx.get("hash"), "actual_status": "error",
                                   "events_match": False, "error": str(e)})

            if i % 50 == 0 or i == len(txs_to_replay):
                rate = passed / i
                print(f"    [{i}/{len(txs_to_replay)}]  pass={passed}  fail={failed}"
                      f"  rate={rate:.1%}")

        total     = len(txs_to_replay)
        pass_rate = round(passed / total, 3) if total else None
        saved     = pass_rate is not None and pass_rate >= pass_threshold

        result = {
            "state":            "DONE",
            "address":          address,
            "contract_name":    contract_name,
            "deployed_address": deployed_address,
            "constructor_args": constructor_args,
            "k":                len(txs_to_replay),
            "passed":           passed,
            "failed":           failed,
            "pass_rate":        pass_rate,
            "saved":            saved,
            "tx_results":       tx_results,
        }
        _save_ckpt(contract_dir, result)

        # Salva i risultati del replay in un file separato per non appesantire il checkpoint
        (contract_dir / "replay_result.json").write_text(
            json.dumps({"pass_rate": pass_rate, "passed": passed, "failed": failed,
                        "k": len(txs_to_replay), "txs": tx_results}, indent=2)
        )

        rate_str = f"{pass_rate:.1%}" if pass_rate is not None else "n/a"
        icon     = "✓ PASS" if saved else "✗ FAIL"
        print(f"  → {icon}  pass_rate={rate_str}  ({passed}/{total})")
        return result

    finally:
        server.close()