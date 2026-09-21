# SolFormalEval

Code and pipeline for **"SolBehave: Evaluating the Behavioural Correctness of
LLM-Generated Smart Contracts"** (submitted to IEEE TSE).

Generated artifacts (model outputs, replay results, fuzzing corpora, human
evaluation data) are hosted separately on Hugging Face:
**[`Giovanni-P/soldcr-artifacts`](https://huggingface.co/datasets/Giovanni-P/soldcr-artifacts)**.

> **Status:** both this repository and the HF dataset repo are currently
> **private**. They will be made public together, tied to paper
> acceptance/camera-ready.

---

## Repository layout

```
src/
  dataset/              contract crawling + the benchmark dataset
  source_generation/    one-shot LLM contract generation + repair
  dcr_generation/       DCR (Dynamic Condition Response) graph extraction
  dcr_replay/           synthetic-transaction replay (DCR-derived oracle)
  transaction_replay/   historical-transaction replay
  differential_fuzzying/ Echidna-based differential fuzzing
  evaluation/            final tables, coverage + depth analyses
  human_eval/             manual DCR annotation + inter-rater metrics
  utils/                  shared helpers (Hardhat, LLM clients, storage)
  coverage_analysis/      superseded — kept for provenance, not part of the pipeline
scripts/                  orchestration shell scripts (see below)
```

Only source code is tracked in this repository. Generated data is excluded
via `.gitignore` and lives on the HF dataset repo instead — see
[Repopulating data from Hugging Face](#repopulating-data-from-hugging-face).

---

## Prerequisites

- Docker with GPU support (`--gpus`), NVIDIA drivers, a single CUDA-capable
  GPU (the pipeline is written for **one GPU, strictly sequential** model
  loading — see [Orchestration model](#orchestration-model))
- Python 3 + `pip`
- A local cache of GGUF model weights (see `download_llamacpp_models.sh`)
- A `.env` file at the repo root (never committed — see
  [Environment variables](#environment-variables))

---

## Environment variables

Copy this into a `.env` file at the repo root and fill in real values.
`.env` is gitignored; nothing here should ever be committed.

```bash
# Container / host
DEVICE=0                     # GPU index passed to `docker run --gpus device=...`
ROOT_DIRECTORY=src           # mounted into the container by start_bash.sh

# llama.cpp server
MODEL_DIR=/llms
MODEL_NAME=Qwen3.6-27B-UD-Q4_K_XL.gguf
HOST=127.0.0.1
PORT=8080
GPU_DEVICE=0
LLAMA_API_KEY=local-llamacpp-key
CTX_SIZE=8192
PARALLEL=3
BATCH_SIZE=1024
N_GPU_LAYERS=999
FLASH_ATTN=on
```

---

## One-time setup

1. **Download model weights** — edit `download_llamacpp_models.sh` to
   uncomment the models you need, then run it. The comments in that script
   record informal trial notes (latency, apparent quality) from model
   selection; the 7 models actually used in the paper are the ones active
   by default.
2. **Start the working container:**
   ```bash
   bash start_bash.sh
   ```
3. **Inside the container, install toolchain dependencies (once, before
   anything else):**
   ```bash
   bash get_hardhat_deps.sh
   source ~/.bashrc
   source .env
   ```
4. **Install Echidna** (for differential fuzzing):
   ```bash
   bash install_echidna.sh
   ```

---

## Orchestration model

Every `run_*.sh` script under `scripts/` follows the same pattern for
GPU-bound stages: **one model at a time, strictly sequential**, because the
pipeline was run on a single GPU:

1. start a llama.cpp server for one model (backgrounded, named container)
2. wait for `/v1/models` to respond
3. run a smoke test (confirms "thinking" output is off)
4. run the actual Python stage against that server
5. tear the server down before moving to the next model

`serve_llamacpp.sh` is a **standalone debugging utility** — a foreground,
single-model server for manual testing. The orchestrators below do not call
it; they each start their own backgrounded server internally.

### Editing the model list

Every orchestrator has a `MODELS=(...)` bash array near the top, listing the
7 GGUF filenames used in the paper. **Several scripts currently have some of
these models commented out** — left in whatever state they were last run in
(e.g. `run_dcr_generations_llamacpp.sh` only has 3 of 7 active, per its own
"testing" comment). Before running a script for a full reproduction, open it
and make sure the `MODELS` array has the models you actually want
uncommented. Per-model comments like `# avg_compile@k: 0.6472` are informal
result notes from prior runs, not configuration.

---

## Pipeline

Run in this order. Steps marked **(GPU)** use the sequential
server-per-model pattern above; the rest are plain Python/data processing.

| # | Script | Produces |
|---|---|---|
| 1 | `scripts/crawl_all.sh` | `src/dataset/contracts_dataset_1to20/` — the benchmark contracts |
| 2 | `scripts/run_generations_llamacpp.sh` **(GPU)** | `src/source_generation/generated_oneshot_<model>/` |
| 3 | `scripts/run_repair_llamacpp.sh` **(GPU)** | `src/source_generation/repaired_oneshot_<model>/` |
| 4 | `scripts/run_dcr_generations_llamacpp.sh` **(GPU)** | `src/dcr_generation/generated_dcr_1to20_<model>/` |
| 5 | `scripts/run_make_sequences.sh` | synthetic tx sequences, written back into each model's DCR dir |
| 6 | `scripts/run_historical_transactions.sh --sanity`, then `scripts/run_historical_transactions.sh` | `src/transaction_replay/` |
| 7 | `scripts/run_synth_replays.sh --sanity`, then `scripts/run_synth_replays.sh`, then `scripts/run_synth_replays.sh --gemma-oracle` (and `--sanity --gemma-oracle`) | `src/dcr_replay/` |
| 8 | `scripts/run_differential_fuzzying.sh` | `src/differential_fuzzying/fuzzying_results_<model>/` (independent of 6–7; only needs step 3's repaired source) |
| 9 | `scripts/run_depth_analyses.sh` | `src/evaluation/depth_historical/`, `depth_synthetic/`, `depth_synthetic_gemma/` (combines 6 + 7) |
| 10 | `scripts/run_coverage_comparison.sh` | `src/evaluation/coverage/` |
| 11 | `src/evaluation/build_final_tables.py` | `src/evaluation/final_tables.json`, `tables.tex` |
| 12 | `src/human_eval/build_dcr_annotation_sample.py`, then manual annotation, then `src/human_eval/compute_metrics.py` | `src/human_eval/metrics_summary.csv` |

`crawl_batch.sh` is a manual single-block-range variant of step 1, useful
for re-crawling one range by hand; `crawl_all.sh` is the canonical entry
point for building the full dataset and is what step 1 above refers to.

> **Known issue:** `run_generations_llamacpp.sh` has a leftover block after
> its main loop that references stale/unset variables and an invalid
> `LLAMACPP_HOST=...` value. It runs harmlessly after the real per-model
> work completes (extra failed log noise only) but should be removed.

---

## Data availability

All generated artifacts (~80 GB total — model outputs, replay results,
fuzzing corpora, human evaluation data) are publicly available on Hugging
Face: **[`Giovanni-P/soldcr-artifacts`](https://huggingface.co/datasets/Giovanni-P/soldcr-artifacts)**.

Download everything with:
```bash
pip install -U "huggingface_hub[cli]"
hf download Giovanni-P/soldcr-artifacts --repo-type dataset --local-dir ./src
```
or just one folder, e.g.:
```bash
hf download Giovanni-P/soldcr-artifacts --repo-type dataset \
  --local-dir ./src --include "dataset/contracts_dataset_1to20/*"
```

`differential_fuzzying/` is stored as per-model `.tar.gz` archives; extract
with `for f in fuzzying_results_*.tar.gz; do tar -xzf "$f"; done`.

`contracts_dataset_pass_08` (the extended dataset) is hosted at
https://huggingface.co/datasets/Giovanni-P/SmartContracts/tree/main.