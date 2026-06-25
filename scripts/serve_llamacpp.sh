#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-llama.cpp:localcuda}"
MODEL_DIR="${MODEL_DIR:-/llms}"
MODEL_NAME="${MODEL_NAME:-Qwen3.6-27B-UD-Q4_K_XL.gguf}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"
GPU_DEVICE="${GPU_DEVICE:-${CUDA_VISIBLE_DEVICES:-0}}"
LLAMA_API_KEY="${LLAMA_API_KEY:-local-llamacpp-key}"

CTX_SIZE="${CTX_SIZE:-60000}"
PARALLEL="${PARALLEL:-3}"
BATCH_SIZE="${BATCH_SIZE:-1024}"
N_GPU_LAYERS="${N_GPU_LAYERS:-999}"
FLASH_ATTN="${FLASH_ATTN:-on}"

docker run --rm \
  --gpus "device=${GPU_DEVICE}" \
  -v "${MODEL_DIR}:/models:ro" \
  -p "${HOST}:${PORT}:8080" \
  "${IMAGE}" \
  -m "/models/${MODEL_NAME}" \
  --api-key "${LLAMA_API_KEY}" \
  --host 0.0.0.0 \
  --port 8080 \
  --n-gpu-layers "${N_GPU_LAYERS}" \
  --ctx-size "${CTX_SIZE}" \
  --parallel "${PARALLEL}" \
  --flash-attn "${FLASH_ATTN}" \
  --batch-size "${BATCH_SIZE}" \
  --chat-template-kwargs '{"enable_thinking": false}' 
