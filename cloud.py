#!/usr/bin/env python3
"""Send selected takes to an existing Colab session and fetch completed MP4s."""
import argparse
from dataclasses import replace
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
from uuid import uuid4

from studio import Project, atomic, digest, file_sha, load_json, media_info, pending

HERE = Path(__file__).resolve().parent
RUN = """#!/usr/bin/env bash
set -euo pipefail
cd "$1"
tar -xf "$2"
mkdir -p work "$3"
ln -s "$3" work/takes
shift 3
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
bash setup-h3.sh /content/video-h3
status=0
/content/video-h3/venv/bin/python -u render_h3.py project.json "$@" --runtime /content/video-h3 || status=$?
/usr/bin/python3.12 - "$@" <<'PY'
import sys
import tarfile
from pathlib import Path
from studio import Project
project = Project(Path("project.json"))
with tarfile.open("result.partial.tar", "w") as bundle:
    for job in project.select(sys.argv[1:]):
        for path in (job.media, job.folder / "render.json"):
            if path.is_file():
                bundle.add(path, arcname=path.relative_to(project.root).as_posix())
PY
mv result.partial.tar result.tar
exit "$status"
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


def import_results(project, jobs, path):
    found = set()
    with tarfile.open(path, "r") as bundle:
        items = {item.name: item for item in bundle if item.isfile()}
        for job in jobs:
            prefix = job.folder.relative_to(project.root).as_posix()
            clip, record = (items.get(f"{prefix}/{name}") for name in ("clip.mp4", "render.json"))
            if not clip or not record:
                continue
            with tempfile.TemporaryDirectory(dir=project.root) as staging:
                staging = Path(staging)
                for member in (clip, record):
                    atomic(staging / Path(member.name).name, bundle.extractfile(member).read())
                meta = load_json(staging / "render.json")
                if meta["key"] != job.key or file_sha(staging / "clip.mp4") != meta["sha256"]:
                    raise ValueError(f"{job.id}: transferred take does not match the local request.")
                if (job.folder / "render.json").exists():
                    media_info(job)
                    if file_sha(job.media) != meta["sha256"]:
                        raise ValueError(f"{job.id}: an existing take has different contents.")
                else:
                    media_info(replace(job, folder=staging, media=staging / "clip.mp4"))
                    job.folder.mkdir(parents=True, exist_ok=True)
                    (staging / "clip.mp4").replace(job.media)
                    (staging / "render.json").replace(job.folder / "render.json")
            found.add(job.id)
            print(f"Fetched and verified {job.id} ({job.key[:8]})", flush=True)
    return found


def run(args):
    project = Project(args.project)
    jobs = pending(project.select(args.ids))
    if not jobs:
        return
    spec = dict(project.data, shots=[job.row for job in jobs], target_seconds=sum(job.row["seconds"] for job in jobs))
    spec.pop("music", None)
    actors = {name for job in jobs for name in job.row["cast"]}
    spec["characters"] = {name: actor for name, actor in spec["characters"].items() if name in actors}
    key = digest(str(args.project.resolve()))[:20]
    token = uuid4().hex[:16]
    remote = f"/content/video-studio/{key}/{token}"
    cache = f"/content/video-studio/{key}/takes"
    remote_bundle = f"/content/video-bundle-{token}.tar"
    print(f"{len(jobs)} pending shots · runtime {args.session} · project {key}", flush=True)
    with tempfile.TemporaryDirectory() as directory:
        bundle_path = Path(directory) / "request.tar"
        with tarfile.open(bundle_path, "w") as tar:
            for name in ("studio.py", "render_h3.py", "setup-h3.sh"):
                tar.add(HERE / name, arcname=name)
            add_bytes(tar, "run.sh", RUN.encode())
            paths = {Path(item[field]) for item in spec["characters"].values() for field in ("image", "voice")
                     if field in item}
            if any(path.parts[0] in {"work", "dist"} or path.as_posix() in {
                "studio.py", "render_h3.py", "setup-h3.sh", "run.sh", "project.json", "result.tar", "result.partial.tar"
            } for path in paths):
                raise ValueError("Place reference assets in their own subfolder, for example assets/.")
            for path in sorted(paths):
                tar.add((project.root / path).resolve(), arcname=path.as_posix())
            add_bytes(tar, "project.json", json.dumps(spec, ensure_ascii=False).encode())
        call(args, ["upload", "-s", args.session, str(bundle_path), remote_bundle], check=True)
        # The archive was assembled above, and all remote shell words are quoted.
        command = f"mkdir -p {shlex.quote(remote)} && " + shlex.join([
            "tar", "-xf", remote_bundle, "-C", remote, "run.sh",
        ]) + " && " + shlex.join([
            "timeout", f"{args.timeout_minutes}m", "bash", f"{remote}/run.sh",
            remote, remote_bundle, cache, *(job.id for job in jobs),
        ])
        result = ssh(args, [command], timeout=args.timeout_minutes * 60 + 120)
        download = Path(directory) / "result.tar"
        with download.open("wb") as stream:
            fetched = ssh(args, ["cat", f"{remote}/result.tar"], stdout=stream, timeout=600)
        if fetched.returncode:
            raise RuntimeError(f"Colab returned {result.returncode}; no result archive was available.")
        verified = import_results(project, jobs, download)
        missing = [job.id for job in jobs if job.id not in verified]
        if result.returncode or missing:
            raise RuntimeError(f"Colab returned {result.returncode}. Still pending: {', '.join(missing) or 'none'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("ids", nargs="+")
    parser.add_argument("--session", required=True)
    parser.add_argument("--cli", default="colab")
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--timeout-minutes", type=int, default=90)
    args = parser.parse_args()
    try:
        if args.timeout_minutes < 1:
            raise ValueError("Timeout must be positive.")
        run(args)
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(f"Cloud job failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
