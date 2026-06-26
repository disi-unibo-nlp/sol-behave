"""
analyse_synth_replay_depth.py
=============================

Deep analysis of SYNTHETIC-transaction replay, comparing:
  - original  contracts replayed on DCR synthetic sequences
      (replay_synth_transactions.py -> synth_replay_result_<K>.json, ONE per contract)
  - generated contracts replayed on the SAME synthetic sequences
      (replay_synth_generated.py  -> synth_replay_sample_<N>_result_<K>.json, MANY per contract)

For each contract, every generated SAMPLE is compared against the single
original replay, matched per-tx by hash. Per-axis rates are computed per
sample, then aggregated across samples (mean) to a per-contract row.

Axes:
  1. Status     pass/revert agreement (orig vs gen on the synthetic seq)
  2. Gas ratio  gen_gas / orig_gas per tx
  3. Log count  same number of events
  4. Topic[0]   same event-signature hashes
  5. State      view-function snapshot agreement (state_after)

Divergence (status_match_rate < 1.0) is the paper's signal: defects the
synthetic oracle exposes in generated contracts that historical replay misses.

Usage:
  python analyse_synth_replay_depth.py \
      --orig-dir ./original_synth_replay_results \
      --gen-dir  ./synth_replay_results_<model> \
      --k 1000
"""

import argparse, json, sys
from pathlib import Path
from typing import Optional

_SCRIPT_DIR = Path(__file__).resolve().parent


