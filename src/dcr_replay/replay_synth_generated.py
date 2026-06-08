"""
replay_synth_generated.py
=========================

Replay synthetic transactions (from make_sequences_hardhat.py) against
LLM-generated (repaired) contract samples.

Relationship to other scripts
------------------------------
  replay_transactions.py      historical txs × original contracts
  replay_generated.py         historical txs × generated samples
  replay_synth_transactions.py  synthetic txs × original contracts   ← sibling
  replay_synth_generated.py     synthetic txs × generated samples    ← this file

The synthetic transactions provide better function coverage than historical
ones (they are DCR-guided), so comparing pass rates here reveals whether
generated implementations are correct on the full reachable call space,
not just the historical happy-path inputs.

Data sources
------------
  <dataset_dir>/<address>/         metadata, ABI, source, deploy_params.json
  <dcr_dir>/<address>/             transactions_synthetic.json
  <generated_dir>/<address>/       sample_N.(sol|json), result.json

Output
------
  <generated_dir>/<address>/
    synth_replay_sample_{N}_checkpoint_{K}.json
    synth_replay_sample_{N}_result_{K}.json
    synth_replay_contract_summary_{K}.json

Usage
-----
  python replay_synth_generated.py [options]

  # Default paths:
  python replay_synth_generated.py

  # Custom:
  python replay_synth_generated.py \\
      --dataset    ../dataset/contracts_dataset_1to20 \\
      --dcr-dir    ../dcr_generation/generated_dcr_1to20 \\
      --generated  ../source_generation/repaired_oneshot \\
      --k 200

  # Filter to contracts that were evaluated in the main pipeline:
  python replay_synth_generated.py \\
      --from-results ../transaction_replay/generated_replay_results.json
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR / ".." / "utils"))

from deploy_replay import (
    DeployServer,
    _default_args,
    setup_workspace,
)
from hardhat_utils import prepare_and_compile
from replay_transactions import build_view_selectors, _format_elapsed
from replay_generated import (
    _compiled_sample_indices,
    _find_sample_path,
)
from replay_synth_transactions import (
    load_synth_data,
    filter_synth_txs,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_DATASET   = str(_SCRIPT_DIR / ".." / "dataset" / "contracts_dataset_1to20")
DEFAULT_DCR_DIR   = str(_SCRIPT_DIR / ".." / "dcr_generation" / "generated_dcr_1to20")
DEFAULT_GENERATED = str(_SCRIPT_DIR / ".." / "source_generation" / "repaired_oneshot")
DEFAULT_WORK_DIR  = str(_SCRIPT_DIR / ".." / "dataset" / "hardhat_workspace_replay")
DEFAULT_K         = 200
DEFAULT_THRESHOLD = 0.50
DEFAULT_TX_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Checkpoint helpers (keyed on sample index + K, prefixed "synth_")
# ---------------------------------------------------------------------------

def _sample_ckpt_path(gen_contract_dir: Path, sample_idx: int, k: int) -> Path:
    return gen_contract_dir / f"synth_replay_sample_{sample_idx}_checkpoint_{k}.json"


def _sample_result_path(gen_contract_dir: Path, sample_idx: int, k: int) -> Path:
    return gen_contract_dir / f"synth_replay_sample_{sample_idx}_result_{k}.json"


def _load_sample_ckpt(gen_contract_dir: Path, sample_idx: int, k: int) -> dict:
    p = _sample_ckpt_path(gen_contract_dir, sample_idx, k)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"state": "NOT_STARTED"}


def _save_sample_ckpt(gen_contract_dir: Path, sample_idx: int, k: int,
                      data: dict) -> None:
    _sample_ckpt_path(gen_contract_dir, sample_idx, k).write_text(
        json.dumps(data, indent=2)
    )


# ---------------------------------------------------------------------------
# Sample source loading (mirrors replay_generated._load_flat_sample)
# ---------------------------------------------------------------------------

def _load_flat_sample(sample_path: Path, contract_name: str) -> str:
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
# Per-sample replay
# ---------------------------------------------------------------------------

def replay_synth_sample(
    address:          str,
    sample_idx:       int,
    sample_path:      Path,
    dataset_dir:      Path,
    dcr_dir:          Path,
    gen_contract_dir: Path,
    work_dir:         Path,
    k:                int,
    tx_timeout:       int,
    force:            bool = False,
    retry_failed:     bool = False,
    verbose:          bool = False,
) -> dict:
    """Replay synthetic txs against one generated sample."""
    ckpt  = _load_sample_ckpt(gen_contract_dir, sample_idx, k)
    state = ckpt.get("state")
    if state in ("DONE", "FAILED"):
        if force or (retry_failed and state == "FAILED"):
            pass
        else:
            return ckpt

    # Load synthetic tx data (same transactions used in replay_synth_transactions)
    data = load_synth_data(dataset_dir, dcr_dir, address)
    if data is None:
        result = {
            "state": "FAILED", "address": address, "sample_idx": sample_idx,
            "reason": "missing dataset/synth files",
        }
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
        return result

    meta             = data["meta"]
    abi              = data["abi"]
    constructor_args = data["constructor_args"]
    transactions     = data["transactions"]
    contract_name    = meta.get("contract_name", "Contract")

    if meta.get("is_proxy"):
        result = {"state": "FAILED", "address": address, "sample_idx": sample_idx,
                  "reason": "proxy — skipped"}
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
        return result

    txs_to_replay = filter_synth_txs(transactions, address, k)
    if not txs_to_replay:
        result = {"state": "FAILED", "address": address, "sample_idx": sample_idx,
                  "reason": "no synthetic transactions"}
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
        return result

    # Load generated source
    generated_source = _load_flat_sample(sample_path, contract_name)
    if not generated_source:
        result = {"state": "FAILED", "address": address, "sample_idx": sample_idx,
                  "reason": "could not load generated sample source"}
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
        return result

    # Compile the generated sample (need a temp source path for prepare_and_compile)
    import tempfile, shutil
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / f"{contract_name}.sol"
        tmp_path.write_text(generated_source)
        print(f"    sample {sample_idx}: compiling ...", end="  ", flush=True)
        ok, err = prepare_and_compile(tmp_path, meta, work_dir)

    if not ok:
        print("✗")
        if verbose:
            print(f"      {err[-300:]}")
        result = {
            "state": "FAILED", "address": address, "sample_idx": sample_idx,
            "reason": "compilation failed", "stderr": err[-200:],
        }
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
        return result
    print("✓")

    # Build FQN for the generated contract
    if sample_path.suffix == ".sol":
        fqn = f"contracts/{contract_name}.sol:{contract_name}"
    else:
        try:
            sources_map = json.loads(sample_path.read_text()).get("sources", {})
            first_file  = next(iter(sources_map), None)
            fqn = f"contracts/{first_file}:{contract_name}" if first_file else contract_name
        except Exception:
            fqn = contract_name

    view_selectors = build_view_selectors(abi)
    server = DeployServer(work_dir, tx_timeout=tx_timeout)
    try:
        print(f"    sample {sample_idx}: deploying ...", end="  ", flush=True)
        try:
            deployed_address = server.deploy(fqn, constructor_args,
                                             view_selectors=view_selectors)
        except RuntimeError as e:
            print(f"✗  {e}")
            ctor_entry = next((e for e in abi if e.get("type") == "constructor"), None)
            fallback   = _default_args(ctor_entry.get("inputs", []) if ctor_entry else [])
            if fallback != constructor_args:
                try:
                    deployed_address = server.deploy(fqn, fallback,
                                                     view_selectors=view_selectors)
                    constructor_args = fallback
                except RuntimeError as e2:
                    server.close()
                    result = {"state": "FAILED", "address": address,
                              "sample_idx": sample_idx, "reason": f"deploy failed: {e2}"}
                    _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
                    return result
            else:
                server.close()
                result = {"state": "FAILED", "address": address,
                          "sample_idx": sample_idx, "reason": f"deploy failed: {e}"}
                _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
                return result

        print(f"✓  {deployed_address[:14]}…")

        # ── Replay ─────────────────────────────────────────────────────────
        tx_results = []
        passed = 0
        failed = 0

        for i, tx in enumerate(txs_to_replay, 1):
            tx_hash = tx.get("hash", "")
            try:
                res   = server.replay_tx(tx)
                match = res.get("events_match", False)
                if match:
                    passed += 1
                else:
                    failed += 1
                    if verbose:
                        reason   = (res.get("revert_reason") or res.get("error") or "")[:80]
                        event_id = tx.get("_event_id", "")
                        print(f"      tx {i} [{event_id}] mismatch: {reason}")

                res["_event_id"]       = tx.get("_event_id", "")
                res["_branch_guard"]   = tx.get("_branch_guard", "")
                res["_fn_sig"]         = tx.get("_fn_sig", "")
                res["_param_source"]   = tx.get("_param_source", "")
                res["_decoded_params"] = tx.get("_decoded_params", {})
                tx_results.append(res)

            except TimeoutError:
                failed += 1
                tx_results.append({
                    "hash": tx_hash, "actual_status": "timeout",
                    "events_match": False,
                    "_event_id": tx.get("_event_id", ""),
                    "_fn_sig":   tx.get("_fn_sig", ""),
                })
            except Exception as e:
                failed += 1
                tx_results.append({
                    "hash": tx_hash, "actual_status": "error",
                    "events_match": False, "error": str(e),
                    "_event_id": tx.get("_event_id", ""),
                    "_fn_sig":   tx.get("_fn_sig", ""),
                })

            if i % 100 == 0 or i == len(txs_to_replay):
                print(f"      [{i:>5}/{len(txs_to_replay)}]  "
                      f"pass={passed}  fail={failed}  rate={passed/i:.1%}")

        total     = len(txs_to_replay)
        pass_rate = round(passed / total, 4) if total else None

        # Per-function pass rates
        fn_pass: dict[str, list[bool]] = {}
        for r in tx_results:
            fn_sig = r.get("_fn_sig", "").split("(")[0]
            if fn_sig:
                fn_pass.setdefault(fn_sig, []).append(r.get("events_match", False))
        fn_pass_rates = {fn: round(sum(v)/len(v), 4) for fn, v in fn_pass.items()}

        gas_ratios = [r["gas_ratio"] for r in tx_results
                      if r.get("gas_ratio") is not None]

        result = {
            "state":            "DONE",
            "address":          address,
            "sample_idx":       sample_idx,
            "contract_name":    contract_name,
            "deployed_address": deployed_address,
            "k":                total,
            "passed":           passed,
            "failed":           failed,
            "pass_rate":        pass_rate,
            "fn_pass_rates":    fn_pass_rates,
            "n_events_covered": len({r["_event_id"] for r in tx_results
                                     if r.get("_event_id")}),
            "mean_gas_ratio":   round(sum(gas_ratios)/len(gas_ratios), 4)
                                if gas_ratios else None,
        }
        _save_sample_ckpt(gen_contract_dir, sample_idx, k, result)
        _sample_result_path(gen_contract_dir, sample_idx, k).write_text(json.dumps({
            **result,
            "timestamp":  datetime.utcnow().isoformat(),
            "tx_results": tx_results,
        }, indent=2))

        rate_str = f"{pass_rate:.1%}" if pass_rate is not None else "n/a"
        print(f"    sample {sample_idx}: pass_rate={rate_str}  ({passed}/{total})")
        return result

    finally:
        server.close()


# ---------------------------------------------------------------------------
# Per-contract orchestration
# ---------------------------------------------------------------------------

def replay_synth_contract_samples(
    address:      str,
    dataset_dir:  Path,
    dcr_dir:      Path,
    gen_dir:      Path,
    work_dir:     Path,
    k:            int,
    tx_timeout:   int,
    force:        bool = False,
    retry_failed: bool = False,
    verbose:      bool = False,
) -> dict:
    gen_contract_dir = gen_dir / address
    compiled_indices = _compiled_sample_indices(gen_contract_dir)

    if not compiled_indices:
        return {
            "address": address, "skipped": True,
            "reason": "no compiled samples", "samples": [],
        }

    meta_path     = dataset_dir / address / "metadata.json"
    contract_name = address[:12] + "…"
    if meta_path.exists():
        try:
            contract_name = json.loads(meta_path.read_text()).get(
                "contract_name", contract_name)
        except Exception:
            pass

    # Check synthetic txs exist
    synth_path = dcr_dir / address / "transactions_synthetic.json"
    if not synth_path.exists():
        return {
            "address": address, "skipped": True,
            "reason": "no transactions_synthetic.json in dcr_dir", "samples": [],
        }

    print(f"  {contract_name} ({address[:12]}…)  "
          f"compiled_samples={len(compiled_indices)}")

    sample_results = []
    for sample_idx in compiled_indices:
        sample_path = _find_sample_path(gen_contract_dir, sample_idx)
        if sample_path is None:
            sample_results.append({
                "sample_idx": sample_idx, "state": "FAILED",
                "reason": "sample file not found",
            })
            continue

        result = replay_synth_sample(
            address          = address,
            sample_idx       = sample_idx,
            sample_path      = sample_path,
            dataset_dir      = dataset_dir,
            dcr_dir          = dcr_dir,
            gen_contract_dir = gen_contract_dir,
            work_dir         = work_dir,
            k                = k,
            tx_timeout       = tx_timeout,
            force            = force,
            retry_failed     = retry_failed,
            verbose          = verbose,
        )
        sample_results.append(result)

    done_results = [r for r in sample_results if r.get("state") == "DONE"]
    rates        = [r["pass_rate"] for r in done_results
                    if r.get("pass_rate") is not None]

    def _mean(vals): return round(sum(vals)/len(vals), 4) if vals else None

    # Aggregate fn_pass_rates across samples
    all_fn_rates: dict[str, list[float]] = {}
    for r in done_results:
        for fn, rate in r.get("fn_pass_rates", {}).items():
            all_fn_rates.setdefault(fn, []).append(rate)
    mean_fn_pass_rates = {fn: _mean(v) for fn, v in all_fn_rates.items()}

    summary = {
        "address":             address,
        "contract_name":       contract_name,
        "compiled_samples":    len(compiled_indices),
        "replayed":            len(done_results),
        "failed_replay":       len(sample_results) - len(done_results),
        "pass_rates":          rates,
        "mean_pass_rate":      _mean(rates),
        "min_pass_rate":       round(min(rates), 4) if rates else None,
        "max_pass_rate":       round(max(rates), 4) if rates else None,
        "mean_fn_pass_rates":  mean_fn_pass_rates,
        "samples":             sample_results,
    }

    (gen_contract_dir / f"synth_replay_contract_summary_{k}.json").write_text(
        json.dumps(summary, indent=2)
    )
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Replay synthetic DCR transactions against generated samples.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--dataset",    type=Path, default=Path(DEFAULT_DATASET))
    p.add_argument("--dcr-dir",    type=Path, default=Path(DEFAULT_DCR_DIR),
                   dest="dcr_dir")
    p.add_argument("--generated",  type=Path, default=Path(DEFAULT_GENERATED))
    p.add_argument("--work-dir",   type=Path, default=Path(DEFAULT_WORK_DIR),
                   dest="work_dir")
    p.add_argument("--k",          type=int,  default=DEFAULT_K)
    p.add_argument("--tx-timeout", type=int,  default=DEFAULT_TX_TIMEOUT,
                   dest="tx_timeout")
    p.add_argument("--addresses",  nargs="*", metavar="ADDR")
    p.add_argument("--from-results", type=Path, dest="from_results", default=None,
                   metavar="JSON",
                   help="Filter to addresses in a generated_replay_results.json "
                        "(replayed>0) or replay_results.json (state=DONE).")
    p.add_argument("--force",      action="store_true")
    p.add_argument("--retry-failed", action="store_true", dest="retry_failed")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--results-file", type=Path,
                   default=_SCRIPT_DIR / "synth_generated_replay_results.json",
                   dest="results_file")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    for d, label in [(args.dataset, "--dataset"), (args.dcr_dir, "--dcr-dir"),
                     (args.generated, "--generated")]:
        if not d.exists():
            print(f"ERROR: {label} not found: {d}")
            sys.exit(1)

    # Address filter from --from-results
    requested: Optional[set] = None
    if args.from_results:
        if not args.from_results.exists():
            print(f"ERROR: --from-results not found: {args.from_results}")
            sys.exit(1)
        data = json.loads(args.from_results.read_text())
        if "results" in data:
            addrs = {r["address"].lower() for r in data["results"]
                     if r.get("state") == "DONE"}
        else:
            addrs = {c["address"].lower() for c in data.get("contracts", [])
                     if c.get("replayed", 0) > 0}
        print(f"  Loaded {len(addrs)} addresses from {args.from_results.name}")
        requested = addrs

    if args.addresses:
        extra = {a.lower() for a in args.addresses}
        requested = (requested & extra) if requested else extra

    # Discover: contracts with generated samples AND synthetic txs
    gen_dirs = sorted([
        d for d in args.generated.iterdir()
        if d.is_dir()
        and any(re.match(r"sample_\d+\.(sol|json)$", f.name) for f in d.iterdir())
        and (args.dataset / d.name).exists()
        and (args.dcr_dir / d.name / "transactions_synthetic.json").exists()
        and (requested is None or d.name.lower() in requested)
    ])

    if not gen_dirs:
        print("No contracts found with both generated samples and synthetic txs.")
        print(f"  generated: {args.generated}")
        print(f"  dcr_dir:   {args.dcr_dir}")
        sys.exit(0)

    print(f"""
