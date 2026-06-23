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
hf download unsloth/gemma-4-31B-it-qat-GGUF --local-dir "$local_dir"