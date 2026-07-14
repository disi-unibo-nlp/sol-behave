#!/usr/bin/env python3
"""
build_dcr_annotation_sample.py
==============================

Builds the annotation package for validating LLM-extracted DCR graphs.

Selects a STRATIFIED sample of contracts (not merely the simple ones — a sample
biased toward easy contracts would say nothing about extraction quality where it
matters) and, for each, exports everything an annotator needs:

  <out>/<NN>_<ContractName>_<addr8>/
      source.sol            the original contract (flattened if Standard-JSON)
      dcr_graph.json        the DCR graph extracted by the reference model
      relations.md          human-readable list of extracted relations, with
                            the events they connect and the guards, one block
                            per relation, ready to be marked correct/incorrect
      events.md             the extracted events with their attributes
      README.md             what to do for this contract

  <out>/ANNOTATION_MANIFEST.csv   one row per relation across all contracts —
                                  this is the sheet the annotators fill in
  <out>/SAMPLE_SUMMARY.json      strata, seed, forced inclusions, type coverage

Sampling design
---------------
  * Stratified by function count into three tiers (default 1-5 / 6-12 / 13-20),
    so precision/recall can be reported per complexity tier.
  * Case-study contracts are force-included (a reviewer will want assurance that
    the graphs behind the paper's examples are sound).
  * Within tiers, sampling is biased to cover all four relation types: purely
    random sampling can miss `response` relations entirely (they are ~7% of the
    corpus), leaving that type unmeasurable.
  * Deterministic given --seed.

Usage
-----
  python3 build_dcr_annotation_sample.py \
      --dataset /workdir/src/dataset/contracts_dataset_1to20 \
      --dcr-dir /workdir/src/dcr_generation/generated_dcr_1to20_gemma-4-31B-it-qat-UD-Q4_K_XL_gguf \
      --out     /workdir/src/evaluation/dcr_annotation \
      --n 30 --seed 42
"""

import argparse
import csv
import json
import random
import shutil
from collections import Counter
from pathlib import Path
from typing import Optional

# Contracts the paper leans on — always include them in the sample.
FORCED = {
    "0x0007039b77d22042afc1a9c3b3da11837b730000": "Trusty (case study)",
    "0xa5f565650890fba1824ee0f21ebbbf660a179934": "RelayReceiver (case study)",
    "0xa9929db24e9837f622f94b82f2abe78aa971288f": "OHMI (flagship)",
}

REL_TYPES = ["condition", "response", "exclusion", "inclusion"]

