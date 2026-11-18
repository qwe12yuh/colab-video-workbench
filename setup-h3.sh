#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 1 ]; then
  echo "Usage: bash setup-h3.sh /absolute/runtime-directory" >&2
  exit 2
fi
runtime="$(realpath -m "$1")"
pika=f961d7ed09a5932b7533559211b7d233e8f1c8e8
diffusers=3a2f35d4efa4c059c8bfb3bc0d6c906264895c81
command -v uv >/dev/null
command -v ffprobe >/dev/null
mkdir -p "$runtime"
exec 9>"$runtime/setup.lock"
flock -n 9
bootstrap_python="${H3_PYTHON:-/usr/bin/python3.12}"
if [ ! -x "$runtime/venv/bin/python" ]; then
  uv venv --python "$bootstrap_python" "$runtime/venv"
fi
py="$runtime/venv/bin/python"
if ! "$py" -c 'import torch, torchvision; assert torch.__version__ == "2.14.1+cu130" and torchvision.__version__ == "0.29.1+cu130"' 2>/dev/null; then
  uv pip install --python "$py" --index https://download.pytorch.org/whl/cu130 'torch==2.14.1+cu130' 'torchvision==0.29.1+cu130'
fi
"$py" - <<'PY'
import sys
import torch
if sys.version_info[:2] != (3, 12):
    raise SystemExit("Use the Python 3.12 Colab image.")
if not torch.cuda.is_available() or torch.cuda.get_device_properties(0).total_memory < 76 * 2**30:
    raise SystemExit("This workflow needs an 80 GB or 96 GB class GPU.")
print(torch.__version__, torch.cuda.get_device_name(), flush=True)
PY
command -v uv >/dev/null
command -v ffprobe >/dev/null
if [ ! -d "$runtime/pika/.git" ]; then
  git init -q "$runtime/pika"
  git -C "$runtime/pika" remote add origin https://github.com/Pika-Labs/Pika-H3-Flex.git
fi
if [ "$(git -C "$runtime/pika" rev-parse --verify HEAD 2>/dev/null || true)" != "$pika" ]; then
  git -C "$runtime/pika" fetch --depth 1 origin "$pika"
  git -C "$runtime/pika" checkout -q --detach FETCH_HEAD
fi

if ! "$py" -c 'import av, peft; from pika_h3.runtime import validate_runtime; validate_runtime()' 2>/dev/null; then
  uv pip install --python "$py" --extra-index-url https://download.pytorch.org/whl/cu130 --index-strategy unsafe-best-match \
    "diffusers @ git+https://github.com/huggingface/diffusers.git@$diffusers" \
    'transformers==5.22.1' 'peft==0.20.0' 'av==19.0.2' 'accelerate==1.15.0' \
    'safetensors==0.8.0' 'huggingface-hub==1.33.0' 'torchao==0.16.0' \
    'numpy==2.4.6' 'pillow==11.3.0' 'pyyaml==6.0.3'
fi
"$py" "$runtime/pika/tools/apply_diffusers_patches.py"
uv pip install --python "$py" --no-deps -e "$runtime/pika"
PYTHONUNBUFFERED=1 "$py" - "$runtime" <<'PY'
import sys
from pathlib import Path
import av
import peft
from huggingface_hub import snapshot_download
from pika_h3.runtime import validate_runtime
from pika_h3.pipeline import resolve_assets, BASE_REPO, BASE_REVISION
from types import SimpleNamespace

validate_runtime()
root = Path(sys.argv[1])
print("Downloading or resuming pinned Ref2VA-only weights (about 135 GiB).", flush=True)
snapshot_download(BASE_REPO, revision=BASE_REVISION, local_dir=root/"base", token=False, max_workers=6,
    allow_patterns=["transformer_ref/*", "text_encoder/*", "tokenizer/*", "processor/*",
                    "vae/*", "audio_vae/*", "*.json", "LICENSE"])
adapter = root/"adapter"
revision = "ca45d99f9b1d1859ffb9dc1cd6bfaaa994257f56"
snapshot_download("Pika-Labs/Pika-H3-Flex", revision=revision, local_dir=adapter, token=False,
    allow_patterns=["ref2va/*", "LICENSE", "NOTICE", "licenses/*"])
resolve_assets(SimpleNamespace(task="ref2va", base_path=root/"base", adapter_path=adapter/"ref2va"))
print(f"Ready: {root}", flush=True)
PY
