"""
hardhat_utils.py

Funzioni condivise per la gestione del workspace Hardhat, la configurazione
del compilatore Solidity e l'installazione delle dipendenze npm.

Usato da pipeline.py (compile@k) e deploy_replay.py (fresh deploy + replay).
Le funzioni qui sono la versione autorevole — pipeline.py le importa invece
di avere le proprie implementazioni inferiori.
"""

import re
import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

# ── EVM version map ───────────────────────────────────────────────────────────

_EVM_MAP = {
    "homestead": "homestead", "tangerineWhistle": "tangerineWhistle",
    "spuriousDragon": "spuriousDragon", "byzantium": "byzantium",
    "constantinople": "constantinople", "petersburg": "petersburg",
    "istanbul": "istanbul", "berlin": "berlin", "london": "london",
    "paris": "paris", "shanghai": "paris", "merge": "paris",
    "cancun": "paris", "default": "paris", "": "paris",
}


def evm_version(evm_str: str, compiler_version: str) -> str:
    """
    Restituisce la stringa evmVersion corretta per Hardhat.
    Clamp automatico per versioni di solc che non supportano evm recenti.
    """
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


# ── Pragma extraction ─────────────────────────────────────────────────────────

def extract_pragma_versions(source_texts: list[str]) -> list[str]:
    """
    Estrae tutte le versioni Solidity distinte da una lista di sorgenti.
    Ogni versione è normalizzata alla forma X.Y.Z usabile in hardhat config.
    """
    versions: list[str] = []
    seen: set[str] = set()

    for text in source_texts:
        for m in re.finditer(r'pragma\s+solidity\s+([^;]+);', text):
            spec = m.group(1).strip()
            # Versione esatta: =0.8.17 o 0.8.17
            exact = re.search(r'(?:^|=)(\d+\.\d+\.\d+)', spec)
            if exact:
                v = exact.group(1)
                if v not in seen:
                    seen.add(v); versions.append(v)
                continue
            # Range: ^0.8.0, >=0.8.0, ~0.8.0 → versione minima
            lower = re.search(r'[\^~>=]+\s*(\d+\.\d+\.\d+)', spec)
            if lower:
                v = lower.group(1)
                if v not in seen:
                    seen.add(v); versions.append(v)
                continue
            # Solo major.minor: 0.8
            mm = re.search(r'(\d+\.\d+)', spec)
            if mm:
                v = mm.group(1) + ".0"
                if v not in seen:
                    seen.add(v); versions.append(v)
    return versions


def primary_compiler_version(meta: dict, source_texts: list[str]) -> str:
    """
    Determina la versione del compiler da usare:
    1. Dal metadata Etherscan (più affidabile — è quella usata originalmente)
    2. Dal pragma del sorgente principale
    3. Fallback a 0.8.20
    """
    meta_ver = meta.get("compiler_version", "").lstrip("v").split("+")[0].strip()
    if meta_ver and re.match(r'^\d+\.\d+\.\d+$', meta_ver):
        return meta_ver
    pragmas = extract_pragma_versions(source_texts)
    if pragmas:
        return pragmas[0]
    return "0.8.20"


# ── Hardhat config ────────────────────────────────────────────────────────────

