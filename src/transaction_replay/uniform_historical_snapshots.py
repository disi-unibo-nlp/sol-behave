"""
uniform_historical_snapshots.py
===============================
Make replay_transactions.py and replay_generated.py capture view-function
state the SAME way the synth scripts do: via the fast Python-side
{"snapshot": True} message every SNAPSHOT_EVERY txs, stored in res["_view_state"].

Currently both historical scripts rely on the JS-side state_after path which:
  - fires on EVERY tx (slow: ~15s/tx with 2 view fns), and
  - in replay_transactions gave null because... actually it should populate,
    but it's the slow path regardless.

After this patch, all four replay scripts produce identical _view_state
snapshots at identical tx indices (50, 100, 150, ...), so the comprehensive
analysis can diff original vs generated state tx-by-tx for BOTH the historical
and synthetic transaction sets.

Changes per script:
  1. Add a module-level _read_view_state(server, view_selectors) helper
     (sends {"snapshot": True}, returns res["snapshot"]).
  2. In the replay loop, after server.replay_tx(...), add:
       if i % SNAPSHOT_EVERY == 0 or i == len(txs_to_replay):
           res["_view_state"] = _read_view_state(server, view_selectors)
  3. Import SNAPSHOT_EVERY from deploy_replay.
"""
import sys, ast
from pathlib import Path


def patch_replay_transactions(p: Path) -> int:
    src = p.read_text()
    n = 0

    # ── 1. Import SNAPSHOT_EVERY from deploy_replay ───────────────────────
    OLD_IMPORT = """from deploy_replay import (
    DeployServer,
    _try_etherscan_args,
    _extract_args_from_tx,
    _default_args,
    synthesize_constructor_args,
    _load_ckpt,
    _save_ckpt,
    setup_workspace,        # writes deploy_server.js in addition to npm install
)"""
    NEW_IMPORT = """from deploy_replay import (
    DeployServer,
    _try_etherscan_args,
    _extract_args_from_tx,
    _default_args,
    synthesize_constructor_args,
    _load_ckpt,
    _save_ckpt,
    setup_workspace,        # writes deploy_server.js in addition to npm install
    SNAPSHOT_EVERY,
)


def _read_view_state(server, view_selectors: list) -> dict:
    \"\"\"
    Snapshot zero-arg view-function state via the persistent Node.js server,
    using the fast {\"snapshot\": True} message (the server already holds the
    view selectors passed at deploy time). Returns {} if no view fns or on error.
    \"\"\"
    if not view_selectors:
        return {}
    try:
        res = server._send({\"snapshot\": True}, timeout=15)
        return res.get(\"snapshot\", {})
    except Exception:
        return {}"""

    if OLD_IMPORT in src and "_read_view_state" not in src:
        src = src.replace(OLD_IMPORT, NEW_IMPORT, 1)
        n += 1
        print("  ✓ rt.1: import SNAPSHOT_EVERY + _read_view_state helper")
    else:
        print("  · rt.1 skipped (anchor not found or already patched)")

    # ── 2. Add snapshot capture in the replay loop ────────────────────────
    # The loop assigns res["_fn_sig"] right after replay_tx; insert snapshot
    # right after the events_match handling, before tx_results.append(res).
    OLD_LOOP = """                res = server.replay_tx(tx, historical_logs=historical_logs)
                res["_fn_sig"] = fn_selector_map.get(
                    (tx.get("input") or "0x")[2:10].lower(), ""
                )
                match = res.get("events_match", False)
                if match:
                    passed += 1
                else:
                    failed += 1
                    if verbose:
                        reason = (res.get("revert_reason") or res.get("error") or "")[:100]
                        if reason:
                            print(f"    tx {i} mismatch: {reason}")
                tx_results.append(res)"""
    NEW_LOOP = """                res = server.replay_tx(tx, historical_logs=historical_logs)
                res["_fn_sig"] = fn_selector_map.get(
                    (tx.get("input") or "0x")[2:10].lower(), ""
                )
                match = res.get("events_match", False)
                # Fast state snapshot every SNAPSHOT_EVERY txs (and at the end),
                # mirroring the synthetic replay scripts for state-divergence analysis.
                if i % SNAPSHOT_EVERY == 0 or i == len(txs_to_replay):
                    res["_view_state"] = _read_view_state(server, view_selectors)
                if match:
                    passed += 1
                else:
                    failed += 1
                    if verbose:
                        reason = (res.get("revert_reason") or res.get("error") or "")[:100]
                        if reason:
                            print(f"    tx {i} mismatch: {reason}")
                tx_results.append(res)"""

    if OLD_LOOP in src:
        src = src.replace(OLD_LOOP, NEW_LOOP, 1)
        n += 1
        print("  ✓ rt.2: snapshot capture added to replay loop")
    else:
        print("  · rt.2 skipped (loop anchor not found)")

    p.write_text(src)
    try:
        ast.parse(src)
        print("  Syntax OK")
    except SyntaxError as e:
        print(f"  SYNTAX ERROR: {e}")
    return n


