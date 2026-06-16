"""Patch replay_transactions.py to add per-function pass rates."""
import sys
from pathlib import Path

target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("replay_transactions.py")
src = target.read_text()
n = 0

# ── PATCH 1: tag each res with _fn_sig in the replay loop ────────────────────
OLD_RES = '''                res = server.replay_tx(tx, historical_logs=historical_logs)
                match = res.get("events_match", False)'''

NEW_RES = '''                res = server.replay_tx(tx, historical_logs=historical_logs)
                res["_fn_sig"] = fn_selector_map.get(
                    (tx.get("input") or "0x")[2:10].lower(), ""
                )
                match = res.get("events_match", False)'''

if OLD_RES in src:
    src = src.replace(OLD_RES, NEW_RES, 1)
    n += 1
    print("  ✓ patch 1: tag res with _fn_sig")
else:
    print("  ✗ patch 1 not found")

# ── PATCH 2: compute fn_pass_rates in Step 5 after gas_ratios ────────────────
OLD_SUMMARY = '''        # Aggregate the new metrics across all tx_results
        gas_ratios    = [r["gas_ratio"]  for r in tx_results'''

NEW_SUMMARY = '''        # Per-function pass rates (keyed on function name, not full sig)
        fn_pass: dict[str, list[bool]] = {}
        for r in tx_results:
            fn_sig = r.get("_fn_sig", "")
            fn_name = fn_sig.split("(")[0] if fn_sig else ""
            if fn_name:
                fn_pass.setdefault(fn_name, []).append(r.get("events_match", False))
        fn_pass_rates = {
            fn: round(sum(v) / len(v), 4)
            for fn, v in fn_pass.items()
        }

        # Aggregate the new metrics across all tx_results
        gas_ratios    = [r["gas_ratio"]  for r in tx_results'''

if OLD_SUMMARY in src:
    src = src.replace(OLD_SUMMARY, NEW_SUMMARY, 1)
    n += 1
    print("  ✓ patch 2: compute fn_pass_rates")
else:
    print("  ✗ patch 2 not found")

# ── PATCH 3: add fn_pass_rates to result dict ─────────────────────────────────
OLD_RESULT = '''            "n_view_fns":       len(view_selectors),
        }
        save_ckpt_k(contract_dir, k, result)'''

NEW_RESULT = '''            "n_view_fns":       len(view_selectors),
            "fn_pass_rates":    fn_pass_rates,
        }
        save_ckpt_k(contract_dir, k, result)'''

if OLD_RESULT in src:
    src = src.replace(OLD_RESULT, NEW_RESULT, 1)
    n += 1
    print("  ✓ patch 3: add fn_pass_rates to result dict")
else:
    print("  ✗ patch 3 not found")

# ── PATCH 4: add fn_pass_rates to _result_path_k write ───────────────────────
OLD_FILE = '''            "n_view_fns":            len(view_selectors),
            "timestamp":             datetime.utcnow().isoformat(),
            "tx_results":            tx_results,'''

NEW_FILE = '''            "n_view_fns":            len(view_selectors),
            "fn_pass_rates":         fn_pass_rates,
            "timestamp":             datetime.utcnow().isoformat(),
            "tx_results":            tx_results,'''

if OLD_FILE in src:
    src = src.replace(OLD_FILE, NEW_FILE, 1)
    n += 1
    print("  ✓ patch 4: add fn_pass_rates to result file")
else:
    print("  ✗ patch 4 not found")

target.write_text(src)
print(f"\nApplied {n}/4 patches to {target}")