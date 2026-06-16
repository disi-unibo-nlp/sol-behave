"""
probe_join_keys.py
Confirm the keys needed to align original vs generated txs for state divergence.
For divergence we compare state_after at the SAME transaction between the
original replay and each generated sample replay. The txs must be in the same
order (they are — same filtered tx list, same k). We verify by checking that
tx hashes line up between original and a generated sample for one contract.

Run from /workdir/src/dcr_replay/.
"""
import json
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent

# ── SYNTHETIC side ────────────────────────────────────────────────────────
synth_orig = SRC / "dcr_replay" / "synth_replay_results"
synth_gen  = SRC / "dcr_replay" / "synth_replay_results_generated"

# Pick a contract present in both
orig_files = {p.parent.name: p for p in synth_orig.glob("*/synth_replay_result_1000.json")}
gen_dirs   = {p.name for p in synth_gen.iterdir() if p.is_dir()}
common = sorted(set(orig_files) & gen_dirs)
print(f"SYNTHETIC: {len(common)} contracts in both original and generated\n")

if common:
    addr = common[0]
    od = json.loads(orig_files[addr].read_text())
    o_txs = od.get("tx_results", [])
    # find a generated sample for this addr
    gsample = next((synth_gen / addr).glob("synth_replay_sample_*_result_1000.json"), None)
    if gsample:
        gd = json.loads(gsample.read_text())
        g_txs = gd.get("tx_results", [])
        print(f"contract {addr[:16]} ({od.get('contract_name')})")
        print(f"  original txs : {len(o_txs)}")
        print(f"  generated txs: {len(g_txs)}  (sample {gd.get('sample_idx')})")
        print(f"  hashes align : {[t.get('hash') for t in o_txs[:3]] == [t.get('hash') for t in g_txs[:3]]}")
        # show a state_after pair at tx 0
        print(f"\n  original  tx0 state_after: {o_txs[0].get('state_after')}")
        print(f"  generated tx0 state_after: {g_txs[0].get('state_after')}")
        # show keys present
        print(f"\n  original tx keys: {[k for k in o_txs[0].keys() if not k.startswith('_')][:8]}")
        print(f"  has _fn_sig: {'_fn_sig' in o_txs[0]}, has _event_id: {'_event_id' in o_txs[0]}")

# ── HISTORICAL side ───────────────────────────────────────────────────────
print("\n" + "="*60)
dataset = SRC / "dataset" / "contracts_dataset_1to20"
gen     = SRC / "source_generation" / "repaired_oneshot"

hist_orig = {p.parent.name: p for p in dataset.glob("*/replay_result_1000.json")}
common_h = []
for addr in hist_orig:
    if list((gen / addr).glob("replay_sample_*_result_1000.json")):
        common_h.append(addr)
print(f"HISTORICAL: {len(common_h)} contracts in both original and generated\n")

if common_h:
    addr = sorted(common_h)[0]
    od = json.loads(hist_orig[addr].read_text())
    o_txs = od.get("tx_results", [])
    gsample = next((gen / addr).glob("replay_sample_*_result_1000.json"), None)
    if gsample:
        gd = json.loads(gsample.read_text())
        g_txs = gd.get("tx_results", [])
        print(f"contract {addr[:16]} ({od.get('contract_name')})")
        print(f"  original txs : {len(o_txs)}")
        print(f"  generated txs: {len(g_txs)}  (sample {gd.get('sample_idx')})")
        print(f"  hashes align : {[t.get('hash') for t in o_txs[:3]] == [t.get('hash') for t in g_txs[:3]]}")
        print(f"\n  original  tx0 state_after: {o_txs[0].get('state_after')}")
        print(f"  generated tx0 state_after: {g_txs[0].get('state_after')}")
        print(f"\n  original tx has _fn_sig: {'_fn_sig' in o_txs[0]}")