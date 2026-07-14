#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# Sequential multi-model DCR-graph generation orchestrator (llama.cpp path).
#
# Analogous to run_generations_llamacpp.sh, but for generate_dcr.py:
#   - calls generate_dcr.py instead of generate_source.py
#   - uses a LARGE per-slot context (DCR prompts feed full contract source)
#   - writes to the DCR output dir, one per model
#
# CAUTION: DCR generation produces your SYNTHETIC-TRANSACTION ORACLE. If your
# locked-down discriminability results were generated with a specific model
# (e.g. Qwen2.5-Coder-32B-AWQ via vLLM), re-running DCR with a different model
# REGENERATES that oracle and can shift those results. For now this is set up
# to run ONE model for testing; add more to MODELS only when you've decided
# you actually want per-model DCR graphs.
#
# Per model:
#   1. start llama.cpp server (backgrounded, named, BIG context)
#   2. wait until /v1/models responds
#   3. thinking-off smoke test
#   4. run generate_dcr.py (LlamaCppClient, greedy/temperature 0)
#   5. tear down server (free GPU)
#
# One 5090 -> strictly sequential.
# ----------------------------------------------------------------------------
set -uo pipefail

# ── Resolve repo root from this script's location (scripts/ is under root) ───
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ── Load env (absolute) ──────────────────────────────────────────────────────
set -a
source "${REPO_ROOT}/.env"
set +a

# ── Config ──────────────────────────────────────────────────────────────────
IMAGE="${IMAGE:-llama.cpp:localcuda}"
MODEL_DIR="${MODEL_DIR:-/llms}"
SERVER_PORT="${PORT:-8080}"
SERVER_HOST="${HOST:-127.0.0.1}"
API_KEY="${LLAMA_API_KEY:-local-llamacpp-key}"
CONTAINER="llamacpp_server"
GEN_DIR_SRC="${REPO_ROOT}/src/dcr_generation"     # where generate_dcr.py lives
GEN_SCRIPT="generate_dcr.py"
LOG_DIR="${REPO_ROOT}/dcr_generation_logs"
READY_TIMEOUT="${READY_TIMEOUT:-600}"

# DCR prompts are LARGE (full contract source). Per-slot context = DCR_CTX_SIZE
# / DCR_PARALLEL. We override the source-gen defaults with bigger values here.
# 49152 / 3 = 16384 tokens per slot: room for a big contract + 4096 output.
DCR_CTX_SIZE="${DCR_CTX_SIZE:-49152}"
DCR_PARALLEL="${DCR_PARALLEL:-3}"

# Path to generate_dcr.py's input/output (relative to GEN_DIR_SRC, per its own
# defaults); override via env if your layout differs.
DCR_CONTRACTS="${CONTRACTS_DIR:-../dataset/contracts_dataset_1to20}"
DCR_GENERATED_BASE="${GENERATED_DIR_DCR:-./generated_dcr_1to20}"

# ── The models to run. For TESTING keep ONE entry. Add more later only if you
#    deliberately want per-model DCR oracles. Each is a GGUF in MODEL_DIR. ────
MODELS=(
  #"Qwen3.6-27B-UD-Q4_K_XL.gguf" # avg_compile@k: 0.6472
  #"Qwen.Qwen3-Coder-30B-A3B-Instruct.Q4_K_M.gguf" # avg_compile@k: 0.5316
  #"mistralai.Devstral-Small-2-24B-Instruct-2512.Q4_K_M.gguf" # avg_compile@k: 0.1884
  #"gemma-4-31B-it-qat-UD-Q4_K_XL.gguf" # avg_compile@k: 0.8908
  "gemma-4-12B-it-qat-UD-Q4_K_XL.gguf" # avg_compile@k: 0.4804
  "ornith-1.0-35b-Q4_K_M.gguf" # avg_compile@k: 0.4124
  "Codestral-22B-v0.1-Q4_K_M.gguf" # avg_compile@k: 0.3432
)

mkdir -p "$LOG_DIR"

stop_server() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}

wait_ready() {
  local url="http://${SERVER_HOST}:${SERVER_PORT}/v1/models"
  local start; start=$(date +%s)
  echo -n "    waiting for model load "
  while true; do
    if curl -fsS -H "Authorization: Bearer ${API_KEY}" "$url" >/dev/null 2>&1; then
      echo " ready"; return 0
    fi
    if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
      echo " FAILED (container exited)"
      docker logs "$CONTAINER" 2>&1 | tail -20
      return 1
    fi
    local now; now=$(date +%s)
    if (( now - start > READY_TIMEOUT )); then
      echo " TIMEOUT after ${READY_TIMEOUT}s"; return 1
    fi
    echo -n "."; sleep 5
  done
}

smoke_test() {
  local url="http://${SERVER_HOST}:${SERVER_PORT}/v1/chat/completions"
  local out
  out=$(curl -fsS -H "Authorization: Bearer ${API_KEY}" \
        -H "Content-Type: application/json" "$url" \
        -d '{"model":"x","messages":[{"role":"user","content":"Reply with one word: ok"}],"max_tokens":10}' 2>/dev/null)
  local content
  content=$(echo "$out" | python3 -c 'import sys,json; print(json.load(sys.stdin)["choices"][0]["message"]["content"])' 2>/dev/null)
  if [[ -z "$content" ]]; then
    echo "    WARN: empty content — thinking may still be ON"
    return 1
  fi
  echo "    smoke test ok: '$content'"
  return 0
}

trap 'echo; echo "Interrupted — tearing down server"; stop_server; exit 130' INT TERM

stop_server   # clean slate at startup

for MODEL_NAME in "${MODELS[@]}"; do
  SAFE=$(echo "$MODEL_NAME" | tr '/. ' '___')
  DCR_OUT="${DCR_GENERATED_BASE}_${SAFE}"   # per-model DCR output dir (relative to GEN_DIR_SRC)
  echo "============================================================"
  echo "  DCR MODEL: $MODEL_NAME"
  echo "============================================================"

  if [[ ! -f "${MODEL_DIR}/${MODEL_NAME}" ]]; then
    echo "  !! ${MODEL_DIR}/${MODEL_NAME} not found — skipping"
    continue
  fi

  stop_server

  echo "  [1/4] starting server (ctx=${DCR_CTX_SIZE}, parallel=${DCR_PARALLEL}) ..."
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
    --ctx-size "${DCR_CTX_SIZE}" \
    --parallel "${DCR_PARALLEL}" \
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

  smoke_test || echo "    (continuing; generate_dcr strips <think> and /no_think)"

  echo "  [3/4] running DCR generation ..."
  if ! ( cd "$GEN_DIR_SRC" && \
      LLM_BACKEND=llamacpp \
      LLAMACPP_HOST="$SERVER_HOST" \
      LLAMACPP_PORT="$SERVER_PORT" \
      LLAMACPP_API_KEY="$API_KEY" \
      LLAMACPP_MAX_PARALLEL="${DCR_PARALLEL}" \
      LLM_TEMPERATURE=0.0 \
      python3 "$GEN_SCRIPT" \
        --model "" \
        --contracts "$DCR_CONTRACTS" \
        --generated "$DCR_OUT" \
    ) 2>&1 | tee "${LOG_DIR}/dcr_${SAFE}_generation.log"; then
    echo "    !! DCR generation FAILED for $MODEL_NAME"
  fi
  echo "    DCR generation done → ${GEN_DIR_SRC}/${DCR_OUT}"

  echo "  [4/4] stopping server ..."
  stop_server
  echo "    GPU freed."
  echo
done

echo "All DCR models complete."