#!/usr/bin/env bash
# =============================================================================
# run_coverage_comparison.sh
# -----------------------------------------------------------------------------
# Runs compare_function_coverage.py for every model, comparing the functional
# coverage of HISTORICAL transactions against that model's DCR SYNTHETIC
# sequences. This is the mechanistic backing for the discriminability result:
# the synthetic oracle catches more defects because it exercises more functions.
#
# Historical coverage is model-independent (same transactions.json for all), so
# the HIST column is identical across models — only the SYNTH column varies.
# That is expected and useful: it shows which model's DCR extraction produces
# the best-covering test sequences.
#
# Output: src/evaluation/coverage/coverage_compare_<safe>.json
#
# Usage:
#   bash scripts/run_coverage_comparison.sh
#   bash scripts/run_coverage_comparison.sh --only gemma-4-31B-it-qat-UD-Q4_K_XL.gguf
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$REPO_ROOT/src"

EVAL_ROOT="${EVAL_ROOT:-$SRC/evaluation}"
COMPARER="${COMPARER:-$EVAL_ROOT/compare_function_coverage.py}"
OUT_DIR="${OUT_DIR:-$EVAL_ROOT/coverage}"

DATASET="${DATASET:-$SRC/dataset/contracts_dataset_1to20}"
DCR_ROOT="${DCR_ROOT:-$SRC/dcr_generation}"
DCR_PREFIX="${DCR_PREFIX:-generated_dcr_1to20_}"

MODELS=(
  "Qwen3.6-27B-UD-Q4_K_XL.gguf"
  "Qwen.Qwen3-Coder-30B-A3B-Instruct.Q4_K_M.gguf"
  "mistralai.Devstral-Small-2-24B-Instruct-2512.Q4_K_M.gguf"
  "gemma-4-31B-it-qat-UD-Q4_K_XL.gguf"
  "gemma-4-12B-it-qat-UD-Q4_K_XL.gguf"
  "ornith-1.0-35b-Q4_K_M.gguf"
  "Codestral-22B-v0.1-Q4_K_M.gguf"
)

sanitize() { echo "$1" | tr '. ' '__'; }

ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done

[ -f "$COMPARER" ] || { echo "ERROR: not found: $COMPARER"; exit 1; }
[ -d "$DATASET"  ] || { echo "ERROR: dataset not found: $DATASET"; exit 1; }
mkdir -p "$OUT_DIR"

echo "════════════════════════════════════════════════════════════"
echo "  run_coverage_comparison.sh"
echo "    comparer = $COMPARER"
echo "    dataset  = $DATASET"
echo "    dcr_root = $DCR_ROOT"
echo "    out_dir  = $OUT_DIR"
echo "════════════════════════════════════════════════════════════"

rc_all=0
for model in "${MODELS[@]}"; do
  if [ -n "$ONLY" ] && [ "$model" != "$ONLY" ] && [ "$(sanitize "$model")" != "$ONLY" ]; then
    continue
  fi
  safe="$(sanitize "$model")"
  dcr_dir="$DCR_ROOT/${DCR_PREFIX}${safe}"

  echo
  echo "────────────────────────────────────────────────────────────"
  echo "  MODEL: $model"
  echo "  dcr  : $dcr_dir"
  echo "────────────────────────────────────────────────────────────"

  if [ ! -d "$dcr_dir" ]; then
    echo "  [skip] DCR dir not found"; rc_all=1; continue
  fi

  ( cd "$REPO_ROOT" && python3 "$COMPARER" \
      --dataset "$DATASET" \
      --dcr-dir "$dcr_dir" \
      --label   "$model" \
      --output  "$OUT_DIR/coverage_compare_${safe}.json" )
  rc=$?
  [ $rc -ne 0 ] && { echo "  [warn] exit $rc"; rc_all=$rc; }
done

echo
echo "════════════════════════════════════════════════════════════"
echo "  DONE. Coverage comparisons in: $OUT_DIR"
ls -1 "$OUT_DIR" 2>/dev/null | sed 's/^/    /'
echo "════════════════════════════════════════════════════════════"
exit $rc_all