def _median(vals):
    if not vals: return None
    s = sorted(vals); n = len(s)
    return s[n//2] if n % 2 else (s[n//2-1] + s[n//2]) / 2


def _mean(vals):
    return round(sum(vals)/len(vals), 4) if vals else None


def _tx_list(result: dict) -> list:
    return result.get("tx_results") or result.get("txs") or []


# ── File loading ─────────────────────────────────────────────────────────────

def _load_one(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _load_original(contract_dir: Path, orig_glob: str) -> Optional[dict]:
    """Original side: a single result file per contract."""
    matches = sorted(contract_dir.glob(orig_glob))
    # Prefer the non-sample file if both exist
    non_sample = [m for m in matches if "sample" not in m.name]
    pick = non_sample[0] if non_sample else (matches[0] if matches else None)
    return _load_one(pick) if pick else None


def _load_generated_samples(contract_dir: Path, gen_glob: str) -> list[dict]:
    """Generated side: ALL per-sample result files for a contract."""
    out = []
    for p in sorted(contract_dir.glob(gen_glob)):
        d = _load_one(p)
        if d is not None:
            out.append(d)
    return out


# ── Per-sample comparison: one generated sample vs the original ──────────────

def _status(tx) -> Optional[str]:
    return tx.get("actual_status") or tx.get("status")


def _compare_one_sample(orig: dict, gen: dict, hash_field: str,
                        state_field: str) -> Optional[dict]:
    """Compare a single generated sample against the original. Per-axis rates."""
    o_txs = _tx_list(orig)
    g_map = {t.get(hash_field): t for t in _tx_list(gen) if t.get(hash_field)}
    if not o_txs or not g_map:
        return None

    status_matches, gas_ratios = [], []
    log_matches, topic_matches = [], []

    for o in o_txs:
        h = o.get(hash_field)
        if not h or h not in g_map:
            continue
        g = g_map[h]

        os_, gs_ = _status(o), _status(g)
        if os_ is not None and gs_ is not None:
            status_matches.append(os_ == gs_)

        og, gg = o.get("actual_gas_used"), g.get("actual_gas_used")
        if og and gg:
            gas_ratios.append(gg / og)

        olc, glc = o.get("actual_log_count"), g.get("actual_log_count")
        if olc is not None and glc is not None:
            log_matches.append(olc == glc)

        ot, gt = o.get("actual_topics"), g.get("actual_topics")
        if ot is not None and gt is not None:
            topic_matches.append(len(ot) == len(gt) and
                                 all(a == b for a, b in zip(ot, gt)))

    if not status_matches:
        return None

    # State snapshot agreement (sparse) — uses state_field (default state_after)
    o_snaps = [t.get(state_field) for t in o_txs if t.get(state_field)]
    g_snaps = [t.get(state_field) for t in _tx_list(gen) if t.get(state_field)]
    state_match = None
    if o_snaps and g_snaps:
        n = min(len(o_snaps), len(g_snaps))
        m = tot = 0
        for i in range(n):
            keys = set(o_snaps[i]) | set(g_snaps[i])
            for k in keys:
                tot += 1
                if o_snaps[i].get(k) == g_snaps[i].get(k):
                    m += 1
        state_match = round(m / tot, 4) if tot else None

    return {
        "n_compared":           len(status_matches),
        "status_match_rate":    _mean([1.0 if x else 0.0 for x in status_matches]),
        "mean_gas_ratio":       _mean(gas_ratios),
        "gas_within_10pct":     _mean([1.0 if 0.9 <= r <= 1.1 else 0.0 for r in gas_ratios]),
        "log_count_match_rate": _mean([1.0 if x else 0.0 for x in log_matches]),
        "topics_match_rate":    _mean([1.0 if x else 0.0 for x in topic_matches]),
        "state_match_rate":     state_match,
    }


# ── Per-contract: aggregate across all generated samples ─────────────────────

def compare_contract(orig: dict, gen_samples: list[dict], hash_field: str,
                     state_field: str) -> Optional[dict]:
    per_sample = []
    for gen in gen_samples:
        r = _compare_one_sample(orig, gen, hash_field, state_field)
        if r is not None:
            per_sample.append(r)
    if not per_sample:
        return None

    def agg(key):
        vals = [s[key] for s in per_sample if s.get(key) is not None]
        return _mean(vals)

    addr = orig.get("address", "?")
    name = orig.get("contract_name", addr[:12] + "…")

    # A contract "diverges" if ANY sample diverged on status (mean < 1.0),
    # and we also track the worst (min) sample status as the strongest signal.
    sample_status = [s["status_match_rate"] for s in per_sample
                     if s.get("status_match_rate") is not None]
    min_status = min(sample_status) if sample_status else None

    # Transactions actually compared (matched orig↔gen by hash). Samples of the
    # same contract share the synthetic sequence, so take the max as the
    # representative count (samples may truncate early on a revert).
    n_txs = max((s.get("n_compared", 0) for s in per_sample), default=0)

    return {
        "address":              addr,
        "contract_name":        name,
        "n_samples":            len(per_sample),
        "n_txs":                n_txs,
        "status_match_rate":    agg("status_match_rate"),   # mean across samples
        "min_sample_status":    round(min_status, 4) if min_status is not None else None,
        "mean_gas_ratio":       agg("mean_gas_ratio"),
        "gas_within_10pct":     agg("gas_within_10pct"),
        "log_count_match_rate": agg("log_count_match_rate"),
        "topics_match_rate":    agg("topics_match_rate"),
        "state_match_rate":     agg("state_match_rate"),
    }


def compute_aggregates(rows: list[dict]) -> dict:
    valid = [r for r in rows if r.get("status_match_rate") is not None]
    if not valid:
        return {"error": "no comparable contracts"}
    status = [r["status_match_rate"]    for r in valid]
    gas    = [r["mean_gas_ratio"]       for r in valid if r.get("mean_gas_ratio") is not None]
    g10    = [r["gas_within_10pct"]     for r in valid if r.get("gas_within_10pct") is not None]
    logs   = [r["log_count_match_rate"] for r in valid if r.get("log_count_match_rate") is not None]
    tops   = [r["topics_match_rate"]    for r in valid if r.get("topics_match_rate") is not None]
    states = [r["state_match_rate"]     for r in valid if r.get("state_match_rate") is not None]
    # Diverged: mean status < 1.0 (any sample disagreed with original)
    diverged = [r for r in valid if r.get("status_match_rate", 1.0) < 1.0]
    # Strongly diverged: at least one sample fully exposed it (min < 1.0)
    diverged_any_sample = [r for r in valid
                           if r.get("min_sample_status") is not None
                           and r["min_sample_status"] < 1.0]
    return {
        "n_contracts":               len(valid),
        "mean_status_match_rate":    _mean(status),
        "median_status_match":       round(_median(status), 4),
        "n_status_diverged":         len(diverged),
        "n_diverged_any_sample":     len(diverged_any_sample),
        "diverged_addresses":        [(r["address"], r["contract_name"],
                                       r["status_match_rate"], r.get("min_sample_status"))
                                      for r in sorted(diverged, key=lambda x: x["status_match_rate"])],
        "mean_gas_ratio":            _mean(gas),
        "mean_gas_within_10pct":     _mean(g10),
        "mean_log_count_match_rate": _mean(logs),
        "mean_topics_match_rate":    _mean(tops),
        "mean_state_match_rate":     _mean(states),
        "n_with_state_data":         len(states),
    }


def print_summary(rows, agg, k):
    valid = sorted([r for r in rows if r.get("status_match_rate") is not None],
                   key=lambda r: r.get("status_match_rate", 1.0))
    def _pct(v): return f"{v:.1%}" if v is not None else "  n/a "
    def _r(v):   return f"{v:.3f}" if v is not None else "  n/a "
    cn, w = 24, 8
    hdr = (f"  {'Contract':<{cn}}{'StatusM':>{w}}{'MinSmpl':>{w}}{'GasRat':>{w}}"
           f"{'G±10%':>{w}}{'LogCnt':>{w}}{'Topics':>{w}}{'StateM':>{w}}"
           f"{'Txs':>{w}}{'Smpls':>{w}}")
    sep = "─" * (len(hdr) - 2)
    print(f"\n  SYNTHETIC REPLAY DEPTH — original vs generated  (K={k})")
    print("  " + sep); print(hdr); print("  " + sep)
    for r in valid:
        print(f"  {r['contract_name']:<{cn}}"
              f"{_pct(r.get('status_match_rate')):>{w}}"
              f"{_pct(r.get('min_sample_status')):>{w}}"
              f"{_r(r.get('mean_gas_ratio')):>{w}}"
              f"{_pct(r.get('gas_within_10pct')):>{w}}"
              f"{_pct(r.get('log_count_match_rate')):>{w}}"
              f"{_pct(r.get('topics_match_rate')):>{w}}"
              f"{_pct(r.get('state_match_rate')):>{w}}"
              f"{r.get('n_txs',0):>{w}}"
              f"{r.get('n_samples',0):>{w}}")
    print("  " + sep)
    print("\n  AGGREGATE STATISTICS\n  " + sep)
    if "error" in agg:
        print(f"  {agg['error']}"); print("  " + sep); return
    print(f"  Contracts compared              : {agg['n_contracts']}")
    print(f"  Status DIVERGED (mean<100%)     : {agg['n_status_diverged']}  ← synthetic oracle catches these")
    print(f"  Diverged in ≥1 sample (min<100%): {agg['n_diverged_any_sample']}")
    print(f"  Mean status match rate          : {_pct(agg['mean_status_match_rate'])}")
    print(f"  Median status match             : {_pct(agg['median_status_match'])}")
    print(f"  Mean gas ratio (gen/orig)       : {_r(agg['mean_gas_ratio'])}")
    print(f"  Mean frac within ±10%           : {_pct(agg['mean_gas_within_10pct'])}")
    print(f"  Mean log-count match rate       : {_pct(agg['mean_log_count_match_rate'])}")
    print(f"  Mean topic[0] match rate        : {_pct(agg['mean_topics_match_rate'])}")
    print(f"  Mean state match rate           : {_pct(agg['mean_state_match_rate'])}  (n={agg['n_with_state_data']})")
    print("  " + sep)
    if agg["diverged_addresses"]:
        print("\n  Diverged contracts (synthetic oracle flags these):")
        print(f"    {'contract':<24}{'meanStatus':>12}{'minSample':>12}   address")
        for addr, name, ms, mn in agg["diverged_addresses"]:
            msr = f"{ms:.1%}" if ms is not None else "n/a"
            mnr = f"{mn:.1%}" if mn is not None else "n/a"
            print(f"    {name:<24}{msr:>12}{mnr:>12}   {addr}")


def parse_args():
    p = argparse.ArgumentParser(description="Compare original vs generated synthetic replay (multi-sample).")
    p.add_argument("--orig-dir", type=Path, required=True)
    p.add_argument("--gen-dir",  type=Path, required=True)
    p.add_argument("--k", type=int, default=1000)
    p.add_argument("--orig-glob", default="*synth*result*.json",
                   help="Glob for the single original result file per contract.")
    p.add_argument("--gen-glob", default="*sample*result*.json",
                   help="Glob for the per-sample generated result files per contract.")
    p.add_argument("--hash-field", default="hash",
                   help="Per-tx field identifying the synthetic tx (orig↔gen match).")
    p.add_argument("--state-field", default="state_after",
                   help="Per-tx field holding the view-state snapshot.")
    p.add_argument("--output", type=Path, default=_SCRIPT_DIR / "synth_replay_depth_results.json")
    return p.parse_args()


def main():
    a = parse_args()
    for d, lbl in [(a.orig_dir, "--orig-dir"), (a.gen_dir, "--gen-dir")]:
        if not d.exists():
            print(f"ERROR: {lbl} not found: {d}"); sys.exit(1)

    orig_addrs = {p.name for p in a.orig_dir.iterdir() if p.is_dir()}
    gen_addrs  = {p.name for p in a.gen_dir.iterdir()  if p.is_dir()}
    common = sorted(orig_addrs & gen_addrs)
    print(f"orig={len(orig_addrs)} gen={len(gen_addrs)} common={len(common)}")

    rows = []
    no_orig = no_gen = no_cmp = 0
    for addr in common:
        orig = _load_original(a.orig_dir / addr, a.orig_glob)
        if orig is None:
            no_orig += 1; continue
        gen_samples = _load_generated_samples(a.gen_dir / addr, a.gen_glob)
        if not gen_samples:
            no_gen += 1; continue
        row = compare_contract(orig, gen_samples, a.hash_field, a.state_field)
        if row is None:
            no_cmp += 1; continue
        rows.append(row)

    print(f"  loaded: {len(rows)} comparable | "
          f"skipped: no_orig={no_orig} no_gen_samples={no_gen} no_tx_match={no_cmp}")

    agg = compute_aggregates(rows)
    a.output.write_text(json.dumps({"k": a.k, "aggregates": agg, "contracts": rows}, indent=2))
    print(f"Results written to {a.output}")
    print_summary(rows, agg, a.k)


if __name__ == "__main__":
    main()