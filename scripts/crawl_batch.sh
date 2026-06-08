#!/bin/bash

set -e

START_BLOCK=$1
END_BLOCK=$2

CAND_DIR="/workdir/src/dataset/candidates"
OUT_DIR="contracts_dataset"
BLACKLIST="blacklist.json"

CAND_FILE="$CAND_DIR/candidates_${START_BLOCK}_${END_BLOCK}.json"

echo "========================================"
echo "Batch: $START_BLOCK → $END_BLOCK"
echo "Candidates: $CAND_FILE"
echo "========================================"

mkdir -p $CAND_DIR

# ── 1. Seed candidates ─────────────────────

echo "[1/2] Seeding candidates..."

python3 /workdir/src/dataset/seed_from_etherscan.py \
  --start-block $START_BLOCK \
  --end-block $END_BLOCK \
  --min-tx 200 \
  --max-candidates 500 \
  --blacklist $BLACKLIST \
  --output $CAND_FILE

# ── 2. Crawl ───────────────────────────────

echo "[2/2] Crawling..."

python3 /workdir/src/dataset/crawl.py \
  --from-file $CAND_FILE \
  --output-dir $OUT_DIR \
  --work-dir hardhat_workspace_crawl \
  --blacklist $BLACKLIST \
  --k 200 \
  --pass-threshold 0.5

echo "✓ Batch completed"