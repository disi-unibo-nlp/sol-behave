#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# Sequential multi-model REPAIR orchestrator for the llama.cpp path.
#
# Mirrors run_multi_model.sh (generation), but runs repair_source.py against
# each model's already-generated one-shot samples.
#
# For each model in MODELS:
#   1. start the llama.cpp server (backgrounded, named container)
#   2. wait until /v1/models responds (model fully loaded)
#   3. disable-thinking smoke test
#   4. run repair_source.py against it (LlamaCppClient), pointed at that
#      model's generated_oneshot_<SAFE> directory
#   5. stop+remove the server container (frees the GPU)
#   6. move to the next model
#
# PREREQUISITE (blocking): repair_source.py's __main__ block currently
# instantiates VLLMClient (in-process vLLM load of an HF/AWQ model id), NOT
# LlamaCppClient. It must receive the same "two-line swap" already applied
# to generate_source.py before this scheduler will work — otherwise it will
# try to load a model locally instead of talking to the server started below.
#
# Assumes (once the swap above is done):
#   - repair_source.py's entry point uses LlamaCppClient, reading
#     LLAMACPP_HOST / LLAMACPP_PORT / LLAMACPP_API_KEY / LLAMACPP_MAX_PARALLEL
#     from the environment, same as generate_source.py
#   - serve_llamacpp.sh is the foreground docker-run script you already have
#   - .env holds GPU_DEVICE / MODEL_DIR / PORT / CTX_SIZE / PARALLEL /
#     LLAMA_API_KEY / LLAMACPP_* etc.
#   - each model in MODELS already has a populated
#     generated_oneshot_<SAFE>/ directory from the generation run
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
REPAIR_DIR_SRC="${REPO_ROOT}/src/source_generation"
REPAIR_SCRIPT="repair_source.py"
LOG_DIR="${REPO_ROOT}/repair_logs"
READY_TIMEOUT="${READY_TIMEOUT:-600}"   # seconds to wait for model load

# Repair-specific: deterministic, low temperature (repair_source.py's own
# default is 0.2; kept separate from LLM_TEMPERATURE=0.7 used for generation).
REPAIR_TEMPERATURE="${REPAIR_TEMPERATURE:-0.2}"
REPAIR_BATCH_SIZE="${REPAIR_BATCH_SIZE:-32}"
REPAIR_CONTEXT_LENGTH="${REPAIR_CONTEXT_LENGTH:-8192}"
CONTRACTS_DIR="${CONTRACTS_DIR:-../dataset/contracts_dataset_1to20}"
WORK_DIR_NAME="${REPAIR_WORK_DIR:-../dataset/hardhat_workspace_crawl}"

# ── The models to repair: one GGUF filename per line ─────────────────────────
# Must match the filenames used during generation, so SAFE-sanitization
# resolves to the same generated_oneshot_<SAFE> directory.
MODELS=(
  "Qwen3.6-27B-UD-Q4_K_XL.gguf"                          #after repair: "avg_compile_at_k": 0.7132 #before repair: avg_compile@k: 0.6472
  "Qwen.Qwen3-Coder-30B-A3B-Instruct.Q4_K_M.gguf"        #after repair: "avg_compile_at_k": 0.6175 #before repair: avg_compile@k: 0.5316
  "mistralai.Devstral-Small-2-24B-Instruct-2512.Q4_K_M.gguf" #after repair: "avg_compile_at_k": 0.264 #before repair: avg_compile@k: 0.1884
  "gemma-4-31B-it-qat-UD-Q4_K_XL.gguf"                   #after repair: "avg_compile_at_k": 0.9268 #before repair: avg_compile@k: 0.8908
  "gemma-4-12B-it-qat-UD-Q4_K_XL.gguf"                   #after repair: "avg_compile_at_k": 0.5788 #before repair: avg_compile@k: 0.4804
  "ornith-1.0-35b-Q4_K_M.gguf"                            #"avg_compile_at_k": 0.5208 #before repair: avg_compile@k: 0.4124
  "Codestral-22B-v0.1-Q4_K_M.gguf"                        #"avg_compile_at_k": 0.3992 #before repair: avg_compile@k: 0.3432
)

mkdir -p "$LOG_DIR"

# ── Helper: stop any stale server ───────────────────────────────────────────
stop_server() {
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

# ── Helper: print which GPU device this run is using ────────────────────────
print_device() {
  local idx="${GPU_DEVICE:-0}"
  local name
  name=$(nvidia-smi --query-gpu=name --format=csv,noheader -i "$idx" 2>/dev/null)
  if [[ -z "$name" ]]; then
    echo "  GPU device: index ${idx} (nvidia-smi unavailable or index not found)"
  else
    echo "  GPU device: index ${idx} (${name})"
  fi
}

# ── Main loop ───────────────────────────────────────────────────────────────
trap 'echo; echo "Interrupted — tearing down server"; stop_server; exit 130' INT TERM

for MODEL_NAME in "${MODELS[@]}"; do
  SAFE=$(echo "$MODEL_NAME" | tr '/. ' '___')
  echo "============================================================"
  echo "  MODEL (repair): $MODEL_NAME"
  print_device
  echo "============================================================"

  GEN_DIR="generated_oneshot_${SAFE}"
  REPAIRED_OUT="repaired_oneshot_${SAFE}"

  # Skip if the GGUF isn't on disk
  if [[ ! -f "${MODEL_DIR}/${MODEL_NAME}" ]]; then
    echo "  !! ${MODEL_DIR}/${MODEL_NAME} not found — skipping"
    continue
  fi

  # Skip if there's nothing to repair (generation never ran for this model)
  if [[ ! -d "${REPAIR_DIR_SRC}/${GEN_DIR}" ]]; then
    echo "  !! ${REPAIR_DIR_SRC}/${GEN_DIR} not found — run generation first, skipping"
    continue
  fi

  stop_server   # clear any stale one

  echo "  [1/4] starting server ..."
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

  echo "  [3/4] running repair (inside solcontract_container) ..."
  docker exec \
    -e LLAMACPP_HOST=127.0.0.1 \
    -e LLAMACPP_PORT="${SERVER_PORT}" \
    -e LLAMACPP_API_KEY="${API_KEY}" \
    -e LLAMACPP_MAX_PARALLEL="${PARALLEL:-3}" \
    -w /workdir/src/source_generation \
    solcontract_container \
    bash -lc "source /root/.nvm/nvm.sh && nvm use 24 >/dev/null && \
      python3 ${REPAIR_SCRIPT} \
        --contracts '${CONTRACTS_DIR}' \
        --generated '${GEN_DIR}' \
        --repaired '${REPAIRED_OUT}' \
        --work_dir '${WORK_DIR_NAME}' \
        --batch_size ${REPAIR_BATCH_SIZE} \
        --context_length ${REPAIR_CONTEXT_LENGTH} \
        --temperature ${REPAIR_TEMPERATURE}" \
    2>&1 | tee "${LOG_DIR}/${SAFE}_repair.log"

  echo "  [4/4] done, tearing down server"
  stop_server
done

echo "All models' repair rounds complete."