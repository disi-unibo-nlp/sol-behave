#!/usr/bin/env python3
"""
check_drain_classification.py
Determine WHETHER the issue is classification (model failed to flag renounce/
transfer as terminal) vs scheduling (it was flagged but the sampler emitted
owner-only events after it anyway).

Looks in the DCR generation artifacts for any owner-only / drain-phase / guard
metadata, and checks the synthetic tx ordering for whether owner-only events
were scheduled AFTER a privilege-drop. Run from /workdir/src/coverage_analysis/.

This is exploratory — it prints whatever ordering/guard metadata exists so we
can see what the generator knew at sampling time.
"""
import json
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
DCR_GEN = SRC / "dcr_generation" / "generated_dcr_1to20"

# Use NotFoundSeriesOne — cleanest case (100% before, 0% after, drop at idx 16)
ADDR = "0x82c8c6231e7a4c40d014cb426a49b42863524c88"
d = DCR_GEN / ADDR

print(f"Files in DCR gen dir for NotFoundSeriesOne:")
for f in sorted(d.glob("*")):
    print(f"  {f.name}  ({f.stat().st_size} bytes)")

def load(p):
    try: return json.loads(p.read_text())
    except Exception: return None

# Look at the synthetic txs ordering + any drain/phase metadata
synth = load(d / "transactions_synthetic.json")
if synth:
    print(f"\nTotal synthetic txs: {len(synth)}")
    # Show the fields available on a tx
    print(f"tx fields: {list(synth[0].keys())}")
    # Find renounce/transfer position and what comes after
    for i, t in enumerate(synth):
        fn = (t.get('_fn_sig','') or '').split('(')[0]
        if fn in ('renounceOwnership','transferOwnership'):
            print(f"\nFirst privilege-drop in synthetic FILE order: {fn} at index {i}")
            print(f"  _event_id: {t.get('_event_id')}  _branch_guard: {t.get('_branch_guard')}")
            print(f"  _param_source: {t.get('_param_source')}")
            # what are the next 5 events?
            print(f"  next 5 events after it:")
            for t2 in synth[i+1:i+6]:
                fn2 = (t2.get('_fn_sig','') or '').split('(')[0]
                print(f"    {fn2}  (event={t2.get('_event_id')})")
            break

# Look for sequences_hardhat.json or coverage which may show phase info
for fname in ("sequences_hardhat.json", "coverage.json", "source_dcr.json"):
    data = load(d / fname)
    if data:
        print(f"\n{fname} top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
        # look for anything mentioning owner/drain/phase/terminal
        blob = json.dumps(data)[:2000]
        for kw in ("drain","phase","terminal","owner","renounce"):
            if kw in blob.lower():
                print(f"  mentions '{kw}': yes")