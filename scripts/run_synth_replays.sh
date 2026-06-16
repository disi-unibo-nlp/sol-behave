#!/usr/bin/env bash
#
# run_synth_replays.sh
# ====================
# Full synthetic pipeline, run in sequence:
#   0. make_sequences_hardhat.py      (regenerate synthetic txs for ALL 250)
#   1. replay_synth_transactions.py   (synthetic txs × original contracts)
#   2. replay_synth_generated.py      (synthetic txs × generated samples)
#
# Each stage runs only if the previous one succeeds. Logs go to
# /workdir/scripts/logs/.
#
# Usage:
#   bash /workdir/scripts/run_synth_replays.sh
#   bash /workdir/scripts/run_synth_replays.sh --force      # forwarded to replays
#
set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
DCR_GEN_DIR="/workdir/src/dcr_generation"
REPLAY_DIR="/workdir/src/dcr_replay"
DATASET="/workdir/src/dataset/contracts_dataset_1to20"
DCR_DIR="/workdir/src/dcr_generation/generated_dcr_1to20"
GENERATED="/workdir/src/source_generation/repaired_oneshot"
LOG_DIR="/workdir/scripts/logs"

K=1000
EXTRA_ARGS="${*:-}"   # forwarded to the two replay scripts (e.g. --force)

mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d_%H%M%S)"

MAKESEQ_LOG="$LOG_DIR/make_sequences.log"
ORIGINAL_LOG="$LOG_DIR/original_synth.log"
GENERATED_LOG="$LOG_DIR/generated_synth.log"

echo "════════════════════════════════════════════════════════════"
echo "  SYNTH PIPELINE SCHEDULER"
echo "  start    : $(date)"
echo "  dcr_gen  : $DCR_GEN_DIR"
echo "  replay   : $REPLAY_DIR"
echo "  k        : $K"
echo "  extra    : ${EXTRA_ARGS:-<none>}"
echo "  logs     : $LOG_DIR"
echo "════════════════════════════════════════════════════════════"

# ── 0. Regenerate synthetic transactions for ALL contracts ───────────────────
echo "Removing existing transactions"
find /workdir/src/dcr_generation/generated_dcr_1to20 -name transactions_synthetic.json -delete
echo ""
echo ">>> [0/2] make_sequences_hardhat.py (regenerate all 250)  →  $MAKESEQ_LOG"
echo ">>> start: $(date)"
START0=$(date +%s)

cd "$DCR_GEN_DIR"
if python3 make_sequences_hardhat.py \
        2>&1 | tee "$MAKESEQ_LOG"; then
    END0=$(date +%s)
    echo ">>> [0/2] COMPLETATO in $(( (END0 - START0) / 60 ))m $(( (END0 - START0) % 60 ))s"
    cp "$MAKESEQ_LOG" "$LOG_DIR/make_sequences_${TS}.log"
else
    echo ">>> [0/2] FALLITO — interrompo, non eseguo i replay."
    exit 1
fi

cd "$REPLAY_DIR"

# ── 1. Original contracts × synthetic txs ────────────────────────────────────
echo ""
echo ">>> [1/2] replay_synth_transactions.py  →  $ORIGINAL_LOG"
echo ">>> start: $(date)"
START1=$(date +%s)

if python3 replay_synth_transactions.py \
        --dataset    "$DATASET" \
        --dcr-dir    "$DCR_DIR" \
        --output-dir ./synth_replay_results \
        --k "$K" \
        $EXTRA_ARGS \
        2>&1 | tee "$ORIGINAL_LOG"; then
    END1=$(date +%s)
    echo ">>> [1/2] COMPLETATO in $(( (END1 - START1) / 60 ))m $(( (END1 - START1) % 60 ))s"
    cp "$ORIGINAL_LOG" "$LOG_DIR/original_synth_${TS}.log"
else
    echo ">>> [1/2] FALLITO — interrompo, non eseguo il secondo script."
    exit 1
fi

# ── 2. Generated samples × synthetic txs ─────────────────────────────────────
echo ""
echo ">>> [2/2] replay_synth_generated.py  →  $GENERATED_LOG"
echo ">>> start: $(date)"
START2=$(date +%s)

if python3 replay_synth_generated.py \
        --dataset    "$DATASET" \
        --dcr-dir    "$DCR_DIR" \
        --generated  "$GENERATED" \
        --output-dir ./synth_replay_results_generated \
        --k "$K" \
        $EXTRA_ARGS \
        2>&1 | tee "$GENERATED_LOG"; then
    END2=$(date +%s)
    echo ">>> [2/2] COMPLETATO in $(( (END2 - START2) / 60 ))m $(( (END2 - START2) % 60 ))s"
    cp "$GENERATED_LOG" "$LOG_DIR/generated_synth_${TS}.log"
else
    echo ">>> [2/2] FALLITO."
    exit 1
fi

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  TUTTO COMPLETATO"
echo "  end : $(date)"
echo "  log : $MAKESEQ_LOG"
echo "        $ORIGINAL_LOG"
echo "        $GENERATED_LOG"
echo "════════════════════════════════════════════════════════════"