def patch_replay_generated(p: Path) -> int:
    src = p.read_text()
    n = 0

    # ── 1. Import SNAPSHOT_EVERY + helper ─────────────────────────────────
    OLD_IMPORT = """from deploy_replay import (
    DeployServer,
    _try_etherscan_args,
    _default_args,
    setup_workspace,
)
from hardhat_utils import prepare_and_compile"""
    NEW_IMPORT = """from deploy_replay import (
    DeployServer,
    _try_etherscan_args,
    _default_args,
    setup_workspace,
    SNAPSHOT_EVERY,
)
from hardhat_utils import prepare_and_compile


def _read_view_state(server, view_selectors: list) -> dict:
    \"\"\"
    Snapshot zero-arg view-function state via the persistent Node.js server,
    using the fast {\"snapshot\": True} message. Returns {} if no view fns or on error.
    \"\"\"
    if not view_selectors:
        return {}
    try:
        res = server._send({\"snapshot\": True}, timeout=15)
        return res.get(\"snapshot\", {})
    except Exception:
        return {}"""

    if OLD_IMPORT in src and "_read_view_state" not in src:
        src = src.replace(OLD_IMPORT, NEW_IMPORT, 1)
        n += 1
        print("  ✓ rg.1: import SNAPSHOT_EVERY + _read_view_state helper")
    else:
        print("  · rg.1 skipped (anchor not found or already patched)")

    # ── 2. Snapshot capture in the replay loop ────────────────────────────
    OLD_LOOP = """                res   = server.replay_tx(tx, historical_logs=historical_logs)
                match = res.get("events_match", False)
                if match:
                    passed += 1
                else:
                    failed += 1
                    if verbose:
                        reason = (res.get("revert_reason") or res.get("error") or "")[:80]
                        if reason:
                            print(f"      tx {i} mismatch: {reason}")
                tx_results.append(res)"""
    NEW_LOOP = """                res   = server.replay_tx(tx, historical_logs=historical_logs)
                match = res.get("events_match", False)
                # Fast state snapshot every SNAPSHOT_EVERY txs (and at the end),
                # mirroring the synthetic replay scripts for state-divergence analysis.
                if i % SNAPSHOT_EVERY == 0 or i == len(txs_to_replay):
                    res["_view_state"] = _read_view_state(server, view_selectors)
                if match:
                    passed += 1
                else:
                    failed += 1
                    if verbose:
                        reason = (res.get("revert_reason") or res.get("error") or "")[:80]
                        if reason:
                            print(f"      tx {i} mismatch: {reason}")
                tx_results.append(res)"""

    if OLD_LOOP in src:
        src = src.replace(OLD_LOOP, NEW_LOOP, 1)
        n += 1
        print("  ✓ rg.2: snapshot capture added to replay loop")
    else:
        print("  · rg.2 skipped (loop anchor not found)")

    p.write_text(src)
    try:
        ast.parse(src)
        print("  Syntax OK")
    except SyntaxError as e:
        print(f"  SYNTAX ERROR: {e}")
    return n


if __name__ == "__main__":
    rt = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("replay_transactions.py")
    rg = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("replay_generated.py")

    if rt.exists():
        print(f"Patching {rt} ...")
        patch_replay_transactions(rt)
    else:
        print(f"  [skip] {rt} not found")

    if rg.exists():
        print(f"\nPatching {rg} ...")
        patch_replay_generated(rg)
    else:
        print(f"  [skip] {rg} not found")