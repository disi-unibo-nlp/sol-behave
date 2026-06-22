from huggingface_hub import snapshot_download
from pathlib import Path
import time

repo_id = "Giovanni-P/SmartContracts"
local_dir = "./contracts_dataset_pass_08"

def wait_with_timer(seconds):
    start = time.time()
    bar_width = 40
    while True:
        elapsed = time.time() - start
        remaining = seconds - elapsed
        if remaining <= 0:
            break
        progress = elapsed / seconds
        filled = int(bar_width * progress)
        bar = "█" * filled + "░" * (bar_width - filled)
        print(f"\r[{bar}] {elapsed:.1f}s / {seconds}s - Riprendo tra {remaining:.1f}s", end="", flush=True)
        time.sleep(0.1)
    print()

while True:
    try:
        snapshot_download(
            repo_id=repo_id,
            repo_type="dataset",
            local_dir=local_dir,
        )
        print(f"Dataset scaricato in {Path(local_dir).resolve()}")
        break
    except Exception as e:
        print(f"\nErrore: {e}")
        print("Rate limit raggiunto, aspetto 5 minuti...")
        wait_with_timer(300)
        print("Riprendo il download...")