def hardhat_config(meta: dict, source_texts: list[str] | None = None,
                   remappings: list[str] | None = None) -> str:
    """
    Genera hardhat.config.js.

    - Usa la versione del compiler dal metadata come versione primaria.
    - Aggiunge versioni extra se i file di supporto hanno pragma diverse
      (es. OZ con ^0.8.0 su un contratto principale con 0.8.4).
    - Gestisce remapping per librerie con path non standard.
    """
    source_texts = source_texts or []
    ver = primary_compiler_version(meta, source_texts)

    major, minor, *patch_parts = ver.split(".")
    patch = int(patch_parts[0]) if patch_parts else 0
    is_old = int(major) == 0 and int(minor) < 6

    opt  = str(meta.get("optimization_used", False)).lower()
    runs = int(meta.get("runs") or 200)
    evm  = evm_version(meta.get("evm_version", ""), ver)

    def _compiler_entry(v: str) -> str:
        _maj, _min, *_p = v.split(".")
        _patch = int(_p[0]) if _p else 0
        _old = int(_maj) == 0 and int(_min) < 6
        _evm_line = "" if _old else f'          evmVersion: "{evm_version(meta.get("evm_version", ""), v)}",\n'
        _remap_line = ""
        if remappings:
            rj = "[" + ", ".join(f'"{r}"' for r in remappings) + "]"
            _remap_line = f'          remappings: {rj},\n'
        return (
            f'    {{\n'
            f'      version: "{v}",\n'
            f'      settings: {{\n'
            f'        optimizer: {{ enabled: {opt}, runs: {runs} }},\n'
            + _evm_line + _remap_line +
            f'      }}\n'
            f'    }}'
        )

    # Versione primaria
    compilers = [_compiler_entry(ver)]

    # Versioni extra dai pragma dei file di supporto
    # (es. OZ usa ^0.8.0 ma il contratto principale ha 0.8.4 — Hardhat
    # con un solo compiler 0.8.4 riesce a compilare ^0.8.0 perché 0.8.4 >= 0.8.0,
    # ma se ci sono file con pragma 0.8.1 esatto serve un compiler 0.8.1 separato)
    if source_texts:
        extra_versions = extract_pragma_versions(source_texts)
        for ev in extra_versions:
            if ev != ver and re.match(r'^\d+\.\d+\.\d+$', ev):
                # Aggiungi solo versioni esatte diverse dalla primaria
                # (i range come ^0.8.0 vengono soddisfatti dal compiler primario)
                compilers.append(_compiler_entry(ev))

    compilers_js = ",\n".join(compilers)

    return (
        'require("@nomicfoundation/hardhat-toolbox");\n\n'
        'module.exports = {\n'
        '  networks: {\n'
        '    hardhat: {\n'
        '      accounts: { accountsBalance: "10000000000000000000000" },\n'
        '      allowUnlimitedContractSize: true,\n'
        '    },\n'
        '  },\n'
        '  solidity: {\n'
        f'    compilers: [\n{compilers_js}\n'
        '    ]\n'
        '  }\n'
        '};\n'
    )


# ── Workspace setup ───────────────────────────────────────────────────────────

def setup_workspace(work_dir: Path) -> None:
    """Crea il workspace Hardhat con package.json e installa i tool di base."""
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
        print("  Installing Hardhat (one-time) ...")
        subprocess.run(["npm", "install"], cwd=work_dir, check=True,
                       capture_output=True)


def write_sources(work_dir: Path, source_texts: dict[str, str]) -> None:
    """
    Scrive i sorgenti nel workspace.
    `source_texts` è un dict {filepath: content} estratto da source.json,
    oppure {contract_name.sol: content} per sorgenti single-file.
    """
    contracts_dir = work_dir / "contracts"
    if contracts_dir.exists():
        shutil.rmtree(contracts_dir)
    contracts_dir.mkdir(parents=True)
    for fp, content in source_texts.items():
        dest = contracts_dir / fp
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)


def _oz_version_for_compiler(compiler_version: str) -> str:
    """
    Determina la versione OZ compatibile con il compiler.
    OZ 5.x richiede solc >= 0.8.20.
    OZ 4.x funziona con solc 0.8.x < 0.8.20.
    OZ 2.x/3.x per solc < 0.8.
    """
    try:
        ver   = compiler_version.lstrip("v").split("+")[0]
        parts = ver.split(".")
        major, minor = int(parts[0]), int(parts[1])
        patch = int(parts[2]) if len(parts) > 2 else 0
        if major == 0:
            if minor < 6:
                return "@2"    # SafeMath era in OZ 2.x per Solidity <0.6
            elif minor < 8:
                return "@3"
            elif minor == 8 and patch < 20:
                return "@4"
    except Exception:
        pass
    return ""   # latest (v5 per solc >= 0.8.20)


