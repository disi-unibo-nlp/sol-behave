#!/usr/bin/env bash
# =============================================================================
# run_depth_analyses.sh
# -----------------------------------------------------------------------------
# Runs the generalized depth analyzer across EVERY replay output produced so
# far, keeping the three analysis families in SEPARATE output folders.
#
# Analyzer:  src/evaluation/analyse_replay_depth.py
#
# Output folders (all under src/evaluation/):
#   depth_historical/            replay_depth_HISTORICAL_<safe>.json
#   depth_synthetic/             replay_depth_SYNTHETIC_<safe>.json
#   depth_synthetic_gemma/       replay_depth_SYNTHETIC_GEMMA_<safe>.json
#
# The three families:
#
#  1. HISTORICAL   originals (historical replay) vs generated samples
#       orig: transaction_replay/original_historical_replay_results/
#       gen : transaction_replay/generated_historical_replay_results_<safe>/
#             (falls back to source_generation/repaired_oneshot_<safe>/ if the
#              dedicated output dir does not exist)
#       globs: "replay_result_*.json" / "replay_sample_*result*.json"
#
#  2. SYNTHETIC (matched)   each model's generated vs its OWN DCR synthetic txs
#       orig: dcr_replay/original_synth_replay_results_<safe>/
#       gen : dcr_replay/synth_replay_results_<safe>/
#
#  3. SYNTHETIC (gemma oracle)  every model's generated vs GEMMA's synthetic txs
#       orig: dcr_replay/original_synth_replay_results_gemmaoracle/
#       gen : dcr_replay/synth_replay_results_<safe>_gemmaoracle/
#
# Missing input dirs are skipped with a warning, so this is safe to run even
# when only some replay stages are complete.
#
# Usage:
#   bash scripts/run_depth_analyses.sh                # all families, all models
#   bash scripts/run_depth_analyses.sh --only-hist
#   bash scripts/run_depth_analyses.sh --only-synth
#   bash scripts/run_depth_analyses.sh --only-gemma
#   bash scripts/run_depth_analyses.sh --model Qwen3.6-27B-UD-Q4_K_XL.gguf
#
# Env overrides:
#   K              default 1000
#   EVAL_ROOT      default src/evaluation
#   ANALYZER       default $EVAL_ROOT/analyse_replay_depth.py
#   OUT_HIST / OUT_SYNTH / OUT_GEMMA   the three output dirs
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$REPO_ROOT/src"

K="${K:-1000}"

# ---- Analyzer + output folders ---------------------------------------------
EVAL_ROOT="${EVAL_ROOT:-$SRC/evaluation}"
ANALYZER="${ANALYZER:-$EVAL_ROOT/analyse_replay_depth.py}"

OUT_HIST="${OUT_HIST:-$EVAL_ROOT/depth_historical}"
OUT_SYNTH="${OUT_SYNTH:-$EVAL_ROOT/depth_synthetic}"
OUT_GEMMA="${OUT_GEMMA:-$EVAL_ROOT/depth_synthetic_gemma}"

# ---- Historical input paths -------------------------------------------------
HIST_ORIG="${HIST_ORIG:-$SRC/transaction_replay/original_historical_replay_results}"
HIST_GEN_ROOT="${HIST_GEN_ROOT:-$SRC/transaction_replay}"
HIST_GEN_PREFIX="${HIST_GEN_PREFIX:-generated_historical_replay_results_}"
# Fallback: historical generated outputs left next to the samples
HIST_GEN_ALT_ROOT="${HIST_GEN_ALT_ROOT:-$SRC/source_generation}"
HIST_GEN_ALT_PREFIX="${HIST_GEN_ALT_PREFIX:-repaired_oneshot_}"

# ---- Synthetic input paths --------------------------------------------------
SYN_ROOT="${SYN_ROOT:-$SRC/dcr_replay}"
SYN_ORIG_PREFIX="${SYN_ORIG_PREFIX:-original_synth_replay_results_}"
SYN_GEN_PREFIX="${SYN_GEN_PREFIX:-synth_replay_results_}"
# The gemma-oracle family compares every model's generated samples against
# GEMMA's synthetic transactions. The correct ORIGINAL baseline for those same
# transactions is the originals replayed on Gemma's synthetic txs — i.e. Gemma's
# own matched-sanity output. No separate "_gemmaoracle" original run is needed.
GEMMA_SAFE="$(echo "gemma-4-31B-it-qat-UD-Q4_K_XL.gguf" | tr '. ' '__')"
SYN_ORIG_GEMMA="${SYN_ORIG_GEMMA:-$SYN_ROOT/${SYN_ORIG_PREFIX}${GEMMA_SAFE}}"

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

