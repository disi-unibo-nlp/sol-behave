"""
fix_snapshot_calls.py
=====================
Two fixes for replay_synth_transactions.py and replay_synth_generated.py:

1. _read_view_state sends {"read_view": ...} but deploy_replay.py's _SERVER_JS
   only handles {"snapshot": True}. This mismatch means every snapshot call
   times out (15s) and returns {}. Fix: use the snapshot message the server
   actually understands.

2. Add a log line when a snapshot is saved, so progress is visible:
   "snapshot saved at tx {i}/{total}"
"""
import sys, ast
from pathlib import Path


def patch(target: Path) -> int:
    src = target.read_text()
    n = 0

    # ── FIX 1: rewrite _read_view_state to use the {"snapshot": True} message ──
    # The server's snapshotState uses the viewSelectors stored at deploy time,
    # so we don't even need to pass selectors again — just request a snapshot.
    OLD_HELPER = '''def _read_view_state(server, view_selectors: list) -> dict:
    """
    Call all zero-arg view functions on the deployed contract and return
    a dict mapping function name → return value (JSON-serialisable).
    Returns {} if no selectors or if the server call fails.
    """
    if not view_selectors:
        return {}
    try:
        res = server._send({"read_view": {"selectors": view_selectors}}, timeout=15)
        return res.get("view_state", {})
    except Exception:
        return {}'''

    NEW_HELPER = '''def _read_view_state(server, view_selectors: list) -> dict:
    """
    Snapshot the contract's zero-arg view-function state via the persistent
    Node.js server. The server already holds the view selectors that were
    passed at deploy time, so we just request a snapshot. Returns a dict
    mapping function name -> return value, or {} if there are no view fns
    or the call fails.
    """
    if not view_selectors:
        return {}
    try:
        res = server._send({"snapshot": True}, timeout=15)
        return res.get("snapshot", {})
    except Exception:
        return {}'''

    if OLD_HELPER in src:
        src = src.replace(OLD_HELPER, NEW_HELPER, 1)
        n += 1
        print("  ✓ fix 1: _read_view_state now uses {snapshot:True} message")
    else:
        print("  · fix 1 skipped (helper not found or already patched)")

    # ── FIX 2: add a log line when snapshot is saved ──────────────────────
    # transactions script style (50-step, indented under replay loop)
    OLD_SNAP_T = '''                if i % 50 == 0 or i == len(txs_to_replay):
                    res["_view_state"] = _read_view_state(server, view_selectors)'''
    NEW_SNAP_T = '''                if i % 50 == 0 or i == len(txs_to_replay):
                    res["_view_state"] = _read_view_state(server, view_selectors)
                    if HAS_TQDM and isinstance(_iter, tqdm):
                        _iter.write(f"    snapshot saved at tx {i}/{len(txs_to_replay)}")
                    else:
                        print(f"    snapshot saved at tx {i}/{len(txs_to_replay)}", flush=True)'''

    if OLD_SNAP_T in src:
        src = src.replace(OLD_SNAP_T, NEW_SNAP_T, 1)
        n += 1
        print("  ✓ fix 2: added snapshot log line (transactions style)")

    # generated script style (different indentation/var names, no tqdm there)
    OLD_SNAP_G = '''                res["_view_state"] = _read_view_state(server, view_selectors)'''
    NEW_SNAP_G = '''                if i % 50 == 0 or i == len(txs_to_replay):
                    res["_view_state"] = _read_view_state(server, view_selectors)
                    print(f"      snapshot saved at tx {i}/{len(txs_to_replay)}", flush=True)'''

    # Only apply the generated-style fix if the transactions-style anchor wasn't
    # the one present (avoid double patching). We check the bare line exists
    # and isn't already guarded by an "if i % 50" right above it.
    if OLD_SNAP_T not in src and OLD_SNAP_G in src:
        # Make sure it's not already wrapped
        if "snapshot saved at tx" not in src:
            src = src.replace(OLD_SNAP_G, NEW_SNAP_G, 1)
            n += 1
            print("  ✓ fix 2: added snapshot guard+log (generated style)")
        else:
            print("  · fix 2 skipped (already has snapshot log)")

    target.write_text(src)

    try:
        ast.parse(src)
        print("  Syntax OK")
    except SyntaxError as e:
        print(f"  SYNTAX ERROR: {e}")

    return n


if __name__ == "__main__":
    targets = sys.argv[1:] if len(sys.argv) > 1 else [
        "replay_synth_transactions.py",
        "replay_synth_generated.py",
    ]
    for t in targets:
        p = Path(t)
        if not p.exists():
            print(f"  [skip] {t} not found")
            continue
        print(f"\nPatching {t} ...")
        nn = patch(p)
        print(f"  Applied {nn} fixes")