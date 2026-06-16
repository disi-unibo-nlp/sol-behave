#!/usr/bin/env python3
"""
patch_absorbing_events.py
=========================
Fix the privilege-drop ordering bug in make_sequences_hardhat.py.

THE BUG
-------
The main exploration loop can fire renounceOwnership / transferOwnership as a
regular event. Once it does, the on-chain owner becomes address(0) (or a
different account), so every subsequent owner-role event reverts. The DCR
sequence keeps scheduling owner-only events after the drop, producing long
runs of reverts that replay faithfully reproduces (the 7 self-failing
contracts: NyanToken, PurchaseContract, etc.).

THE FIX (4 targeted edits)
--------------------------
1. Add _is_absorbing_event(): structurally detect privilege-drop functions
   (renounceOwnership; transferOwnership away from owner) — independent of the
   LLM's ownership_change flag.
2. In _select_event(): HARD-EXCLUDE absorbing events from the main loop's
   candidate pool (they may only fire in the drain phase), AND once ownership
   has been dropped in this sequence, exclude ALL owner-role events.
   This needs an `owner_dropped` signal, threaded via a mutable flag.
3. In the main loop: when an absorbing event somehow fires (defensive) or when
   the drain fires one, set owner_dropped=True.
4. In the drain phase: stop scheduling owner-only events after the first
   successful privilege drop, and ensure renounce fires LAST (already priority
   99, but we also short-circuit afterwards).

This script edits the file in place. A .bak copy is saved first.
"""
import sys
import shutil
from pathlib import Path

