"""
inspect_data.py — run this in /workdir/src/dcr_replay/ to confirm data layout
before writing the comprehensive analysis script.
"""
import json
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent  # /workdir/src

def head(label):
    print(f"\n{'='*60}\n  {label}\n{'='*60}")


SYNTHETIC_REPLAY_RESULTS_GEN = SRC / "dcr_replay" / "synth_replay_results_Qwen3_6-27B-UD-Q4_K_XL_gguf"  #"synth_replay_results_generated"
SYNTHETIC_REPLAY_RESULTS_ORIG = SRC / "dcr_replay" / "original_synth_replay_results"


# ── 1. Historical × original ──────────────────────────────────────────────
head("1. HISTORICAL × ORIGINAL")
hist_summary = SRC / "transaction_replay" / "replay_results.json"
print(f"summary exists: {hist_summary.exists()}  ({hist_summary})")
# Per-contract result with tx_results + state_after
dataset = SRC / "dataset" / "contracts_dataset_1to20"
sample = next(dataset.glob("*/replay_result_1000.json"), None)
if sample:
    d = json.loads(sample.read_text())
    print(f"per-contract sample: {sample.parent.name[:16]}")
    print(f"  top-level keys: {list(d.keys())}")
    txs = d.get("tx_results", [])
    print(f"  n tx_results: {len(txs)}")
    if txs:
        print(f"  tx[0] keys: {list(txs[0].keys())}")
        print(f"  tx[0] has state_after: {'state_after' in txs[0]}")
        print(f"  tx[0] state_after: {txs[0].get('state_after')}")

# ── 2. Synthetic × original ───────────────────────────────────────────────
head("2. SYNTHETIC × ORIGINAL")
synth_orig = SYNTHETIC_REPLAY_RESULTS_ORIG
sample = next(synth_orig.glob("*/synth_replay_result_1000.json"), None)
if sample:
    d = json.loads(sample.read_text())
    print(f"per-contract sample: {sample.parent.name[:16]}")
    print(f"  top-level keys: {list(d.keys())}")
    txs = d.get("tx_results", [])
    print(f"  n tx_results: {len(txs)}")
    if txs:
        print(f"  tx[0] keys: {list(txs[0].keys())}")
        # find a tx with _view_state
        with_vs = [t for t in txs if t.get("_view_state")]
        print(f"  txs with _view_state: {len(with_vs)}")
        if with_vs:
            print(f"  sample _view_state: {with_vs[0]['_view_state']}")

# ── 3. Synthetic × generated ──────────────────────────────────────────────
head("3. SYNTHETIC × GENERATED")
synth_gen = SYNTHETIC_REPLAY_RESULTS_GEN  #"synth_replay_results_generated"
print(f"dir exists: {synth_gen.exists()}  ({synth_gen})")
if synth_gen.exists():
    # sample result file
    sample = next(synth_gen.glob("*/synth_replay_sample_*_result_1000.json"), None)
    if sample:
        d = json.loads(sample.read_text())
        print(f"per-sample file: {sample.parent.name[:16]}/{sample.name}")
        print(f"  top-level keys: {list(d.keys())}")
        txs = d.get("tx_results", [])
        with_vs = [t for t in txs if t.get("_view_state")]
        print(f"  n tx_results: {len(txs)}, with _view_state: {len(with_vs)}")
        if with_vs:
            print(f"  sample _view_state: {with_vs[0]['_view_state']}")
    # summary file
    summ = next(synth_gen.glob("*/synth_replay_contract_summary_1000.json"), None)
    if summ:
        d = json.loads(summ.read_text())
        print(f"contract summary keys: {list(d.keys())}")

# ── 4. DCR coverage ───────────────────────────────────────────────────────
head("4. DCR COVERAGE")
dcr = SRC / "dcr_generation" / "generated_dcr_1to20"
sample = next(dcr.glob("*/coverage.json"), None)
if sample:
    d = json.loads(sample.read_text())
    print(f"coverage sample: {sample.parent.name[:16]}")
    print(f"  top-level keys: {list(d.keys())}")
    print(f"  full content: {json.dumps(d, indent=2)[:800]}")

# ── Counts ────────────────────────────────────────────────────────────────
head("COUNTS")
print(f"historical per-contract results: {len(list(dataset.glob('*/replay_result_1000.json')))}")
print(f"synth-original results:          {len(list(synth_orig.glob('*/synth_replay_result_1000.json')))}")
if synth_gen.exists():
    print(f"synth-generated sample results:  {len(list(synth_gen.glob('*/synth_replay_sample_*_result_1000.json')))}")
    print(f"synth-generated contracts:       {len(list(synth_gen.glob('*/synth_replay_contract_summary_1000.json')))}")
print(f"dcr coverage files:              {len(list(dcr.glob('*/coverage.json')))}")