DO_HIST=1; DO_SYNTH=1; DO_GEMMA=1; ONLY_MODEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --only-hist)  DO_SYNTH=0; DO_GEMMA=0; shift ;;
    --only-synth) DO_HIST=0;  DO_GEMMA=0; shift ;;
    --only-gemma) DO_HIST=0;  DO_SYNTH=0; shift ;;
    --model)      ONLY_MODEL="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done

[ -f "$ANALYZER" ] || { echo "ERROR: analyzer not found: $ANALYZER"; exit 1; }

mkdir -p "$OUT_HIST" "$OUT_SYNTH" "$OUT_GEMMA"

echo "════════════════════════════════════════════════════════════"
echo "  run_depth_analyses.sh   (K=$K)"
echo "    analyzer   = $ANALYZER"
echo "    out hist   = $OUT_HIST"
echo "    out synth  = $OUT_SYNTH"
echo "    out gemma  = $OUT_GEMMA"
echo "    families   = hist:$DO_HIST synth:$DO_SYNTH gemma:$DO_GEMMA"
echo "════════════════════════════════════════════════════════════"

run_one() {
  local label="$1" orig="$2" gen="$3" out="$4" oglob="$5" gglob="$6"
  if [ ! -d "$orig" ]; then echo "  [skip] missing orig: $orig"; return 1; fi
  if [ ! -d "$gen"  ]; then echo "  [skip] missing gen : $gen";  return 1; fi
  echo
  echo "  ── $label"
  echo "     orig: $orig"
  echo "     gen : $gen"
  ( cd "$REPO_ROOT" && python3 "$ANALYZER" \
      --orig-dir "$orig" --gen-dir "$gen" \
      --orig-glob "$oglob" --gen-glob "$gglob" \
      --hash-field hash --state-field state_after \
      --k "$K" --label "$label" --output "$out" )
  local rc=$?
  [ $rc -eq 0 ] && echo "     -> $out" || echo "     [warn] exit $rc"
  return $rc
}

for model in "${MODELS[@]}"; do
  if [ -n "$ONLY_MODEL" ] && [ "$model" != "$ONLY_MODEL" ] \
     && [ "$(sanitize "$model")" != "$ONLY_MODEL" ]; then continue; fi
  safe="$(sanitize "$model")"

  echo
  echo "════════════════════════════════════════════════════════════"
  echo "  MODEL: $model"
  echo "════════════════════════════════════════════════════════════"

  # ---- 1. HISTORICAL -> depth_historical/ ----------------------------------
  if [ "$DO_HIST" = 1 ]; then
    hist_gen="$HIST_GEN_ROOT/${HIST_GEN_PREFIX}${safe}"
    if [ ! -d "$hist_gen" ]; then
      hist_gen="$HIST_GEN_ALT_ROOT/${HIST_GEN_ALT_PREFIX}${safe}"
    fi
    run_one "HISTORICAL[$safe]" \
      "$HIST_ORIG" "$hist_gen" \
      "$OUT_HIST/replay_depth_HISTORICAL_${safe}.json" \
      "replay_result_*.json" "replay_sample_*result*.json"
  fi

  # ---- 2. SYNTHETIC matched -> depth_synthetic/ ----------------------------
  if [ "$DO_SYNTH" = 1 ]; then
    run_one "SYNTHETIC[$safe]" \
      "$SYN_ROOT/${SYN_ORIG_PREFIX}${safe}" \
      "$SYN_ROOT/${SYN_GEN_PREFIX}${safe}" \
      "$OUT_SYNTH/replay_depth_SYNTHETIC_${safe}.json" \
      "*synth*result*.json" "*sample*result*.json"
  fi

  # ---- 3. SYNTHETIC gemma oracle -> depth_synthetic_gemma/ -----------------
  if [ "$DO_GEMMA" = 1 ]; then
    run_one "SYNTHETIC-GEMMA[$safe]" \
      "$SYN_ORIG_GEMMA" \
      "$SYN_ROOT/${SYN_GEN_PREFIX}${safe}_gemmaoracle" \
      "$OUT_GEMMA/replay_depth_SYNTHETIC_GEMMA_${safe}.json" \
      "*synth*result*.json" "*sample*result*.json"
  fi
done

echo
echo "════════════════════════════════════════════════════════════"
echo "  DONE."
for d in "$OUT_HIST" "$OUT_SYNTH" "$OUT_GEMMA"; do
  n=$(ls -1 "$d" 2>/dev/null | wc -l)
  echo "    $d  ($n files)"
  ls -1 "$d" 2>/dev/null | sed 's/^/        /'
done
echo "════════════════════════════════════════════════════════════"