REL_SEMANTICS = {
    "condition": "A must execute BEFORE B can execute. "
                 "(Is B genuinely blocked until A has run?)",
    "response":  "Executing A OBLIGATES B to eventually execute. "
                 "(Is B a required follow-up of A in the contract's intended process?)",
    "exclusion": "Executing A permanently DISABLES B. "
                 "(After A, can B never succeed again?)",
    "inclusion": "Executing A RE-ENABLES B. "
                 "(Does A restore B's ability to execute?)",
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_source(contract_dir: Path) -> Optional[str]:
    sol = contract_dir / "source.sol"
    if sol.exists():
        return sol.read_text(errors="replace")
    jsn = contract_dir / "source.json"
    if jsn.exists():
        try:
            d = json.loads(jsn.read_text())
            srcs = d.get("sources", {})
            if srcs:
                return "\n\n".join(
                    f"// ===== {f} =====\n" + fc.get("content", "")
                    for f, fc in srcs.items()
                )
            return jsn.read_text(errors="replace")
        except Exception:
            return jsn.read_text(errors="replace")
    return None


def n_state_changing_fns(abi: list) -> int:
    return sum(
        1 for e in abi
        if e.get("type") == "function"
        and e.get("stateMutability", "nonpayable") not in ("view", "pure")
    )


def contract_name(contract_dir: Path, address: str) -> str:
    p = contract_dir / "metadata.json"
    if p.exists():
        try:
            return json.loads(p.read_text()).get("contract_name", address[:10])
        except Exception:
            pass
    return address[:10]


# ---------------------------------------------------------------------------
# Candidate assembly
# ---------------------------------------------------------------------------

def build_candidates(dataset: Path, dcr_dir: Path) -> list[dict]:
    out = []
    for cdir in sorted(d for d in dataset.iterdir() if d.is_dir()):
        addr = cdir.name
        dcr_path = dcr_dir / addr / "source_dcr.json"
        if not dcr_path.exists():
            continue
        try:
            dcr = json.loads(dcr_path.read_text())
        except Exception:
            continue

        rels = dcr.get("relations", [])
        events = dcr.get("events", [])
        if not rels or not events:
            continue   # nothing to annotate

        src = load_source(cdir)
        if not src:
            continue

        abi_path = cdir / "abi.json"
        abi = []
        if abi_path.exists():
            try:
                abi = json.loads(abi_path.read_text())
            except Exception:
                pass
        n_fns = n_state_changing_fns(abi)
        if n_fns == 0:
            continue

        type_counts = Counter(r.get("type", "?") for r in rels)

        # coverage.json (if present) — lets us balance good/bad coverage cases
        cov_ratio = None
        cov_path = dcr_dir / addr / "coverage.json"
        if cov_path.exists():
            try:
                cov = json.loads(cov_path.read_text())
                cov_ratio = cov.get("function_coverage", {}).get("coverage_ratio")
            except Exception:
                pass

        out.append({
            "address":     addr,
            "name":        contract_name(cdir, addr),
            "n_fns":       n_fns,
            "n_events":    len(events),
            "n_relations": len(rels),
            "types":       dict(type_counts),
            "cov_ratio":   cov_ratio,
            "src_len":     len(src),
            "source":      src,
            "dcr":         dcr,
        })
    return out


def tier_of(n_fns: int, bounds: tuple[int, int]) -> int:
    lo, hi = bounds
    if n_fns <= lo:
        return 0
    if n_fns <= hi:
        return 1
    return 2


# ---------------------------------------------------------------------------
# Stratified, type-balanced selection
# ---------------------------------------------------------------------------

def select(candidates: list[dict], n: int, seed: int,
           bounds: tuple[int, int], min_per_type: int) -> tuple[list[dict], dict]:
    rng = random.Random(seed)
    by_addr = {c["address"].lower(): c for c in candidates}

    picked: list[dict] = []
    picked_addrs: set[str] = set()

    # 1. forced case-study contracts
    forced_hit, forced_miss = [], []
    for addr, why in FORCED.items():
        c = by_addr.get(addr.lower())
        if c:
            c = dict(c); c["forced_reason"] = why
            picked.append(c); picked_addrs.add(addr.lower())
            forced_hit.append(f"{c['name']} ({why})")
        else:
            forced_miss.append(f"{addr} ({why})")

    # 2. stratify the rest
    tiers = {0: [], 1: [], 2: []}
    for c in candidates:
        if c["address"].lower() in picked_addrs:
            continue
        tiers[tier_of(c["n_fns"], bounds)].append(c)
    for t in tiers:
        rng.shuffle(tiers[t])

    remaining = n - len(picked)
    per_tier = [remaining // 3] * 3
    for i in range(remaining - sum(per_tier)):
        per_tier[i] += 1

    # running tally of relation types, so we can steer toward under-covered ones
    tally = Counter()
    for c in picked:
        tally.update(c["types"])

    def deficit_score(c: dict) -> int:
        """How much this contract helps cover under-represented relation types."""
        s = 0
        for t in REL_TYPES:
            need = max(0, min_per_type - tally.get(t, 0))
            if need > 0:
                s += min(need, c["types"].get(t, 0))
        return s

    def _take(pool: list[dict]) -> Optional[dict]:
        """Pop the contract that best reduces the relation-type deficit."""
        if not pool:
            return None
        pool.sort(key=deficit_score, reverse=True)
        best = pool[0]
        if deficit_score(best) == 0:
            best = pool[rng.randrange(len(pool))]   # no deficit → pure random
        pool.remove(best)
        return best

    for t_idx in range(3):
        pool = tiers[t_idx]
        for _ in range(per_tier[t_idx]):
            c = _take(pool)
            if c is None:
                break              # this tier is exhausted; topped up below
            picked.append(c)
            picked_addrs.add(c["address"].lower())
            tally.update(c["types"])

    # A tier can run dry (few contracts have 13+ functions AND a usable graph).
    # Redistribute the shortfall across whatever candidates remain, so the sample
    # size is always exactly n. Note which tiers were short, so it is visible.
    short_tiers = [t for t in range(3) if not tiers[t]]
    leftovers = [c for t in tiers for c in tiers[t]]
    while len(picked) < n and leftovers:
        c = _take(leftovers)
        if c is None:
            break
        picked.append(c)
        picked_addrs.add(c["address"].lower())
        tally.update(c["types"])

    meta = {
        "seed": seed,
        "n_requested": n,
        "n_selected": len(picked),
        "tiers_exhausted": short_tiers,   # tiers with too few eligible contracts
        "tier_bounds": {"tier0": f"1-{bounds[0]} fns",
                        "tier1": f"{bounds[0]+1}-{bounds[1]} fns",
                        "tier2": f"{bounds[1]+1}+ fns"},
        "forced_included": forced_hit,
        "forced_missing":  forced_miss,
        "relation_type_coverage": dict(tally),
        "min_per_type_target": min_per_type,
    }
    return picked, meta


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def relations_md(c: dict) -> str:
    dcr = c["dcr"]
    events = {e["id"]: e for e in dcr.get("events", [])}
    L = [f"# Relations — {c['name']}  (`{c['address']}`)", ""]
    L.append(f"Extracted events: {len(events)}  |  relations: {c['n_relations']}")
    L.append("")
    L.append("For each relation below, decide whether it is **justified by the "
             "contract source**. Mark it in ANNOTATION_MANIFEST.csv.")
    L.append("")
    for i, r in enumerate(dcr.get("relations", []), 1):
        rtype = r.get("type", "?")
        src_e = events.get(r.get("from"), {})
        tgt_e = events.get(r.get("to"), {})
        L.append(f"## R{i}. `{rtype}`  {r.get('from')} → {r.get('to')}")
        L.append("")
        L.append(f"**Claim:** {REL_SEMANTICS.get(rtype, '?')}")
        L.append("")
        L.append(f"- **A** (`{r.get('from')}`): "
                 f"fn=`{src_e.get('function_name','?')}` "
                 f"role={src_e.get('role','?')} "
                 f"payable={src_e.get('payable','?')} — {src_e.get('label','')}")
        L.append(f"- **B** (`{r.get('to')}`): "
                 f"fn=`{tgt_e.get('function_name','?')}` "
                 f"role={tgt_e.get('role','?')} "
                 f"payable={tgt_e.get('payable','?')} — {tgt_e.get('label','')}")
        guard = r.get("guard", "")
        L.append(f"- **Guard:** `{guard}`" if guard else "- **Guard:** _(none)_")
        L.append("")
    return "\n".join(L)


def events_md(c: dict) -> str:
    L = [f"# Events — {c['name']}  (`{c['address']}`)", ""]
    L.append("| id | function | role | payable | repeatable | label |")
    L.append("|---|---|---|---|---|---|")
    for e in c["dcr"].get("events", []):
        L.append(f"| `{e.get('id')}` | `{e.get('function_name','')}` | "
                 f"{e.get('role','')} | {e.get('payable','')} | "
                 f"{e.get('repeatable','')} | {e.get('label','')} |")
    L.append("")
    L.append("**Also check:** are any state-changing functions of the contract "
             "MISSING from this event list? Note them in the manifest "
             "(`missing_events` sheet) — this is what lets us measure recall.")
    return "\n".join(L)


def readme_md(c: dict, idx: int, total: int) -> str:
    """Per-contract fact sheet. Instructions live in ANNOTATION_GUIDE.md."""
    forced = c.get("forced_reason")
    forced_line = f"\n- *Included because:* {forced}" if forced else ""
    return f"""# Contract {idx} of {total} — {c['name']}

**Copy this into the spreadsheets:**

| field | value |
|---|---|
| `contract_idx`  | `{idx}` |
| `contract_name` | `{c['name']}` |
| `address`       | `{c['address']}` |

- State-changing functions: **{c['n_fns']}**
- Rules the model extracted: **{c['n_relations']}**  {c['types']}
- Functions the model paid attention to: **{c['n_events']}**{forced_line}

---

Read `source.sol` first, then `relations.md`.
Full instructions: **`ANNOTATION_GUIDE.md`** in the top-level folder.
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--dcr-dir", type=Path, required=True, dest="dcr_dir")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--tier-bounds", type=int, nargs=2, default=[5, 12],
                    metavar=("LO", "HI"),
                    help="Function-count tier boundaries (default 5 12 → 1-5 / 6-12 / 13+).")
    ap.add_argument("--min-per-type", type=int, default=15,
                    help="Target minimum relations of EACH type in the sample.")
    args = ap.parse_args()

    for d, lbl in [(args.dataset, "--dataset"), (args.dcr_dir, "--dcr-dir")]:
        if not d.exists():
            raise SystemExit(f"ERROR: {lbl} not found: {d}")

    print("Scanning candidates ...")
    cands = build_candidates(args.dataset, args.dcr_dir)
    print(f"  {len(cands)} contracts have both a source and a non-empty DCR graph")
    if not cands:
        raise SystemExit("Nothing to sample.")

    picked, meta = select(cands, args.n, args.seed,
                          tuple(args.tier_bounds), args.min_per_type)

    # --- export -------------------------------------------------------------
    out = args.out
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    rows = []
    for i, c in enumerate(sorted(picked, key=lambda x: (x["n_fns"], x["name"])), 1):
        d = out / f"{i:02d}_{c['name'][:24].replace('/', '_')}_{c['address'][2:10]}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "source.sol").write_text(c["source"])
        (d / "dcr_graph.json").write_text(json.dumps(c["dcr"], indent=2))
        (d / "relations.md").write_text(relations_md(c))
        (d / "events.md").write_text(events_md(c))
        (d / "README.md").write_text(readme_md(c, i, len(picked)))

        for j, r in enumerate(c["dcr"].get("relations", []), 1):
            rows.append({
                "contract_idx":  i,
                "contract_name": c["name"],
                "address":       c["address"],
                "n_fns":         c["n_fns"],
                "relation_id":   f"R{j}",
                "type":          r.get("type", ""),
                "from":          r.get("from", ""),
                "to":            r.get("to", ""),
                "guard":         r.get("guard", ""),
                "verdict":       "",     # correct | incorrect | unsure
                "reason":        "",     # one line if incorrect
                "annotator":     "",
            })

    # the sheet the annotators fill in
    man = out / "ANNOTATION_MANIFEST.csv"
    with man.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # a second sheet for recall (missing events / missing relations)
    miss = out / "MISSING_RELATIONS.csv"
    with miss.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["contract_idx", "contract_name", "address",
                    "missing_kind (event|relation)", "type (if relation)",
                    "from", "to", "why_it_should_exist", "annotator"])

    (out / "SAMPLE_SUMMARY.json").write_text(json.dumps({
        **meta,
        "dcr_dir": str(args.dcr_dir),
        "contracts": [
            {k: c[k] for k in
             ("address", "name", "n_fns", "n_events", "n_relations", "types", "cov_ratio")}
            for c in picked
        ],
    }, indent=2))

    # NOTE: the top-level README is intentionally NOT generated.
    # Place ANNOTATION_GUIDE.md in the output folder by hand — it is the
    # instructions document the annotators actually read.
    guide = out / "ANNOTATION_GUIDE.md"
    if not guide.exists():
        print(f"\n  [!] Remember to copy ANNOTATION_GUIDE.md into {out}")

    # --- report -------------------------------------------------------------
    print(f"\nSelected {len(picked)} contracts, {len(rows)} relations to annotate.")
    print("\n  tier distribution:")
    tc = Counter(tier_of(c["n_fns"], tuple(args.tier_bounds)) for c in picked)
    for t in range(3):
        print(f"    tier {t} ({meta['tier_bounds'][f'tier{t}']}): {tc.get(t,0)} contracts")
    print("\n  relation-type coverage in sample:")
    for t in REL_TYPES:
        n = meta["relation_type_coverage"].get(t, 0)
        flag = "" if n >= args.min_per_type else f"  ← below target ({args.min_per_type})"
        print(f"    {t:<10} {n}{flag}")
    if meta["forced_missing"]:
        print("\n  [warn] forced contracts NOT found (no DCR graph?):")
        for m in meta["forced_missing"]:
            print(f"    {m}")
    print(f"\nPackage written to {out}")
    print(f"  annotate: {man}")
    print(f"  recall:   {miss}")


if __name__ == "__main__":
    main()