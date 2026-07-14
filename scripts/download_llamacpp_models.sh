#!/bin/bash

local_dir="/llms"

# Seems to work
# Was already present in /llms
#hf download "Qwen3.6-27B-UD-Q4_K_XL.gguf" \
# --include "*Q4_K_M*" --local-dir "$local_dir"

# Still thinking while giving the answer:
#hf download DevQuasar/CohereLabs.North-Mini-Code-1.0-GGUF \
#  --include "*Q4_K_M*" --local-dir "$local_dir"

# Seems to work
#hf download DevQuasar/Qwen.Qwen3-Coder-30B-A3B-Instruct-GGUF \
#  --include "*Q4_K_M*" --local-dir "$local_dir"

# Downloaded in pieces, didn't work
#hf download DevQuasar/Qwen.Qwen3-Coder-Next-GGUF \
#  --include "*Q4_K_M*" --local-dir "$local_dir"

# Seems to work
#hf download DevQuasar/mistralai.Devstral-Small-2-24B-Instruct-2512-GGUF \
#  --include "*Q4_K_M*" --local-dir "$local_dir"

# Seems to work
#hf download unsloth/gemma-4-31B-it-qat-GGUF --local-dir "$local_dir"

# Seems to work, though it's likely to have a low compile@k
#hf download ibm-granite/granite-4.1-8b-GGUF \
#  --include "granite-4.1-8b-Q4_K_M.gguf" \
#  --local-dir "$local_dir"

# Seems to work, compile@k seems less than its bigger counterpart
#hf download "unsloth/gemma-4-12B-it-qat-GGUF" \
#  --include "gemma-4-12B-it-qat-UD-Q4_K_XL.gguf" \
#  --local-dir "$local_dir"

# Not working out of the box (generates a reasoning stream rather than the final answer)
#hf download "unsloth/Phi-4-reasoning-plus-GGUF" \
#    --include "Phi-4-reasoning-plus-UD-Q4_K_XL.gguf" \
#    --local-dir "$local_dir"

# Seems to perform poorly
#hf download "unsloth/Phi-4-mini-instruct-GGUF" \
#    --include "Phi-4-mini-instruct.BF16.gguf" \
#    --local-dir "$local_dir"

# Seems to underperform, even in terms of latency
#hf download "bartowski/DeepSeek-Coder-V2-Lite-Instruct-GGUF" \
#    --include "DeepSeek-Coder-V2-Lite-Instruct-Q4_K_L.gguf" \
#    --local-dir "$local_dir"

# Seems to work, with a nice compile@10 though it weights a lot (21Gb)
#hf download "deepreinforce-ai/Ornith-1.0-35B-GGUF" \
#    --include "ornith-1.0-35b-Q4_K_M.gguf" \
#    --local-dir "$local_dir"

# Seems to be slow in terms of latency, it also seems to underperform
#hf download "NikolayKozloff/granite-20b-code-instruct-Q4_0-GGUF" \
#    --include "granite-20b-code-instruct.Q4_0.gguf" \
#    --local-dir "$local_dir"


# Really slow in terms of latency, 
#hf download "bartowski/granite-34b-code-instruct-GGUF" \
#    --include "granite-34b-code-instruct-Q4_K_M.gguf" \
#    --local-dir "$local_dir"

hf download "bartowski/Codestral-22B-v0.1-GGUF" \
    --include "Codestral-22B-v0.1-Q4_K_M.gguf" \
    --local-dir "$local_dir"