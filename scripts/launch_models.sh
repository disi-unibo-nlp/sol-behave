#!/bin/bash

source .env

set -e

LLM_PORT=${LLM_PORT:-8000}

echo "Loading main model on port $LLM_PORT..."

vllm serve "$LLM_MODEL" \
    --host 0.0.0.0 \
    --port "$LLM_PORT" \
    --download-dir /models \
    --max-num-seqs "$MAX_NUM_SEQS" \
    --max-model-len "$MAX_MODEL_LEN" \
    --default-chat-template-kwargs '{"enable_thinking": false}' \
    --gpu-memory-utilization "$VLLM_MEMORY_PERC" > /dev/null 2>&1 &
sleep 60