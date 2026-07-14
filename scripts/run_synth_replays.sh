#!/usr/bin/env bash
# =============================================================================
# run_synth_replays.sh
# -----------------------------------------------------------------------------
# Orchestrates SYNTHETIC (DCR) transaction replay in two stages:
#
#   --sanity        replay_synth_transactions.py: synthetic txs × ORIGINAL
#                   contracts. The baseline the generated side is compared to.
#
#   (default)       replay_synth_generated.py: synthetic txs × GENERATED
#                   samples, per model.
#
# ORACLE PAIRING (the important part):
#
#   Matched (default): model X's generated sources are replayed against model
#                      X's OWN synthetic transactions.
#                        --generated generated_oneshot_<X>
#                        --dcr-dir   generated_dcr_1to20_<X>
#
#   --gemma-oracle:    every model's generated sources are replayed against
#                      GEMMA-31B's synthetic transactions (highest compile@10),
#                      isolating oracle quality across models.
#                        --generated generated_oneshot_<X>
#                        --dcr-dir   generated_dcr_1to20_<GEMMA>
#
# In --sanity the same pairing applies: matched replays originals against each
# model's DCR txs (one output set per model); --gemma-oracle replays originals
# against Gemma's DCR txs once.
#
# "safe" = model filename sanitized with: tr '. ' '__'
#
# Usage:
#   bash scripts/run_synth_replays.sh --sanity                 # originals, matched
#   bash scripts/run_synth_replays.sh --sanity --gemma-oracle  # originals vs Gemma oracle
#   bash scripts/run_synth_replays.sh                          # generated, matched
#   bash scripts/run_synth_replays.sh --gemma-oracle           # generated vs Gemma oracle
#   bash scripts/run_synth_replays.sh --only Codestral-22B-v0.1-Q4_K_M.gguf
#   K=1000 FORCE=1 bash scripts/run_synth_replays.sh
#
# Env overrides:
#   K            txs per contract        (default 1000)
#   DATASET      dataset dir             (default .../contracts_dataset_1to20)
#   DCR_ROOT     holds generated_dcr_*   (default src/dcr_generation)
#   DCR_PREFIX   DCR dir prefix          (default generated_dcr_1to20_)
#   GEN_ROOT     holds generated_oneshot (default src/source_generation)
#   GEN_PREFIX   generated dir prefix    (default generated_oneshot_)
#   OUT_ROOT     output root             (default src/dcr_replay)
#   WORK_DIR     hardhat workspace       (default .../hardhat_workspace_replay)
#   TX_TIMEOUT   per-tx timeout          (default 60)
#   FORCE=1 / RETRY_FAILED=1 / VERBOSE=1
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$REPO_ROOT/src"

SYNTH_ORIG="$SRC/dcr_replay/replay_synth_transactions.py"
SYNTH_GEN="$SRC/dcr_replay/replay_synth_generated.py"

# ---- Config ----------------------------------------------------------------
K="${K:-1000}"
DATASET="${DATASET:-$SRC/dataset/contracts_dataset_1to20}"
DCR_ROOT="${DCR_ROOT:-$SRC/dcr_generation}"
DCR_PREFIX="${DCR_PREFIX:-generated_dcr_1to20_}"
GEN_ROOT="${GEN_ROOT:-$SRC/source_generation}"
#GEN_PREFIX="${GEN_PREFIX:-generated_oneshot_}"
GEN_PREFIX="${GEN_PREFIX:-repaired_oneshot_}"
OUT_ROOT="${OUT_ROOT:-$SRC/dcr_replay}"
WORK_DIR="${WORK_DIR:-$SRC/dataset/hardhat_workspace_replay}"
TX_TIMEOUT="${TX_TIMEOUT:-60}"

PYFLAGS=""
[ "${FORCE:-0}" = "1" ]        && PYFLAGS="$PYFLAGS --force"
[ "${RETRY_FAILED:-0}" = "1" ] && PYFLAGS="$PYFLAGS --retry-failed"
[ "${VERBOSE:-0}" = "1" ]      && PYFLAGS="$PYFLAGS --verbose"

# The model whose DCR txs are the fixed oracle in --gemma-oracle mode.
GEMMA_MODEL="gemma-4-31B-it-qat-UD-Q4_K_XL.gguf"

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

# ---- Args ------------------------------------------------------------------
SANITY=0
GEMMA_ORACLE=0
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --sanity)       SANITY=1; shift ;;
    --gemma-oracle) GEMMA_ORACLE=1; shift ;;
    --only)         ONLY="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done

# ---- Preflight -------------------------------------------------------------
[ -f "$SYNTH_ORIG" ] || { echo "ERROR: not found: $SYNTH_ORIG"; exit 1; }
[ -f "$SYNTH_GEN"  ] || { echo "ERROR: not found: $SYNTH_GEN";  exit 1; }
[ -d "$DATASET"    ] || { echo "ERROR: dataset not found: $DATASET"; exit 1; }

GEMMA_SAFE="$(sanitize "$GEMMA_MODEL")"

echo "════════════════════════════════════════════════════════════"
echo "  run_synth_replays.sh"
echo "    K            = $K"
echo "    mode         = $([ "$SANITY" = 1 ] && echo SANITY || echo GENERATED)"
echo "    oracle       = $([ "$GEMMA_ORACLE" = 1 ] && echo "FIXED (Gemma-31B)" || echo MATCHED)"
echo "    dataset      = $DATASET"
echo "    dcr_root     = $DCR_ROOT"
echo "    gen_root     = $GEN_ROOT"
echo "    out_root     = $OUT_ROOT"
echo "    py flags     = ${PYFLAGS:-<none>}"
echo "════════════════════════════════════════════════════════════"

