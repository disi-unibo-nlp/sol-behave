#!/bin/bash
# run_crawl_batches.sh
#
# Crawl Ethereum contracts from 2023-01-01 to present in batches of 100k blocks.
#
# Block estimates:
#   2020-01-01 ~  9193266
#   2021-01-01 ~ 11565019
#   2022-01-01 ~ 13916166
#   2023-01-01 ~ 16308190
#   2024-01-01 ~ 18908895
#   2025-01-01 ~ 21525700
#   2026-05-15 ~ 22000000  (approximate current)
#
# Usage:
#   bash run_crawl_batches.sh
#   BATCH_SIZE=50000 START=16308190 END=22000000 bash run_crawl_batches.sh
#   RESUME=1 bash run_crawl_batches.sh   # skip already-completed batches

set -e

START=${START:-16308190}     # 2023-01-01
END=${END:-22000000}         # ~2026-05-15 current
BATCH_SIZE=${BATCH_SIZE:-100000}   # ~2 weeks per batch

SCRIPT_DIR="/workdir/src/dataset"
CAND_DIR="/workdir/src/dataset/candidates"
OUT_DIR="${OUT_DIR:-/workdir/src/dataset/contracts_dataset}"
BLACKLIST="${BLACKLIST:-/workdir/src/dataset/blacklist.json}"
BATCH_LOG="${BATCH_LOG:-/workdir/src/dataset/batch_progress.log}"

RESUME=${RESUME:-0}

echo "========================================"
echo "Crawl batches: $START → $END"
echo "Batch size:    $BATCH_SIZE blocks (~2 weeks)"
echo "Output dir:    $OUT_DIR"
echo "Blacklist:     $BLACKLIST"
echo "Resume mode:   $RESUME"
echo "========================================"

mkdir -p "$CAND_DIR" "$OUT_DIR"
touch "$BATCH_LOG"

# Count total batches
TOTAL_BATCHES=$(( (END - START + BATCH_SIZE - 1) / BATCH_SIZE ))
BATCH_NUM=0
FAILED_BATCHES=0

CURRENT=$START
while [ $CURRENT -lt $END ]; do
    BATCH_END=$(( CURRENT + BATCH_SIZE ))
    if [ $BATCH_END -gt $END ]; then
        BATCH_END=$END
    fi
    BATCH_NUM=$(( BATCH_NUM + 1 ))

    CAND_FILE="$CAND_DIR/candidates_${CURRENT}_${BATCH_END}.json"

    # Resume: skip if already logged as DONE
    if [ "$RESUME" = "1" ]; then
        if grep -q "DONE:${CURRENT}_${BATCH_END}" "$BATCH_LOG" 2>/dev/null; then
            echo "[${BATCH_NUM}/${TOTAL_BATCHES}] SKIP (already done): $CURRENT → $BATCH_END"
            CURRENT=$BATCH_END
            continue
        fi
    fi

    echo ""
    echo "========================================"
    echo "[${BATCH_NUM}/${TOTAL_BATCHES}] Batch: $CURRENT → $BATCH_END"
    echo "  Started: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "========================================"

    # ── 1. Seed candidates ────────────────────────────────────────────────
    echo "[1/2] Seeding candidates $CURRENT → $BATCH_END ..."
    if python3 "$SCRIPT_DIR/seed_from_etherscan.py" \
        --start-block $CURRENT \
        --end-block   $BATCH_END \
        --min-tx      200 \
        --max-candidates 500 \
        --blacklist   "$BLACKLIST" \
        --output      "$CAND_FILE"; then
        echo "  Seeding OK"
    else
        echo "  Seeding FAILED for batch $CURRENT → $BATCH_END — skipping"
        echo "FAIL_SEED:${CURRENT}_${BATCH_END}  $(date)" >> "$BATCH_LOG"
        FAILED_BATCHES=$(( FAILED_BATCHES + 1 ))
        CURRENT=$BATCH_END
        continue
    fi

    # Check if any candidates were found
    N_CANDS=0
    if [ -f "$CAND_FILE" ]; then
        N_CANDS=$(python3 -c "
import json, sys
d = json.load(open('$CAND_FILE'))
print(len(d.get('candidates', [])))
" 2>/dev/null || echo 0)
    fi

    if [ "$N_CANDS" = "0" ]; then
        echo "  No candidates found — skipping crawl"
        echo "DONE_EMPTY:${CURRENT}_${BATCH_END}  $(date)" >> "$BATCH_LOG"
        CURRENT=$BATCH_END
        continue
    fi
    echo "  Found $N_CANDS candidates"

    # ── 2. Crawl ──────────────────────────────────────────────────────────
    echo "[2/2] Crawling $N_CANDS candidates ..."
    if python3 "$SCRIPT_DIR/crawl.py" \
        --from-file   "$CAND_FILE" \
        --output-dir  "$OUT_DIR" \
        --work-dir    "$OUT_DIR/../hardhat_workspace_crawl" \
        --blacklist   "$BLACKLIST" \
        --k           200 \
        --pass-threshold 0.5; then
        echo "  Crawl OK"
        echo "DONE:${CURRENT}_${BATCH_END}  $(date)  candidates=$N_CANDS" >> "$BATCH_LOG"
    else
        echo "  Crawl FAILED for batch $CURRENT → $BATCH_END"
        echo "FAIL_CRAWL:${CURRENT}_${BATCH_END}  $(date)" >> "$BATCH_LOG"
        FAILED_BATCHES=$(( FAILED_BATCHES + 1 ))
    fi

    echo "  Finished: $(date '+%Y-%m-%d %H:%M:%S')"
    CURRENT=$BATCH_END
done

echo ""
echo "========================================"
echo "All batches completed."
echo "  Total batches: $TOTAL_BATCHES"
echo "  Failed:        $FAILED_BATCHES"
echo "  Output:        $OUT_DIR"
echo "  Log:           $BATCH_LOG"
echo "========================================"