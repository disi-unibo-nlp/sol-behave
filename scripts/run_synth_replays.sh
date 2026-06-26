#!/usr/bin/env bash
#
# run_synth_replays.sh  (multi-model)
# ===================================
# Synthetic pipeline per model:
#   0. make_sequences_hardhat.py    (regenerate synthetic txs — ONCE, model-independent)
#   1. replay_synth_transactions.py (synthetic txs × ORIGINAL contracts — ONCE)
#   2. replay_synth_generated.py    (synthetic txs × GENERATED samples — PER MODEL)
#
# Stage 0 and 1 are model-independent (original contracts + the DCR synthetic
# sequence), so they run once. Stage 2 runs per model, each pointing at that
# model's generated source dir and writing to a model-tagged results dir.
#
# Usage:
#   bash run_synth_replays.sh                    # all MODELS below
#   bash run_synth_replays.sh --force            # forwarded to replays
#   SKIP_REGEN=1 bash run_synth_replays.sh --force   # skip stages 0+1, only re-do gen replay
#
set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
DCR_GEN_DIR="/workdir/src/dcr_generation"
REPLAY_DIR="/workdir/src/dcr_replay"
DATASET="/workdir/src/dataset/contracts_dataset_1to20"
DCR_DIR="/workdir/src/dcr_generation/generated_dcr_1to20_Qwen3_6-27B-UD-Q4_K_XL_gguf"
SRC_GEN_DIR="/workdir/src/source_generation"
LOG_DIR="/workdir/scripts/logs"

K=1000
EXTRA_ARGS="${*:-}"
SKIP_REGEN="${SKIP_REGEN:-0}"   # set to 1 to skip stages 0 and 1

# ── Models: GENERATION-time GGUF filenames. Stage 2 runs once per model. ──────
# For each, generated source is expected at:
#   $SRC_GEN_DIR/generated_oneshot_<safe>
# and results are written to:
#   $REPLAY_DIR/synth_replay_results_<safe>
MODELS=(
  "Qwen3.6-27B-UD-Q4_K_XL.gguf"
  # "Qwen.Qwen3-Coder-30B-A3B-Instruct.Q4_K_M.gguf"
  # "gemma-4-31B-it-qat-UD-Q4_K_XL.gguf"
)

mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d_%H%M%S)"

echo "════════════════════════════════════════════════════════════"
echo "  SYNTH PIPELINE SCHEDULER (multi-model)"
echo "  start    : $(date)"
echo "  k        : $K"
echo "  models   : ${MODELS[*]}"
echo "  skip 0+1 : $SKIP_REGEN"
echo "  extra    : ${EXTRA_ARGS:-<none>}"
echo "════════════════════════════════════════════════════════════"

ORIG_OUT="$REPLAY_DIR/original_synth_replay_results"

# ── Stage 0+1: model-independent (run once unless SKIP_REGEN) ─────────────────
if [[ "$SKIP_REGEN" != "1" ]]; then
  echo ">>> [0] make_sequences_hardhat.py (regenerate all 250)"
  find "$DCR_DIR" -name transactions_synthetic.json -delete
  cd "$DCR_GEN_DIR"
  python3 make_sequences_hardhat.py 2>&1 | tee "$LOG_DIR/make_sequences_${TS}.log"

  echo ">>> [1] replay_synth_transactions.py (original contracts)  -> $ORIG_OUT"
  cd "$REPLAY_DIR"
  python3 replay_synth_transactions.py \
      --dataset    "$DATASET" \
      --dcr-dir    "$DCR_DIR" \
      --output-dir "$ORIG_OUT" \
      --k "$K" $EXTRA_ARGS \
      2>&1 | tee "$LOG_DIR/original_synth_${TS}.log"
else
  echo ">>> SKIP_REGEN=1 — reusing existing $ORIG_OUT"
fi

# ── Stage 2: per model ────────────────────────────────────────────────────────
cd "$REPLAY_DIR"
for MODEL_NAME in "${MODELS[@]}"; do
  SAFE=$(echo "$MODEL_NAME" | tr '. ' '__')
  GEN_SRC="$SRC_GEN_DIR/generated_oneshot_${SAFE}"
  GEN_OUT="$REPLAY_DIR/synth_replay_results_${SAFE}"

  echo "════════════════════════════════════════════════════════════"
  echo "  [2] GENERATED synth replay: $MODEL_NAME"
  echo "      generated src = $GEN_SRC"
  echo "      output dir    = $GEN_OUT"
  echo "════════════════════════════════════════════════════════════"

  if [[ ! -d "$GEN_SRC" ]]; then
    echo "  !! generated source dir not found: $GEN_SRC — skipping model"
    echo "     (expected generated_oneshot_<safe>; did source-gen run for this model?)"
    continue
  fi

  python3 replay_synth_generated.py \
      --dataset    "$DATASET" \
      --dcr-dir    "$DCR_DIR" \
      --generated  "$GEN_SRC" \
      --output-dir "$GEN_OUT" \
      --k "$K" $EXTRA_ARGS \
      2>&1 | tee "$LOG_DIR/generated_synth_${SAFE}_${TS}.log"

  echo "  -> done: $GEN_OUT"
  echo "  Analyse with:"
  echo "    python3 analyse_synth_replay_depth.py \\"
  echo "        --orig-dir $ORIG_OUT \\"
  echo "        --gen-dir  $GEN_OUT --k $K"
done

echo "════════════════════════════════════════════════════════════"
echo "  ALL DONE : $(date)"
echo "════════════════════════════════════════════════════════════"