def patch(path: Path) -> None:
    src = path.read_text()
    orig = src
    edits = 0

    # ─────────────────────────────────────────────────────────────────────
    # EDIT 1: add _is_absorbing_event helper right before _select_event
    # ─────────────────────────────────────────────────────────────────────
    anchor1 = "def _select_event(\n"
    helper = '''# ── Absorbing (privilege-drop) event detection ───────────────────────────────
# An "absorbing" event irreversibly removes the deployer's ownership, after
# which all owner-only functions revert. These MUST NOT be scheduled in the
# middle of a sequence — only in the drain phase, and once one fires no further
# owner-role event may be scheduled. Detection is structural (function name),
# so it does not depend on the LLM having set ownership_change correctly.

_ABSORBING_FN_RE = _re.compile(
    r"(renounce(_)?ownership|renouncerole|revokerole)",
    _re.IGNORECASE,
)


def _is_absorbing_event(event: dict) -> bool:
    """
    True if firing this event irreversibly drops the deployer's privileges.

    - renounceOwnership / renounceRole / revokeRole  -> always absorbing.
    - transferOwnership                              -> absorbing (ownership
      moves away from the OWNER signer used at replay; subsequent owner-only
      calls from OWNER revert).
    The LLM-provided ownership_change flag is also honoured as a fallback.
    """
    fn = (event.get("function_name") or event.get("id") or "")
    if _ABSORBING_FN_RE.search(fn):
        return True
    if "transferownership" in fn.lower().replace("_", ""):
        return True
    # Fallback to the generator's own flag if present.
    return bool(event.get("ownership_change", False)) and \\
        ("renounce" in fn.lower() or "transfer" in fn.lower())


'''
    if "_is_absorbing_event" not in src and anchor1 in src:
        src = src.replace(anchor1, helper + anchor1, 1)
        edits += 1
        print("  ✓ EDIT 1: added _is_absorbing_event() helper")
    else:
        print("  · EDIT 1 skipped (already present or anchor missing)")

    # ─────────────────────────────────────────────────────────────────────
    # EDIT 2: _select_event signature — add owner_dropped param
    # ─────────────────────────────────────────────────────────────────────
    old_sig = '''def _select_event(
    dcr: dict,
    dcr_state: DCRState,
    rng: random.Random,
    success_counts: dict[str, int],
    attempt_counts: dict[str, int],
    failure_counts: dict[str, int],
    persistent_failures: dict[str, int],
    total_steps: int,
    max_persistent: int,
) -> str | None:'''
    new_sig = '''def _select_event(
    dcr: dict,
    dcr_state: DCRState,
    rng: random.Random,
    success_counts: dict[str, int],
    attempt_counts: dict[str, int],
    failure_counts: dict[str, int],
    persistent_failures: dict[str, int],
    total_steps: int,
    max_persistent: int,
    owner_dropped: bool = False,
) -> str | None:'''
    if old_sig in src:
        src = src.replace(old_sig, new_sig, 1)
        edits += 1
        print("  ✓ EDIT 2: _select_event signature now takes owner_dropped")
    else:
        print("  · EDIT 2 skipped (signature not found verbatim)")

    # ─────────────────────────────────────────────────────────────────────
    # EDIT 3: inside _select_event, hard-exclude absorbing events (and all
    # owner-role events once ownership dropped) from the safe pool.
    # We hook right after `safe_enabled = [...]` is first computed.
    # ─────────────────────────────────────────────────────────────────────
    old_safe = '''    safe_enabled = [e for e in enabled if e not in destructive and _ok(e)]
    if not safe_enabled:
        safe_enabled = [e for e in enabled if _ok(e)]
    if not safe_enabled:
        return None'''
    new_safe = '''    # Absorbing privilege-drop events (renounce/transferOwnership) must NEVER
    # fire in the main loop — only in the drain phase. Exclude them here.
    absorbing_ids = {e["id"] for e in dcr["events"] if _is_absorbing_event(e)}

    # If ownership was already dropped earlier in this sequence, exclude ALL
    # owner-role events too — they would revert with "not the owner".
    owner_role_ids = set()
    if owner_dropped:
        owner_role_ids = {e["id"] for e in dcr["events"]
                          if e.get("role") == "owner"}

    blocked = destructive | absorbing_ids | owner_role_ids

    safe_enabled = [e for e in enabled if e not in blocked and _ok(e)]
    if not safe_enabled:
        # Relax `destructive` but KEEP absorbing/owner-dropped exclusions:
        # we would rather end the sequence than fire a privilege drop mid-run.
        relaxed_block = absorbing_ids | owner_role_ids
        safe_enabled = [e for e in enabled if e not in relaxed_block and _ok(e)]
    if not safe_enabled:
        return None'''
    if old_safe in src:
        src = src.replace(old_safe, new_safe, 1)
        edits += 1
        print("  ✓ EDIT 3: absorbing + owner-dropped events excluded in _select_event")
    else:
        print("  · EDIT 3 skipped (safe_enabled block not found verbatim)")

    # ─────────────────────────────────────────────────────────────────────
    # EDIT 4: in generate_sequences_hardhat, declare owner_dropped and pass it
    # to _select_event; set it if an absorbing event ever fires successfully.
    # ─────────────────────────────────────────────────────────────────────
    # 4a. declare the flag near the other counters
    old_decl = '''    steps_ok   = 0
    reset_count = 0'''
    new_decl = '''    steps_ok   = 0
    reset_count = 0
    owner_dropped = False   # set True once a privilege-drop event fires'''
    if old_decl in src and "owner_dropped = False" not in src:
        src = src.replace(old_decl, new_decl, 1)
        edits += 1
        print("  ✓ EDIT 4a: owner_dropped flag declared")
    else:
        print("  · EDIT 4a skipped")

    # 4b. pass owner_dropped into _select_event call
    old_call = '''        event_id = _select_event(dcr, dcr_state, rng,
                                  success_counts, attempt_counts,
                                  failure_counts, persistent_failures,
                                  total_steps=steps_ok + 1,
                                  max_persistent=MAX_PERSISTENT_FAILURES)'''
    new_call = '''        event_id = _select_event(dcr, dcr_state, rng,
                                  success_counts, attempt_counts,
                                  failure_counts, persistent_failures,
                                  total_steps=steps_ok + 1,
                                  max_persistent=MAX_PERSISTENT_FAILURES,
                                  owner_dropped=owner_dropped)'''
    if old_call in src:
        src = src.replace(old_call, new_call, 1)
        edits += 1
        print("  ✓ EDIT 4b: owner_dropped passed to _select_event")
    else:
        print("  · EDIT 4b skipped (call not found verbatim)")

    # 4c. set owner_dropped when an absorbing event fires successfully in main loop
    #     (defensive — should no longer happen, but if it does we must flag it).
    old_success = '''        if status == "success":
            stall_counter = 0
            success_counts[event_id] += 1
            failure_counts[event_id]  = 0
            successful_functions.add(fn_name)
            successful_events.add(event_id)'''
    new_success = '''        if status == "success":
            stall_counter = 0
            success_counts[event_id] += 1
            failure_counts[event_id]  = 0
            successful_functions.add(fn_name)
            successful_events.add(event_id)
            # If this was a privilege-drop event, flag it so no further
            # owner-role events get scheduled in the rest of the sequence.
            if _is_absorbing_event(event):
                owner_dropped = True'''
    if old_success in src and "if _is_absorbing_event(event):" not in src:
        src = src.replace(old_success, new_success, 1)
        edits += 1
        print("  ✓ EDIT 4c: owner_dropped set on absorbing success in main loop")
    else:
        print("  · EDIT 4c skipped")

    # ─────────────────────────────────────────────────────────────────────
    # EDIT 5: drain phase — stop scheduling owner-only events after the first
    # successful privilege drop. We sort so non-renounce drains go first, then
    # renounce LAST, and once a renounce/transfer succeeds we break.
    # ─────────────────────────────────────────────────────────────────────
    old_drain_loop = '''    for _, event, fn_entry in drain_candidates:
        eid       = event["id"]
        fn_name   = event.get("function_name", eid)
        cp        = call_params.get(eid, {})
        params    = cp.get("params", [])
        value_wei = int(cp.get("value_wei", 0))
        try:
            tx  = encode_call(fn_entry, params, value_wei, OWNER_ADDR)
            res = server.send({"tx": tx})
            if res.get("status") == "success":'''
    new_drain_loop = '''    drain_owner_dropped = owner_dropped
    for _, event, fn_entry in drain_candidates:
        eid       = event["id"]
        fn_name   = event.get("function_name", eid)
        # Once ownership has been dropped, every remaining owner-only drain
        # event would revert — skip them instead of recording reverts.
        if drain_owner_dropped and event.get("role", "owner") == "owner":
            print(f"      [drain skip] {fn_name}  (ownership already dropped)")
            continue
        cp        = call_params.get(eid, {})
        params    = cp.get("params", [])
        value_wei = int(cp.get("value_wei", 0))
        try:
            tx  = encode_call(fn_entry, params, value_wei, OWNER_ADDR)
            res = server.send({"tx": tx})
            if res.get("status") == "success":'''
    if old_drain_loop in src:
        src = src.replace(old_drain_loop, new_drain_loop, 1)
        edits += 1
        print("  ✓ EDIT 5a: drain loop skips owner events after ownership drop")
    else:
        print("  · EDIT 5a skipped (drain loop head not found verbatim)")

    # 5b. After a successful drain of an absorbing event, set the flag.
    old_drain_success = '''                all_txs.append(vtx)
                block_num += 1
                successful_functions.add(fn_name)
                successful_events.add(eid)
                # Update DCR state so subsequent drain events see the new state
                dcr_state = fire_dcr(dcr, dcr_state, eid, known_state=known_state)'''
    new_drain_success = '''                all_txs.append(vtx)
                block_num += 1
                successful_functions.add(fn_name)
                successful_events.add(eid)
                # If this drain event dropped ownership, block all later
                # owner-only drain events.
                if _is_absorbing_event(event):
                    drain_owner_dropped = True
                # Update DCR state so subsequent drain events see the new state
                dcr_state = fire_dcr(dcr, dcr_state, eid, known_state=known_state)'''
    if old_drain_success in src and "drain_owner_dropped = True" not in src:
        src = src.replace(old_drain_success, new_drain_success, 1)
        edits += 1
        print("  ✓ EDIT 5b: drain sets drain_owner_dropped on absorbing success")
    else:
        print("  · EDIT 5b skipped")

    # ─────────────────────────────────────────────────────────────────────
    if src == orig:
        print("\n  NO CHANGES APPLIED — anchors did not match. File untouched.")
        return

    bak = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak)
    path.write_text(src)
    print(f"\n  Applied {edits} edits.")
    print(f"  Backup saved: {bak}")

    # syntax check
    import ast
    try:
        ast.parse(src)
        print("  Syntax OK ✓")
    except SyntaxError as e:
        print(f"  SYNTAX ERROR: {e}  — restoring backup")
        shutil.copy2(bak, path)


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("make_sequences_hardhat.py")
    if not target.exists():
        print(f"ERROR: {target} not found. Pass the path:")
        print(f"  python patch_absorbing_events.py /workdir/src/dcr_generation/make_sequences_hardhat.py")
        sys.exit(1)
    print(f"Patching {target} ...\n")
    patch(target)