replay_synth_generated.py
══════════════════════════════════════════════════════════
  dataset         = {args.dataset}
  dcr_dir         = {args.dcr_dir}
  generated       = {args.generated}
  work_dir        = {args.work_dir}
  k               = {args.k}
  contracts found = {len(gen_dirs)}
  force           = {args.force}
══════════════════════════════════════════════════════════
""")

    setup_workspace(args.work_dir)

    all_summaries: list[dict] = []
    start_time = time.time()

    try:
        for i, gen_contract_dir in enumerate(gen_dirs, 1):
            address = gen_contract_dir.name
            print(f"\n[{i}/{len(gen_dirs)}] {address}")
            try:
                summary = replay_synth_contract_samples(
                    address      = address,
                    dataset_dir  = args.dataset,
                    dcr_dir      = args.dcr_dir,
                    gen_dir      = args.generated,
                    work_dir     = args.work_dir,
                    k            = args.k,
                    tx_timeout   = args.tx_timeout,
                    force        = args.force,
                    retry_failed = args.retry_failed,
                    verbose      = args.verbose,
                )
                all_summaries.append(summary)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  Unexpected error: {e}")
                all_summaries.append({
                    "address": address, "skipped": True,
                    "reason": f"unexpected error: {e}", "samples": [],
                })
    except KeyboardInterrupt:
        print("\nInterrupted — saving partial results ...")

    elapsed = time.time() - start_time

    all_rates = [r for s in all_summaries for r in s.get("pass_rates", [])]
    mean_rates = [s["mean_pass_rate"] for s in all_summaries
                  if s.get("mean_pass_rate") is not None]

    args.results_file.write_text(json.dumps({
        "timestamp":        datetime.utcnow().isoformat(),
        "duration_seconds": round(elapsed, 1),
        "config": {"k": args.k, "dataset": str(args.dataset),
                   "dcr_dir": str(args.dcr_dir), "generated": str(args.generated)},
        "total_contracts":        len(all_summaries),
        "contracts_replayed":     sum(1 for s in all_summaries if s.get("replayed", 0) > 0),
        "total_samples_replayed": sum(s.get("replayed", 0) for s in all_summaries),
        "mean_pass_rate":         round(sum(all_rates)/len(all_rates), 4)
                                  if all_rates else None,
        "contracts": all_summaries,
    }, indent=2))
    print(f"\nResults written to {args.results_file}")

    # Summary
    done = [s for s in all_summaries if s.get("replayed", 0) > 0]
    print(f"\n{'═'*60}")
    print(f"  SYNTH GENERATED REPLAY SUMMARY  (K={args.k})")
    print(f"{'═'*60}")
    print(f"  Contracts processed      : {len(all_summaries)}")
    print(f"  With replays             : {len(done)}")
    print(f"  Total samples replayed   : {sum(s.get('replayed',0) for s in done)}")
    if all_rates:
        print(f"  Mean pass rate (samples) : {sum(all_rates)/len(all_rates):.1%}")
    if mean_rates:
        print(f"  Mean pass rate (per ctrt): {sum(mean_rates)/len(mean_rates):.1%}")
    print(f"  Total time               : {_format_elapsed(elapsed)}")
    print(f"{'═'*60}")


if __name__ == "__main__":
    main()