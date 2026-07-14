#!/usr/bin/env bash
# =============================================================================
# run_make_sequences.sh
# -----------------------------------------------------------------------------
# Runs make_sequences_hardhat.py for every model, producing per-contract
# validated synthetic transaction sequences from each model's DCR graphs.
#
# make_sequences_hardhat.py is configured via ENVIRONMENT VARIABLES (not CLI
# flags). This orchestrator exports GENERATED_DIR per model and invokes it.
# The only positional args the script accepts are contract addresses; we pass
# none, so it processes every contract that has source_dcr.json in that dir.
#
# Per model it reads:
#   src/dcr_generation/generated_dcr_1to20_<safe_model>/<addr>/source_dcr.json
# and writes (into the SAME dir, next to the DCR):
#   <addr>/transactions_synthetic.json
#   <addr>/sequences_hardhat.json
#   <addr>/coverage.json
# plus <dataset>/<addr>/deploy_params.json
#
# "safe_model" = filename sanitized with: tr '. ' '__'
#   e.g. Qwen3.6-27B-UD-Q4_K_XL.gguf -> Qwen3_6-27B-UD-Q4_K_XL_gguf
#
# Usage:
#   bash scripts/run_make_sequences.sh                 # all models
#   bash scripts/run_make_sequences.sh --only gemma-4-31B-it-qat-UD-Q4_K_XL.gguf
#   MAX_STEPS=1000 SEED=42 bash scripts/run_make_sequences.sh
#
# Env overrides (passed through to the python script):
#   MAX_STEPS      target successful txs per contract   (default 1000)
#   SEED           RNG seed                              (default 42)
#   DATASET_DIR    original dataset dir                 (default .../contracts_dataset_1to20)
#   WORK_DIR       shared Hardhat workspace             (default .../hardhat_workspace_crawl)
#   DCR_ROOT       dir holding generated_dcr_1to20_*    (default src/dcr_generation)
#   DCR_PREFIX     per-model DCR dir prefix             (default generated_dcr_1to20_)
#   LLM_API_URL    if set, enables param synthesis      (default empty = no LLM)
# =============================================================================
set -uo pipefail

# ---- Locate repo dirs -------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$REPO_ROOT/src"

MAKE_SEQ="$SRC/dcr_generation/make_sequences_hardhat.py"

# ---- Config (env-overridable) ----------------------------------------------
MAX_STEPS="${MAX_STEPS:-1000}"
SEED="${SEED:-42}"
DATASET_DIR="${DATASET_DIR:-$SRC/dataset/contracts_dataset_1to20}"
WORK_DIR="${WORK_DIR:-$SRC/dataset/hardhat_workspace_crawl}"
DCR_ROOT="${DCR_ROOT:-$SRC/dcr_generation}"
DCR_PREFIX="${DCR_PREFIX:-generated_dcr_1to20_}"
LLM_API_URL="${LLM_API_URL:-}"   # empty = no LLM param synthesis (default)

# ---- Model list -------------------------------------------------------------
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

# ---- Arg parsing ------------------------------------------------------------
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done

# ---- Preflight --------------------------------------------------------------
[ -f "$MAKE_SEQ"    ] || { echo "ERROR: not found: $MAKE_SEQ"; exit 1; }
[ -d "$DATASET_DIR" ] || { echo "ERROR: dataset not found: $DATASET_DIR"; exit 1; }

echo "════════════════════════════════════════════════════════════"
echo "  run_make_sequences.sh"
echo "    max_steps  = $MAX_STEPS"
echo "    seed       = $SEED"
echo "    dataset    = $DATASET_DIR"
echo "    work_dir   = $WORK_DIR"
echo "    dcr_root   = $DCR_ROOT"
echo "    dcr_prefix = $DCR_PREFIX"
echo "    llm_api    = ${LLM_API_URL:-<none: no param synthesis>}"
echo "════════════════════════════════════════════════════════════"

overall_rc=0
for model in "${MODELS[@]}"; do
  if [ -n "$ONLY" ] && [ "$model" != "$ONLY" ] && [ "$(sanitize "$model")" != "$ONLY" ]; then
    continue
  fi

  safe="$(sanitize "$model")"
  gen_dir="$DCR_ROOT/${DCR_PREFIX}${safe}"

  echo
  echo "────────────────────────────────────────────────────────────"
  echo "  MODEL: $model"
  echo "  DCR  : $gen_dir"
  echo "────────────────────────────────────────────────────────────"

  if [ ! -d "$gen_dir" ]; then
    echo "  [skip] DCR dir not found: $gen_dir"
    overall_rc=1
    continue
  fi

  # Count contracts with a DCR graph (informational)
  n_dcr=$(find "$gen_dir" -maxdepth 2 -name source_dcr.json 2>/dev/null | wc -l)
  echo "  contracts with source_dcr.json: $n_dcr"

  # Drive the python script purely through env vars; it takes no config flags.
  ( cd "$REPO_ROOT" && \
      DATASET_DIR="$DATASET_DIR" \
      GENERATED_DIR="$gen_dir" \
      WORK_DIR="$WORK_DIR" \
      MAX_STEPS="$MAX_STEPS" \
      SEED="$SEED" \
      LLM_API_URL="$LLM_API_URL" \
      python3 "$MAKE_SEQ" )
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "  [warn] make_sequences exited $rc for $model"
    overall_rc=$rc
  fi
  echo "  -> outputs written under: $gen_dir/<address>/"
done

echo
echo "════════════════════════════════════════════════════════════"
echo "  ALL MODELS DONE (overall exit $overall_rc)"
echo "════════════════════════════════════════════════════════════"
exit $overall_rc