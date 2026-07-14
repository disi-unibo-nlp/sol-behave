#!/usr/bin/env bash
# =============================================================================
# run_historical_transactions.sh
# -----------------------------------------------------------------------------
# Orchestrates HISTORICAL transaction replay.
#
#   --sanity        Run replay_transactions.py on the ORIGINAL dataset contracts.
#                   This is the ground-truth reproduction check (the "does the
#                   original source redeploy and reproduce its own history"
#                   filter). Run this ONCE; it is model-independent.
#
#   (default)       For every model, run replay_generated.py against that
#                   model's repaired samples dir:
#                     src/source_generation/repaired_oneshot_<safe_model>/
#                   with outputs written OUT of source_generation into:
#                     src/transaction_replay/generated_historical_replay_results_<safe>/
#
# "safe_model" is the model filename sanitized with:  tr '. ' '__'
#   e.g. Qwen3.6-27B-UD-Q4_K_XL.gguf -> Qwen3_6-27B-UD-Q4_K_XL_gguf
#
# Usage:
#   bash scripts/run_historical_transactions.sh --sanity
#   bash scripts/run_historical_transactions.sh                 # all models
#   bash scripts/run_historical_transactions.sh --only Qwen3.6-27B-UD-Q4_K_XL.gguf
#   K=1000 FORCE=1 bash scripts/run_historical_transactions.sh
#
# Env overrides:
#   K              transactions per contract      (default 1000)
#   DATASET        original dataset dir           (default .../contracts_dataset_1to20)
#   GEN_ROOT       source_generation root         (default src/source_generation)
#   GEN_PREFIX     per-model dir prefix           (default repaired_oneshot_)
#   OUT_ROOT       root for replay outputs        (default src/transaction_replay)
#   OUT_PREFIX     per-model output dir prefix    (default generated_historical_replay_results_)
#   TX_TIMEOUT     per-tx timeout seconds         (default 60)
#   FORCE=1        pass --force to the python jobs
#   RETRY_FAILED=1 pass --retry-failed
#   VERBOSE=1      pass --verbose
# =============================================================================
set -uo pipefail

# ---- Locate repo dirs relative to this script ------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"     # scripts/ is under repo root
SRC="$REPO_ROOT/src"

REPLAY_TX="$SRC/transaction_replay/replay_transactions.py"
REPLAY_GEN="$SRC/transaction_replay/replay_generated.py"

# ---- Config (env-overridable) ----------------------------------------------
K="${K:-1000}"
DATASET="${DATASET:-$SRC/dataset/contracts_dataset_1to20}"
GEN_ROOT="${GEN_ROOT:-$SRC/source_generation}"
GEN_PREFIX="${GEN_PREFIX:-repaired_oneshot_}"
OUT_ROOT="${OUT_ROOT:-$SRC/transaction_replay}"
OUT_PREFIX="${OUT_PREFIX:-generated_historical_replay_results_}"
TX_TIMEOUT="${TX_TIMEOUT:-60}"

# Build optional flag string passed through to python
PYFLAGS=""
[ "${FORCE:-0}" = "1" ]        && PYFLAGS="$PYFLAGS --force"
[ "${RETRY_FAILED:-0}" = "1" ] && PYFLAGS="$PYFLAGS --retry-failed"
[ "${VERBOSE:-0}" = "1" ]      && PYFLAGS="$PYFLAGS --verbose"

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
SANITY=0
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --sanity) SANITY=1; shift ;;
    --only)   ONLY="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done

# ---- Preflight --------------------------------------------------------------
[ -f "$REPLAY_TX"  ] || { echo "ERROR: not found: $REPLAY_TX";  exit 1; }
[ -f "$REPLAY_GEN" ] || { echo "ERROR: not found: $REPLAY_GEN"; exit 1; }
[ -d "$DATASET"    ] || { echo "ERROR: dataset not found: $DATASET"; exit 1; }

echo "════════════════════════════════════════════════════════════"
echo "  run_historical_transactions.sh"
echo "    K            = $K"
echo "    dataset      = $DATASET"
echo "    gen_root     = $GEN_ROOT"
echo "    gen_prefix   = $GEN_PREFIX"
echo "    out_root     = $OUT_ROOT"
echo "    out_prefix   = $OUT_PREFIX"
echo "    py flags     = ${PYFLAGS:-<none>}"
echo "    mode         = $([ "$SANITY" = 1 ] && echo SANITY || echo GENERATED)"
echo "════════════════════════════════════════════════════════════"

# =============================================================================
# SANITY MODE: replay originals (ground-truth reproduction check)
# =============================================================================
if [ "$SANITY" = 1 ]; then
  echo
  echo ">>> SANITY: replaying ORIGINAL contracts (replay_transactions.py)"
  ( cd "$REPO_ROOT" && python3 "$REPLAY_TX" \
      --dataset "$DATASET" \
      --k "$K" \
      --tx-timeout "$TX_TIMEOUT" \
      $PYFLAGS )
  rc=$?
  echo ">>> SANITY done (exit $rc)"
  exit $rc
fi

# =============================================================================
# GENERATED MODE: loop models -> replay_generated.py on each repaired dir
# =============================================================================
overall_rc=0
for model in "${MODELS[@]}"; do
  # respect --only (accepts either the raw filename or the sanitized form)
  if [ -n "$ONLY" ] && [ "$model" != "$ONLY" ] && [ "$(sanitize "$model")" != "$ONLY" ]; then
    continue
  fi

  safe="$(sanitize "$model")"
  gen_dir="$GEN_ROOT/${GEN_PREFIX}${safe}"
  out_dir="$OUT_ROOT/${OUT_PREFIX}${safe}"

  echo
  echo "────────────────────────────────────────────────────────────"
  echo "  MODEL: $model"
  echo "  gen  : $gen_dir"
  echo "  out  : $out_dir"
  echo "────────────────────────────────────────────────────────────"

  if [ ! -d "$gen_dir" ]; then
    echo "  [skip] generated dir not found: $gen_dir"
    overall_rc=1
    continue
  fi

  # Per-model aggregate output so runs don't clobber each other
  results_file="$OUT_ROOT/generated_replay_results_${safe}.json"

  ( cd "$REPO_ROOT" && python3 "$REPLAY_GEN" \
      --dataset "$DATASET" \
      --generated "$gen_dir" \
      --output-dir "$out_dir" \
      --k "$K" \
      --tx-timeout "$TX_TIMEOUT" \
      --results-file "$results_file" \
      $PYFLAGS )
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "  [warn] replay_generated exited $rc for $model"
    overall_rc=$rc
  fi
  echo "  -> per-sample detail: $out_dir/<address>/"
  echo "  -> aggregate:         $results_file"
done

echo
echo "════════════════════════════════════════════════════════════"
echo "  ALL MODELS DONE (overall exit $overall_rc)"
echo "════════════════════════════════════════════════════════════"
exit $overall_rc