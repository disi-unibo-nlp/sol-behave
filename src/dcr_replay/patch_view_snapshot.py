"""
patch_view_snapshots_v2.py
==========================
The deploy_replay.py _SERVER_JS already captures state_before/state_after
in every tx result when view_selectors are passed at deploy time.

What's missing in replay_synth_transactions.py and replay_synth_generated.py:
  1. view_selectors is built but NOT passed to server.deploy()
     (the call is: server.deploy(fqn, constructor_args) — missing 3rd arg)
  2. state_before / state_after are already in each tx result from the JS side,
     but we need to make sure they're preserved in the saved tx_results.
     (They are — since res is saved as-is, this is already fine.)

So the only real patch needed is passing view_selectors to server.deploy().
"""
import sys, ast
from pathlib import Path


def patch(target: Path) -> int:
    src = target.read_text()
    n = 0

    # ── Patch: pass view_selectors to server.deploy() ─────────────────────
    # Both scripts call server.deploy(fqn, constructor_args, view_selectors=...)
    # already in some places but may be missing in others. Find all bare calls.

    # Pattern 1: replay_synth_transactions.py style
    OLD1 = "deployed_address = server.deploy(fqn, constructor_args)"
    NEW1 = "deployed_address = server.deploy(fqn, constructor_args, view_selectors=view_selectors)"
    if OLD1 in src:
        count = src.count(OLD1)
        src = src.replace(OLD1, NEW1)
        n += count
        print(f"  ✓ patched {count}× bare server.deploy() → with view_selectors")

    # Pattern 2: fallback deploy with fallback args
    OLD2 = "deployed_address = server.deploy(fqn, fallback)"
    NEW2 = "deployed_address = server.deploy(fqn, fallback, view_selectors=view_selectors)"
    if OLD2 in src:
        count = src.count(OLD2)
        src = src.replace(OLD2, NEW2)
        n += count
        print(f"  ✓ patched {count}× fallback server.deploy() → with view_selectors")

    # Pattern 3: replay_synth_generated.py uses different variable name
    OLD3 = "deployed_address = server.deploy(fqn, constructor_args,\n                                             view_selectors=view_selectors)"
    # already correct — skip
    if OLD3 in src:
        print(f"  · view_selectors already passed in deploy call (no change needed)")

    target.write_text(src)

    # syntax check
    try:
        ast.parse(src)
        print(f"  Syntax OK")
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
        n = patch(p)
        print(f"  Applied {n} replacements")