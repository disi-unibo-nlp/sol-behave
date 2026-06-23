#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# Sequential multi-model generation orchestrator for the llama.cpp path.
#
# For each model in MODELS:
#   1. start the llama.cpp server (backgrounded, named container)
#   2. wait until /v1/models responds (model fully loaded)
#   3. disable-thinking smoke test
#   4. run generate_source.py against it (LlamaCppClient)
#   5. stop+remove the server container (frees the GPU)
#   6. move to the next model
#
# One 5090 → strictly sequential. A model's server is fully torn down
# before the next one starts, so VRAM never double-allocates.
#
# Assumes:
#   - generate_source.py entry point uses LlamaCppClient (the two-line swap)
#   - serve_llamacpp.sh is the foreground docker-run script you already have
#   - .env holds GPU_DEVICE / MODEL_DIR / PORT / CTX_SIZE / PARALLEL /
#     LLAMA_API_KEY / LLAMACPP_* etc.
# ----------------------------------------------------------------------------
set -uo pipefail

# ── Load env ────────────────────────────────────────────────────────────────
set -a
source "$(cd "$(dirname "$0")/.." && pwd)/.env"
set +a

# ── Config ──────────────────────────────────────────────────────────────────
IMAGE="${IMAGE:-llama.cpp:localcuda}"
MODEL_DIR="${MODEL_DIR:-/llms}"
SERVER_PORT="${PORT:-8080}"
SERVER_HOST="${HOST:-127.0.0.1}"
API_KEY="${LLAMA_API_KEY:-local-llamacpp-key}"
CONTAINER="llamacpp_server"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"   # resolves to SolFormalEval
GEN_DIR_SRC="${REPO_ROOT}/src/source_generation"
GEN_SCRIPT="generate_source.py"
LOG_DIR="${REPO_ROOT}/generation_logs"
READY_TIMEOUT="${READY_TIMEOUT:-600}"   # seconds to wait for model load

# ── The models to run: one GGUF filename per line (must exist in MODEL_DIR) ──
# Keep ONE quant level across all for a clean comparison.
MODELS=(
  "Qwen3.6-27B-UD-Q4_K_XL.gguf"
  "Qwen.Qwen3-Coder-30B-A3B-Instruct.Q4_K_M.gguf"
  "mistralai.Devstral-Small-2-24B-Instruct-2512.Q4_K_M.gguf"
  "gemma-4-31B-it-qat-UD-Q4_K_XL.gguf" 
)

mkdir -p "$LOG_DIR"

# ── Helper: stop any stale server ───────────────────────────────────────────
stop_server() {
  # Force-remove by name, whether running, stopped, or zombie.
  # -f kills if running AND removes in one step; ignore errors if absent.
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}

# ── Helper: wait until the server answers /v1/models ────────────────────────
wait_ready() {
  local url="http://${SERVER_HOST}:${SERVER_PORT}/v1/models"
  local start; start=$(date +%s)
  echo -n "    waiting for model load "
  while true; do
    if curl -fsS -H "Authorization: Bearer ${API_KEY}" "$url" >/dev/null 2>&1; then
      echo " ready"
      return 0
    fi
    # bail if the container died during load (OOM, bad file, etc.)
    if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
      echo " FAILED (container exited)"
      docker logs "$CONTAINER" 2>&1 | tail -20
      return 1
    fi
    local now; now=$(date +%s)
    if (( now - start > READY_TIMEOUT )); then
      echo " TIMEOUT after ${READY_TIMEOUT}s"
      return 1
    fi
    echo -n "."
    sleep 5
  done
}

# ── Helper: confirm thinking is OFF (content non-empty) ─────────────────────
smoke_test() {
  local url="http://${SERVER_HOST}:${SERVER_PORT}/v1/chat/completions"
  local out
  out=$(curl -fsS -H "Authorization: Bearer ${API_KEY}" \
        -H "Content-Type: application/json" "$url" \
        -d '{"model":"x","messages":[{"role":"user","content":"Reply with one word: ok"}],"max_tokens":10}' 2>/dev/null)
  local content
  content=$(echo "$out" | python3 -c 'import sys,json; print(json.load(sys.stdin)["choices"][0]["message"]["content"])' 2>/dev/null)
  if [[ -z "$content" ]]; then
    echo "    WARN: empty content — thinking may still be ON for this model"
    return 1
  fi
  echo "    smoke test ok: '$content'"
  return 0
}

# ── Main loop ───────────────────────────────────────────────────────────────
trap 'echo; echo "Interrupted — tearing down server"; stop_server; exit 130' INT TERM

for MODEL_NAME in "${MODELS[@]}"; do
  SAFE=$(echo "$MODEL_NAME" | tr '/. ' '___')
  echo "============================================================"
  echo "  MODEL: $MODEL_NAME"
  echo "============================================================"

  # Skip if the GGUF isn't on disk
  if [[ ! -f "${MODEL_DIR}/${MODEL_NAME}" ]]; then
    echo "  !! ${MODEL_DIR}/${MODEL_NAME} not found — skipping"
    continue
  fi

  stop_server   # clear any stale one

  echo "  [1/4] starting server ..."
  # Launch serve_llamacpp.sh's docker run, but backgrounded + named.
  # We replicate the run here so we can add -d and --name without editing
  # your serve script.
  docker run -d --rm \
    --gpus "device=${GPU_DEVICE:-0}" \
    --name "$CONTAINER" \
    -v "${MODEL_DIR}:/models:ro" \
    -p "${SERVER_HOST}:${SERVER_PORT}:8080" \
    "${IMAGE}" \
    -m "/models/${MODEL_NAME}" \
    --api-key "${API_KEY}" \
    --host 0.0.0.0 --port 8080 \
    --n-gpu-layers "${N_GPU_LAYERS:-999}" \
    --ctx-size "${CTX_SIZE:-8192}" \
    --parallel "${PARALLEL:-3}" \
    --flash-attn "${FLASH_ATTN:-on}" \
    --batch-size "${BATCH_SIZE:-1024}" \
    --chat-template-kwargs '{"enable_thinking": false}' \
    --jinja >/dev/null

  echo "  [2/4] waiting for readiness ..."
  if ! wait_ready; then
    echo "  !! server failed to become ready — skipping $MODEL_NAME"
    stop_server
    continue
  fi

  smoke_test || echo "    (continuing anyway; client strips <think>)"

  echo "  [3/4] running generation (inside solcontract_container) ..."
  GEN_OUT="generated_oneshot_${SAFE}"
  docker exec \
    -e LLAMACPP_HOST=127.0.0.1 \
    -e LLAMACPP_PORT="${SERVER_PORT}" \
    -e LLAMACPP_API_KEY="${API_KEY}" \
    -e LLAMACPP_MAX_PARALLEL="${PARALLEL:-3}" \
    -e LLM_TEMPERATURE="${LLM_TEMPERATURE:-0.7}" \
    -w /workdir/src/source_generation \
    solcontract_container \
    bash -lc "source /root/.nvm/nvm.sh && nvm use 24 >/dev/null && python3 generate_source.py --generated '$GEN_OUT'" \
    2>&1 | tee "${LOG_DIR}/${SAFE}_generation.log"
done

if ! ( cd "$GEN_DIR_SRC" && LLAMACPP_HOST=... python3 "$GEN_SCRIPT" --generated "$GEN_OUT" ) 2>&1 | tee "${LOG_DIR}/${SAFE}_generation.log"; then
    echo "    !! generation FAILED for $MODEL_NAME"
fi

echo "All models complete."