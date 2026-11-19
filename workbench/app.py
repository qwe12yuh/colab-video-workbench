#!/usr/bin/env python3
"""Colab Video Workbench local service. Run inside Ubuntu WSL."""
import argparse
import base64
import binascii
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
from uuid import uuid4

import jobs
import gateway
import gpu
import director
from jobs import STATE, ROOT, HERE
from studio import Project, approved, atomic, digest, export_key, load_json, probe, save_json

PORT = 8765
APP = "colab-video-workbench"
WEB = {"/": ("index.html", "text/html; charset=utf-8"),
       "/app.js": ("app.js", "text/javascript; charset=utf-8"),
       "/style.css": ("style.css", "text/css; charset=utf-8")}
MIMES = {".mp4": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg",
         ".wav": "audio/wav", ".mp3": "audio/mpeg"}
HEX = re.compile(r"[a-f0-9]{16}")
MAX_BODY = 24 * 1024 * 1024


def safe_projects():
    for path in sorted((STATE / "projects").glob("*/project.json")):
        if jobs.ID.fullmatch(path.parent.name):
            yield path


def example(name, source):
    target = jobs.project_path(name).parent
    if target.exists():
        return
    template = ROOT / "examples" / source
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template / "assets", target / "assets")
    shutil.copy2(template / "project.json", target / "project.json")