# Resolve the DCR dir to use for a given model, honouring --gemma-oracle.
dcr_dir_for() {
  local model_safe="$1"
  if [ "$GEMMA_ORACLE" = 1 ]; then
    echo "$DCR_ROOT/${DCR_PREFIX}${GEMMA_SAFE}"
  else
    echo "$DCR_ROOT/${DCR_PREFIX}${model_safe}"
  fi
}

# Tag appended to output dirs so matched vs gemma-oracle runs don't collide.
oracle_tag() {
  [ "$GEMMA_ORACLE" = 1 ] && echo "_gemmaoracle" || echo ""
}

overall_rc=0

# =============================================================================
# SANITY: synthetic txs × ORIGINAL contracts
# =============================================================================
if [ "$SANITY" = 1 ]; then
  # In gemma-oracle mode the oracle is model-independent, so run ONCE.
  # In matched mode, run once per model (each model's DCR txs differ).
  if [ "$GEMMA_ORACLE" = 1 ]; then
    dcr_dir="$(dcr_dir_for "$GEMMA_SAFE")"
    out_dir="$OUT_ROOT/original_synth_replay_results$(oracle_tag)"
    echo
    echo ">>> SANITY (Gemma oracle): originals × Gemma synthetic txs"
    echo "    dcr: $dcr_dir"
    echo "    out: $out_dir"
    [ -d "$dcr_dir" ] || { echo "  [err] DCR dir missing: $dcr_dir"; exit 1; }
    ( cd "$REPO_ROOT" && python3 "$SYNTH_ORIG" \
        --dataset "$DATASET" --dcr-dir "$dcr_dir" \
        --output-dir "$out_dir" --work-dir "$WORK_DIR" \
        --k "$K" --tx-timeout "$TX_TIMEOUT" \
        --results-file "$OUT_ROOT/synth_replay_results_orig$(oracle_tag).json" \
        $PYFLAGS )
    exit $?
  fi

  for model in "${MODELS[@]}"; do
    if [ -n "$ONLY" ] && [ "$model" != "$ONLY" ] && [ "$(sanitize "$model")" != "$ONLY" ]; then
      continue
    fi
    safe="$(sanitize "$model")"
    dcr_dir="$(dcr_dir_for "$safe")"
    out_dir="$OUT_ROOT/original_synth_replay_results_${safe}"
    echo
    echo "──────────────────────────────────────────────────────────"
    echo "  SANITY  MODEL: $model"
    echo "  dcr: $dcr_dir"
    echo "  out: $out_dir"
    echo "──────────────────────────────────────────────────────────"
    if [ ! -d "$dcr_dir" ]; then
      echo "  [skip] DCR dir not found: $dcr_dir"; overall_rc=1; continue
    fi
    ( cd "$REPO_ROOT" && python3 "$SYNTH_ORIG" \
        --dataset "$DATASET" --dcr-dir "$dcr_dir" \
        --output-dir "$out_dir" --work-dir "$WORK_DIR" \
        --k "$K" --tx-timeout "$TX_TIMEOUT" \
        --results-file "$OUT_ROOT/synth_replay_results_orig_${safe}.json" \
        $PYFLAGS )
    rc=$?; [ $rc -ne 0 ] && { echo "  [warn] exit $rc"; overall_rc=$rc; }
  done

  echo
  echo "  SANITY DONE (overall exit $overall_rc)"
  exit $overall_rc
fi

# =============================================================================
# GENERATED: synthetic txs × GENERATED samples, per model
# =============================================================================
for model in "${MODELS[@]}"; do
  if [ -n "$ONLY" ] && [ "$model" != "$ONLY" ] && [ "$(sanitize "$model")" != "$ONLY" ]; then
    continue
  fi

  safe="$(sanitize "$model")"
  gen_dir="$GEN_ROOT/${GEN_PREFIX}${safe}"
  dcr_dir="$(dcr_dir_for "$safe")"
  out_dir="$OUT_ROOT/synth_replay_results_${safe}$(oracle_tag)"

  echo
  echo "────────────────────────────────────────────────────────────"
  echo "  MODEL: $model"
  echo "  generated : $gen_dir"
  echo "  dcr (oracle): $dcr_dir"
  echo "  out       : $out_dir"
  echo "────────────────────────────────────────────────────────────"

  if [ ! -d "$gen_dir" ]; then
    echo "  [skip] generated dir not found: $gen_dir"; overall_rc=1; continue
  fi
  if [ ! -d "$dcr_dir" ]; then
    echo "  [skip] DCR dir not found: $dcr_dir"; overall_rc=1; continue
  fi

  results_file="$OUT_ROOT/synth_generated_replay_results_${safe}$(oracle_tag).json"

  ( cd "$REPO_ROOT" && python3 "$SYNTH_GEN" \
      --dataset "$DATASET" \
      --dcr-dir "$dcr_dir" \
      --generated "$gen_dir" \
      --output-dir "$out_dir" \
      --work-dir "$WORK_DIR" \
      --k "$K" --tx-timeout "$TX_TIMEOUT" \
      --results-file "$results_file" \
      $PYFLAGS )
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "  [warn] replay_synth_generated exited $rc for $model"; overall_rc=$rc
  fi
  echo "  -> per-sample: $out_dir/<address>/"
  echo "  -> aggregate:  $results_file"
done

echo
echo "════════════════════════════════════════════════════════════"
echo "  ALL MODELS DONE (overall exit $overall_rc)"
echo "════════════════════════════════════════════════════════════"
exit $overall_rc