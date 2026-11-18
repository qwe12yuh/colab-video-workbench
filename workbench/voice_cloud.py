#!/usr/bin/env python3
"""Send one voice synthesis request to an allocated Colab session and fetch the WAV takes."""
import argparse
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
from uuid import uuid4

HERE = Path(__file__).resolve().parent
# A private venv keeps the Colab image's own packages untouched.
REMOTE = """set -euo pipefail
cd "$1"
tar -xf "$2"
if [ ! -x /content/qwen-tts/bin/python ]; then
  echo "[voice] installing Qwen3-TTS (first run on this VM)"
  uv venv -q --python 3.12 /content/qwen-tts
fi
/content/qwen-tts/bin/python -c 'import qwen_tts, soundfile' 2>/dev/null || \\
  uv pip install -q --python /content/qwen-tts/bin/python -U qwen-tts soundfile
/content/qwen-tts/bin/python -u voice_tts.py request.json out
tar -cf result.partial.tar -C out .
mv result.partial.tar result.tar
"""


def call(args, argv, **kwargs):
    return subprocess.run([args.cli, *argv], timeout=kwargs.pop("timeout", 180), **kwargs)


def ssh(args, command, **kwargs):
    identity = ["-i", str(args.identity)] if args.identity else []
    return call(args, ["ssh", "-s", args.session, *identity, "--", *command], **kwargs)


def add_bytes(bundle, name, content):
    member = tarfile.TarInfo(name)
    member.size = len(content)
    bundle.addfile(member, io.BytesIO(content))


def run(args):
    request = json.loads(args.request.read_text(encoding="utf-8"))
    token = uuid4().hex[:12]
    remote, bundle_remote = f"/content/voice-{token}", f"/content/voice-{token}.tar"
    with tempfile.TemporaryDirectory() as directory:
        bundle = Path(directory) / "request.tar"
        with tarfile.open(bundle, "w") as tar:
            tar.add(HERE / "voice_tts.py", arcname="voice_tts.py")
            add_bytes(tar, "run.sh", REMOTE.encode())
            if request.get("ref"):
                source = Path(request.pop("ref_file"))
                request["ref"] = "ref" + source.suffix.lower()
                tar.add(source, arcname=request["ref"])
            add_bytes(tar, "request.json", json.dumps(request, ensure_ascii=False).encode())
        print("[voice] uploading request", flush=True)
        call(args, ["upload", "-s", args.session, str(bundle), bundle_remote], check=True)
        command = f"mkdir -p {shlex.quote(remote)} && " + shlex.join(["tar", "-xf", bundle_remote, "-C", remote, "run.sh"]) \
            + " && " + shlex.join(["timeout", "25m", "bash", f"{remote}/run.sh", remote, bundle_remote])
        result = ssh(args, [command], timeout=27 * 60)
        if result.returncode:
            raise RuntimeError(f"Colab 合成返回 {result.returncode}")
        download = Path(directory) / "result.tar"
        with download.open("wb") as stream:
            if ssh(args, ["cat", f"{remote}/result.tar"], stdout=stream, timeout=300).returncode:
                raise RuntimeError("没有取回合成结果")
        args.out.mkdir(parents=True, exist_ok=True)
        with tarfile.open(download) as bundle_in:
            for item in bundle_in:
                name = Path(item.name).name
                if item.isfile() and name.startswith("take") and name.endswith(".wav") and item.size < 40_000_000:
                    (args.out / name).write_bytes(bundle_in.extractfile(item).read())
                    print(f"[voice] fetched {name}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--cli", default="colab")
    parser.add_argument("--identity", type=Path)
    args = parser.parse_args()
    try:
        run(args)
    except (ValueError, RuntimeError, OSError, KeyError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(f"Voice job failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