def read_project(name):
    manifest = jobs.project_path(name)
    project = Project(manifest)
    videos = []
    for shot in project.jobs.values():
        found = shot.media.is_file() and (shot.source or (shot.folder / "render.json").is_file())
        qa = shot.folder / "asr.json"
        asr = load_json(qa) if qa.is_file() and found else None
        if asr and asr.get("trim_start", 0) != shot.row.get("trim_start", 0):
            asr = None
        videos.append({"id": shot.id, "key": shot.key[:8], "source": shot.source,
                       "state": "approved" if approved(shot) else "review" if found else "pending",
                       "url": f"/media/{name}/shot/{shot.id}?take={shot.key}" if found else None,
                       "asr": asr})
    exports = []
    reports = sorted((project.root / "dist").glob("*/report.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    current_export = export_key(project) if reports and all(row["state"] == "approved" for row in videos) else None
    for path in reports:
        key = path.parent.name
        if HEX.fullmatch(key) and (path.parent / "film.mp4").is_file():
            subtitles = path.parent / "timeline.srt"
            exports.append(dict(load_json(path), url=f"/media/{name}/export/{key}",
                                subtitles=f"/media/{name}/subtitles/{key}",
                                has_subtitles=subtitles.is_file() and subtitles.stat().st_size > 0,
                                current=key == current_export))
    return {"id": name, "manifest": project.data, "revision": digest(project.data),
            "videos": videos, "exports": exports}


def assert_idle(name=None):
    jobs.recover_jobs()
    if any(row["status"] in jobs.ACTIVE and (name is None or name == row.get("project"))
           for row in jobs.list_jobs()):
        raise ValueError("此项目正在执行任务。")


def new_project(data):
    title = data.get("title")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
        raise ValueError("请输入 1–100 字的项目名。")
    with jobs.gate("control.lock"):
        name = "edit-" + uuid4().hex[:12]
        example(name, "gpu-smoke")
        path = jobs.project_path(name)
        spec = load_json(path)
        spec["title"] = title.strip()
        shot = spec["shots"][0]
        shot.update(seconds=6, seed=int(uuid4().hex[:8], 16))
        spec["target_seconds"] = 6
        save_json(path, spec)
        return read_project(name)


def new_reference_project(data):
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 1800:
        raise ValueError("请输入 1–1800 字的视频要求。")
    prompt = prompt.strip()
    aspect, seconds = data.get("aspect", "portrait"), data.get("seconds", 6)
    if aspect not in {"portrait", "landscape"} or type(seconds) is not int or not 3 <= seconds <= 12:
        raise ValueError("请选择横屏或竖屏，以及 3–12 秒的时长。")
    extension = data.get("extension")
    if extension not in {".png", ".jpg"}:
        raise ValueError("参考图支持 PNG 和 JPEG。")
    image = base64.b64decode(data["base64"], validate=True)
    if not image or len(image) > 17_500_000:
        raise ValueError("参考图须小于 17.5 MB。")
    name = "quick-" + uuid4().hex[:12]
    target = jobs.project_path(name)
    target.parent.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".reference-", dir=target.parent.parent) as directory:
        root = Path(directory)
        relative = "assets/reference" + extension
        atomic(root / relative, image)
        try:
            streams = probe(root / relative)
        except subprocess.CalledProcessError:
            raise ValueError("参考图无法解码。请重新选择 PNG 或 JPEG。") from None
        codec = "png" if extension == ".png" else "mjpeg"
        if not any(row.get("codec_name") == codec and 0 < row.get("width", 0) <= 8192
                   and 0 < row.get("height", 0) <= 8192 for row in streams):
            raise ValueError("请上传有效的 PNG 或 JPEG 参考图，长宽均须小于或等于 8192 像素。")
        title = re.sub(r"\s+", " ", prompt)[:24].strip()
        manifest = {
            "title": title,
            "aspect": aspect,
            "style": "Follow the visual style and camera direction in the user's request.",
            "characters": {"subject": {"description": "the primary subject from the uploaded reference image",
                                       "image": relative}},
            "locations": {"scene": "Use the requested setting, matching the reference where appropriate."},
            "shots": [{"id": "s01", "title": "参考图生成", "seconds": seconds, "location": "scene",
                       "cast": ["subject"], "action": prompt, "input_mode": "reference_prompt",
                       "seed": int(uuid4().hex[:8], 16)}],
            "target_seconds": seconds,
        }
        save_json(root / "project.json", manifest)
        Project(root / "project.json")
        root.rename(target.parent)
    return read_project(name)


def save_project(name, data):
    with jobs.gate("control.lock"):
        assert_idle(name)
        file = jobs.project_path(name)
        original = Project(file)
        if data.get("revision") != digest(original.data):
            raise ValueError("项目已在另一窗口改动，请刷新后再保存。")
        staging = file.with_name(".validate.json")
        try:
            save_json(staging, data["manifest"])
            Project(staging)
            staging.replace(file)
        finally:
            staging.unlink(missing_ok=True)
        return read_project(name)


def upload(name, data):
    extension = data.get("extension")
    if extension not in {".png", ".jpg", ".wav", ".mp3", ".mp4"}:
        raise ValueError("支持 PNG、JPEG、WAV、MP3 和 MP4。")
    payload = base64.b64decode(data["base64"], validate=True)
    if not payload or len(payload) > 17_500_000:
        raise ValueError("单个素材须小于 17.5 MB。")
    with jobs.gate("control.lock"):
        assert_idle(name)
        file = jobs.project_path(name)
        if not file.is_file():
            raise FileNotFoundError("项目不存在")
        filename = hashlib.sha256(payload).hexdigest()[:24] + extension
        path = file.parent / "assets" / filename
        if not path.exists():
            staging = path.with_name("." + filename)
            try:
                atomic(staging, payload)
                media_type = "audio" if extension in {".wav", ".mp3"} else "video"
                if not any(stream["codec_type"] == media_type for stream in probe(staging)):
                    raise ValueError("文件不包含所需的媒体流。")
                staging.replace(path)
            finally:
                staging.unlink(missing_ok=True)
        return {"path": f"assets/{filename}"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def access(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def send_data(self, status, body, content_type):
        self.send_response(status)
        self.headers_common(content_type, len(body))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def headers_common(self, content_type, size):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; media-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

    def json(self, status, value):
        self.send_data(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def media(self, parts):
        _, _, name, kind, key = parts
        project = Project(jobs.project_path(name))
        if kind == "shot":
            shot = project.jobs[key]
            file = shot.media
        elif kind in {"export", "subtitles"} and HEX.fullmatch(key):
            file = project.root / "dist" / key / ("film.mp4" if kind == "export" else "timeline.srt")
        elif kind == "asset" and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,80}", key):
            file = project.root / "assets" / key
        else:
            raise FileNotFoundError()
        if not file.resolve().is_relative_to(project.root) or not file.is_file():
            raise FileNotFoundError()
        size = file.stat().st_size
        start, end = 0, size - 1
        header = self.headers.get("Range")
        if header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
            if match and (match[1] or match[2]):
                if match[1]:
                    start = int(match[1])
                    end = min(int(match[2]), end) if match[2] else end
                else:
                    start = max(0, size - int(match[2]))
            if not match or not 0 <= start <= end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.headers_common("application/octet-stream", 0)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        self.send_response(206 if header else 200)
        self.headers_common(MIMES.get(file.suffix.lower(), "text/plain; charset=utf-8"), end-start+1)
        self.send_header("Accept-Ranges", "bytes")
        if header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with file.open("rb") as stream:
            stream.seek(start)
            remaining = end-start+1
            while remaining:
                block = stream.read(min(remaining, 256 * 1024))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def dispatch(self):
        if not self.access():
            return self.json(403, {"error": "本地工作台拒绝此来源。"})
        path = self.path.split("?", 1)[0]
        parts = path.split("/")
        if self.command in {"GET", "HEAD"}:
            if path == "/favicon.ico":
                return self.send_data(204, b"", "image/x-icon")
            if path in WEB:
                name, mimetype = WEB[path]
                return self.send_data(200, (HERE / "ui" / name).read_bytes(), mimetype)
            if path == "/api/health":
                return self.json(200, {"app": APP, "version": "1.2"})
            if path == "/api/agent-config":
                uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
                args = ["-d", os.environ.get("WSL_DISTRO_NAME", "Ubuntu-24.04"), "--",
                        "env", f"COLAB_WORKBENCH_PORT={self.server.server_port}",
                        uv, "run", "--python", "3.12", "--with", "mcp==1.27.0", "python",
                        str(HERE / "agent_mcp.py")]
                return self.json(200, {"mcpServers": {"colab-workbench": {"command": "wsl.exe", "args": args}}})
            if path == "/api/projects":
                return self.json(200, [{"id": p.parent.name, "title": load_json(p)["title"]} for p in safe_projects()])
            if path == "/api/jobs":
                with jobs.gate("control.lock"):
                    jobs.recover_jobs()
                    return self.json(200, [dict(row, cancel_requested=jobs.cancellation_requested(row))
                                           for row in jobs.list_jobs()[:35]])
            if path == "/api/gateway":
                return self.json(200, gateway.settings())
            if path == "/api/voice":
                import voice
                return self.json(200, dict(voice.catalog(), gpus=[dict(spec, id=key) for key, spec in gpu.TTS_GPUS.items()],
                                           plan=gpu.TTS_PLAN))
            if path == "/api/gpus":
                return self.json(200, {"gpus": [dict(spec, id=key) for key, spec in gpu.GPUS.items()],
                                       "default": gpu.DEFAULT_PLAN})
            if path == "/api/usage":
                return self.json(200, jobs.usage())
            if len(parts) == 4 and parts[1:3] == ["api", "project"]:
                return self.json(200, read_project(parts[3]))
            if len(parts) == 4 and parts[1:3] == ["api", "job"]:
                return self.json(200, jobs.job_details(parts[3]))
            if len(parts) == 5 and parts[1] == "media":
                return self.media(parts)
        else:
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if (self.headers.get("Origin") != origin or self.headers.get("X-Workbench") != "1"
                    or self.headers.get_content_type() != "application/json"):
                self.close_connection = True
                return self.json(403, {"error": "本地工作台拒绝此来源。"})
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= MAX_BODY:
                self.close_connection = True
                return self.json(413, {"error": "请求过大或为空。"})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("请求格式有误。")
            if path == "/api/gateway/models":
                return self.json(200, gateway.models(data))
            if path == "/api/gateway/save":
                return self.json(200, gateway.save(data))
            if path == "/api/gateway/polish":
                return self.json(200, gateway.polish(data))
            if path == "/api/voice/save":
                import voice
                return self.json(200, voice.save(data))
            if path == "/api/director/storyboard":
                return self.json(200, director.storyboard(data))
            if path == "/api/director/rewrite":
                return self.json(200, director.rewrite(data))
            if path == "/api/assistant":
                import agent
                return self.json(200, agent.run(data, read_project, lambda: [
                    {"id": p.parent.name, "title": load_json(p)["title"]} for p in safe_projects()]))
            if path == "/api/quick":
                return self.json(201, new_reference_project(data))
            if path == "/api/new":
                return self.json(201, new_project(data))
            if path == "/api/run":
                return self.json(202, jobs.enqueue(data))
            if path == "/api/cancel":
                with jobs.gate("control.lock"):
                    job_path = jobs.job_path(data.get("job"))
                    if load_json(job_path)["status"] not in jobs.ACTIVE:
                        raise ValueError("任务已结束。")
                    atomic(job_path.with_name("cancel"), b"cancel\n")
                return self.json(202, {"accepted": True})
            if len(parts) == 4 and parts[1:3] == ["api", "project"]:
                return self.json(200, save_project(parts[3], data))
            if len(parts) == 4 and parts[1:3] == ["api", "asset"]:
                return self.json(201, upload(parts[3], data))
        return self.json(404, {"error": "找不到此资源。"})

    def do_GET(self):
        try:
            self.dispatch()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except FileNotFoundError:
            self.json(404, {"error": "文件还没生成，或依赖未安装。"})
        except (ValueError, TypeError, KeyError, AttributeError, binascii.Error) as error:
            self.json(400, {"error": jobs.clean(str(error))[:1300]})
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            self.json(503, {"error": jobs.clean(str(error))[-1400:]})

    do_POST = do_HEAD = do_GET


def ready(port):
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
            return json.load(response).get("app") == APP
    except (OSError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["serve", "ensure"])
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-examples", action="store_true")
    args = parser.parse_args()
    if args.action == "serve":
        os.umask(0o077)
        if not args.no_examples:
            with jobs.gate("control.lock"):
                example("demo", "demo")
        with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
            server.serve_forever()
    else:
        with jobs.gate("launcher.lock"):
            if not ready(args.port):
                with (STATE / "server.log").open("ab") as log:
                    command = [sys.executable, str(__file__), "serve", "--port", str(args.port)]
                    if args.no_examples:
                        command.append("--no-examples")
                    process = subprocess.Popen(command,
                        stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
                for _ in range(45):
                    if ready(args.port):
                        break
                    if process.poll() is not None:
                        raise RuntimeError("工作台后台无法启动，请查看 server.log。")
                    time.sleep(.2)
                else:
                    raise RuntimeError("工作台后台启动超时。")
        print(f"http://127.0.0.1:{args.port}")


if __name__ == "__main__":
    main()
