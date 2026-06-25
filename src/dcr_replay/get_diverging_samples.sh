#!/usr/bin/env bash
# Collect original + generated samples for case-study contracts into
# per-address folders: ./<address>/source.sol  ./<address>/sample_i.sol
set -uo pipefail

# ── Addresses ────────────────────────────────────────────────────────────────
TRUSTY=0x0007039b77d22042afc1a9c3b3da11837b730000
RELAY=0xa5f565650890fba1824ee0f21ebbbf660a179934

DATASET=/workdir/src/dataset/contracts_dataset_1to20
GEN=/workdir/src/source_generation/generated_oneshot_Qwen3_6-27B-UD-Q4_K_XL_gguf

# Where to collect (current dir by default; override with OUT_ROOT=...)
OUT_ROOT="${OUT_ROOT:-.}"

# ── Helper: copy original source.sol (extract from source.json if needed) ─────
collect_one () {
  local addr=$1
  local dst="$OUT_ROOT/$addr"
  mkdir -p "$dst"
  echo "=== $addr -> $dst ==="

  # Original source: flat .sol, or extract from Standard-JSON source.json
  if [ -f "$DATASET/$addr/source.sol" ]; then
    cp "$DATASET/$addr/source.sol" "$dst/source.sol"
    echo "  original: source.sol (flat)"
  elif [ -f "$DATASET/$addr/source.json" ]; then
    python3 -c "
import json
d = json.load(open('$DATASET/$addr/source.json'))
srcs = d.get('sources', {})
parts = []
for fname, fc in srcs.items():
    parts.append(f'// ===== {fname} =====\n' + fc.get('content',''))
open('$dst/source.sol','w').write('\n\n'.join(parts) if parts else json.dumps(d))
print('  original: extracted from source.json (%d files)' % len(srcs))
"
  else
    echo "  !! no original source found"
  fi

  # Generated samples: copy every sample_i.sol (or extract sample_i.json)
  shopt -s nullglob
  local n=0
  for s in "$GEN/$addr"/sample_*.sol; do
    cp "$s" "$dst/$(basename "$s")"
    n=$((n+1))
  done
  for s in "$GEN/$addr"/sample_*.json; do
    base=$(basename "$s" .json)
    python3 -c "
import json
d = json.load(open('$s'))
srcs = d.get('sources', {})
parts = [f'// ===== {f} =====\n'+fc.get('content','') for f,fc in srcs.items()]
open('$dst/${base}.sol','w').write('\n\n'.join(parts) if parts else open('$s').read())
"
    n=$((n+1))
  done
  shopt -u nullglob
  echo "  generated: $n sample(s) copied"
  echo "  contents:"
  ls -1 "$dst" | sed 's/^/    /'
  echo
}

collect_one "$TRUSTY"
collect_one "$RELAY"

echo "Done. Folders created under: $OUT_ROOT/"