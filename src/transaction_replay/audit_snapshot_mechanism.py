"""
audit_snapshot_mechanisms.py
Audit all four replay scripts to see how each handles snapshots, so we can
uniform them. Checks:
  - does it call build_view_selectors?
  - does it pass view_selectors to server.deploy()?
  - does it use the Python-side {"snapshot": True} path (every N txs)?
  - does it rely on JS-side state_after (every tx)?
Run from /workdir/src/transaction_replay/ (or wherever the scripts are).
"""
from pathlib import Path

SCRIPTS = {
    "historical × original":  "replay_transactions.py",
    "historical × generated": "replay_generated.py",
    "synthetic × original":   "../dcr_replay/replay_synth_transactions.py",
    "synthetic × generated":  "../dcr_replay/replay_synth_generated.py",
}

# fall back: some repos keep all four in transaction_replay/
ALT = {
    "synthetic × original":   "replay_synth_transactions.py",
    "synthetic × generated":  "replay_synth_generated.py",
}

def find(path_str):
    p = Path(path_str)
    if p.exists():
        return p
    # try alt location
    name = Path(path_str).name
    alt = Path(name)
    if alt.exists():
        return alt
    return None

print(f"{'replay':<26} {'view_sel':>8} {'deploy(vs)':>11} {'snap_msg':>9} {'state_after':>12}")
print("-" * 70)

for label, path_str in SCRIPTS.items():
    p = find(path_str)
    if p is None:
        print(f"{label:<26} {'NOT FOUND':>8}")
        continue
    src = p.read_text()
    builds_vs   = "build_view_selectors" in src
    deploy_vs   = "view_selectors=view_selectors" in src
    snap_msg    = '{"snapshot": True}' in src or "'snapshot': True" in src or '"snapshot": True' in src
    state_after = "_read_view_state" in src or "snapshot" in src.lower()
    print(f"{label:<26} {str(builds_vs):>8} {str(deploy_vs):>11} {str(snap_msg):>9} {str(state_after):>12}")

print()
print("Legend:")
print("  view_sel    : builds view selectors from ABI")
print("  deploy(vs)  : passes view_selectors to server.deploy()  ← REQUIRED for JS snapshots")
print("  snap_msg    : uses Python-side {'snapshot':True} every N txs  ← the fast path")
print("  state_after : has snapshot logic of some kind")