def install_npm_deps(work_dir: Path, source_texts: list[str],
                     compiler_version: str = "") -> None:
    """Installa le dipendenze npm rilevate dagli import nei sorgenti."""
    raw: set[str] = set()
    pat = re.compile(r'''import\s+(?:[^"']*\s+from\s+)?["']([^"']+)["']''', re.MULTILINE)
    for text in source_texts:
        for m in pat.finditer(text):
            imp = m.group(1).strip()
            if not imp.startswith(".") and not imp.startswith("/"):
                raw.add(imp)

    packages: set[str] = {
        "/".join(i.split("/")[:2]) if i.startswith("@") else i.split("/")[0]
        for i in raw
    }
    _SKIP = {"permit2", "ds-test", "forge-std"}

    oz_ver = _oz_version_for_compiler(compiler_version) if compiler_version else ""

    _REMAP = {
        "@openzeppelin/contracts":             f"@openzeppelin/contracts{oz_ver}",
        "@openzeppelin/contracts-upgradeable": f"@openzeppelin/contracts-upgradeable{oz_ver}",
        "openzeppelin-contracts":              f"@openzeppelin/contracts{oz_ver}",
        "@uniswap/v2-core":                   "@uniswap/v2-core",
        "@uniswap/v2-periphery":              "@uniswap/v2-periphery",
        "@uniswap/v3-core":                   "@uniswap/v3-core",
        "@uniswap/v3-periphery":              "@uniswap/v3-periphery",
        "solmate":                             "solmate",
        "@rari-capital/solmate":              "@rari-capital/solmate",
        "erc721a":                             "erc721a",
    }
    nm = work_dir / "node_modules"
    to_install = []
    for p in packages:
        if p in _SKIP:
            continue
        resolved = _REMAP.get(p, p)
        # Calcola il path base in node_modules (senza @version suffix)
        if resolved.startswith("@"):
            pkg_base = "@" + resolved.lstrip("@").split("@")[0]
        else:
            pkg_base = resolved.split("@")[0]
        pkg_dir = nm / pkg_base

        if not pkg_dir.exists():
            to_install.append(resolved)
        elif oz_ver and p in ("@openzeppelin/contracts",
                               "@openzeppelin/contracts-upgradeable",
                               "openzeppelin-contracts"):
            # Verifica major version installata
            pkg_json = pkg_dir / "package.json"
            if pkg_json.exists():
                try:
                    inst = json.loads(pkg_json.read_text()).get("version", "")
                    inst_major = int(inst.split(".")[0])
                    need_major = int(oz_ver.lstrip("@"))
                    if inst_major != need_major:
                        to_install.append(resolved)
                except Exception:
                    pass

    to_install = list(dict.fromkeys(to_install))
    if to_install:
        subprocess.run(
            ["npm", "install", "--save-dev"] + to_install,
            cwd=work_dir, capture_output=True, text=True,
        )


def detect_remappings(work_dir: Path, source_texts: list[str]) -> list[str]:
    """
    Rileva i remapping necessari per librerie con path non standard
    (es. solmate che usa `solmate/src/` ma viene importato come `solmate/`).
    """
    nm = work_dir / "node_modules"
    imports: set[str] = set()
    for text in source_texts:
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


def load_source_texts(source_path: Path) -> dict[str, str]:
    """
    Carica i sorgenti da source.sol o source.json.
    Ritorna {filepath: content} — per single-file il filepath è il nome del file.
    """
    if source_path.suffix == ".sol":
        return {source_path.name: source_path.read_text()}
    data = json.loads(source_path.read_text())
    return {
        fp: fc.get("content", "")
        for fp, fc in data.get("sources", {}).items()
    }

def prepare_and_compile(
    source_path: Path,
    meta: dict,
    work_dir: Path,
) -> tuple[bool, str]:
    sources   = load_source_texts(source_path)
    all_texts = list(sources.values())

    # Pulisci artifacts e cache prima di ogni compilazione
    # per evitare HH700 da removeObsoleteArtifacts su run precedenti
    for stale_dir in ["artifacts", "cache"]:
        stale = work_dir / stale_dir
        if stale.exists():
            shutil.rmtree(stale)

    contracts_dir = work_dir / "contracts"
    if contracts_dir.exists():
        shutil.rmtree(contracts_dir)
    contracts_dir.mkdir(parents=True)

    if source_path.suffix == ".sol":
        cname    = meta.get("contract_name", "contract")
        new_name = f"{cname}.sol"
        sources  = {new_name: list(sources.values())[0]}

    for fp, content in sources.items():
        dest = contracts_dir / fp
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)

    install_npm_deps(work_dir, all_texts,
                     compiler_version=meta.get("compiler_version", ""))
    remaps = detect_remappings(work_dir, all_texts)
    cfg    = hardhat_config(meta, source_texts=all_texts, remappings=remaps or None)
    (work_dir / "hardhat.config.js").write_text(cfg)

    cp = subprocess.run(
        ["npx", "hardhat", "compile", "--force"],
        cwd=work_dir, capture_output=True, text=True,
    )
    ok  = cp.returncode == 0
    err = cp.stderr if not ok else ""
    return ok, err



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
    if not isinstance(t, str):
        return ""
    
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
