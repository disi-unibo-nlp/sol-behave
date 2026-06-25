#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# Differential-fuzzing orchestrator (Echidna) — per model.
#
# Unlike the generation orchestrators, this needs NO llama.cpp server and NO
# GPU: differential_fuzzing.py runs Echidna over already-generated source.
# It just loops over models, pointing --generated at each model's
# generated-source dir and --diff-dir at a per-model results dir.
#
# The Python script's DEFAULT_GENERATED / DEFAULT_DIFF_DIR encode the model
# name as a suffix; we override both via CLI per model, so no Python edits.
#
# Directory naming convention (matches existing dirs):
#   model "Qwen3.6-27B-UD-Q4_K_XL.gguf"
#     -> generated_oneshot_Qwen3_6-27B-UD-Q4_K_XL_gguf   (source in)
#     -> fuzzying_results_Qwen3_6-27B-UD-Q4_K_XL_gguf     (results out)
#   (dots and spaces in the model name become underscores)
# ----------------------------------------------------------------------------
set -uo pipefail

# ── Resolve repo root from this script's location (scripts/ under root) ──────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Where differential_fuzzing.py lives (adjust if your layout differs)
FUZZ_DIR="${REPO_ROOT}/src/differential_fuzzying"
FUZZ_SCRIPT="differential_fuzzying.py"

# Where per-model generated source dirs live
SRC_GEN_DIR="${REPO_ROOT}/src/source_generation"

# Echidna / run knobs (passed through to the Python script)
TEST_LIMIT="${TEST_LIMIT:-10000}"
SEQ_LEN="${SEQ_LEN:-20}"
TIMEOUT="${TIMEOUT:-120}"
WORKERS="${WORKERS:-8}"
REPLAY_K="${REPLAY_K:-1000}"

# Optional: restrict to the same evaluated set as replay (recommended for
# apples-to-apples). Point at a replay results JSON, or leave empty for all.
FROM_RESULTS="${FROM_RESULTS:-}"

# ── Models to fuzz. Each is the GGUF filename used at GENERATION time, so the
#    generated-source dir suffix matches. Uncomment the ones you want. ────────
MODELS=(
  "Qwen3.6-27B-UD-Q4_K_XL.gguf"
  #"Qwen.Qwen3-Coder-30B-A3B-Instruct.Q4_K_M.gguf"
  #"mistralai.Devstral-Small-2-24B-Instruct-2512.Q4_K_M.gguf"
  #"gemma-4-31B-it-qat-UD-Q4_K_XL.gguf"
)

# ── Sanity checks ────────────────────────────────────────────────────────────
if ! command -v echidna-bin >/dev/null 2>&1; then
  echo "WARNING: echidna-bin not found on PATH — harnesses will be generated"
  echo "         but fuzzing will be skipped. Install echidna first."
fi

if [[ ! -f "${FUZZ_DIR}/${FUZZ_SCRIPT}" ]]; then
  echo "ERROR: ${FUZZ_DIR}/${FUZZ_SCRIPT} not found"
  exit 1
fi

# ── Per-model loop ───────────────────────────────────────────────────────────
for MODEL_NAME in "${MODELS[@]}"; do
  # Sanitize: dots and spaces -> underscores (matches existing dir names)
  SAFE=$(echo "$MODEL_NAME" | tr '. ' '__')

  GEN_DIR="${SRC_GEN_DIR}/generated_oneshot_${SAFE}"
  DIFF_DIR="${FUZZ_DIR}/fuzzying_results_${SAFE}"

  echo "============================================================"
  echo "  DIFFERENTIAL FUZZING: $MODEL_NAME"
  echo "    generated = $GEN_DIR"
  echo "    diff-dir  = $DIFF_DIR"
  echo "============================================================"

  if [[ ! -d "$GEN_DIR" ]]; then
    echo "  !! generated-source dir not found: $GEN_DIR"
    echo "     (did source generation run for this model?) — skipping"
    continue
  fi

  # Build the argument list
  ARGS=(
    --generated  "$GEN_DIR"
    --diff-dir   "$DIFF_DIR"
    --test-limit "$TEST_LIMIT"
    --seq-len    "$SEQ_LEN"
    --timeout    "$TIMEOUT"
    --workers    "$WORKERS"
    --replay-k   "$REPLAY_K"
    --force
  )
  # Use the same evaluated set as replay, if provided
  if [[ -n "$FROM_RESULTS" ]]; then
    ARGS+=( --from-results "$FROM_RESULTS" )
  fi

  ( cd "$FUZZ_DIR" && python3 "$FUZZ_SCRIPT" "${ARGS[@]}" )

  echo "  done -> ${DIFF_DIR}/fuzzing_results.json"
  echo
done

echo "All models complete."