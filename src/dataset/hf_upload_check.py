"""
    Script made to double check dataset loading on huggingface using the following command:
    hf upload-large-folder Giovanni-P/SmartContracts ./contracts_dataset_pass_08_copy --repo-type=dataset
"""

"""
from huggingface_hub import HfApi
from pathlib import Path

import os
from tqdm import tqdm



repo_id = "Giovanni-P/SmartContracts"
local_dir = "./contracts_dataset_pass_08_copy"

api = HfApi()

remote_files = list(api.list_repo_files(repo_id=repo_id, repo_type="dataset"))
remote_folders = set(f.split("/")[0] for f in remote_files if "/" in f)
local_folders = [f for f in os.listdir(local_dir) if f != ".cache"]
local_count = sum(1 for f in Path(local_dir).rglob("*") if f.is_file() and ".cache" not in f.parts)

print(f"Cartelle locali:  {len(local_folders)}")
print(f"Cartelle remote:  {len(remote_folders)}")

print(f"Files locali:  {local_count}")
print(f"Files remoti:  {len(remote_files)}")
"""

"""
api = HfApi()

api = HfApi()
api.grant_access(
    repo_id="Giovanni-P/SmartContracts",
    repo_type="dataset",
    user="RageOfTheDucks"
)
"""


from huggingface_hub import HfApi
from pathlib import Path

import os
from tqdm import tqdm

api = HfApi()

requests = api.list_accepted_access_requests(
    repo_id="Giovanni-P/SmartContracts",
    repo_type="dataset"
)

for r in requests:
    print(r)