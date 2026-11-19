"""Voice jobs end to end with a fake Colab CLI (small-GPU grab, upload, fetch) and a fake TTS API."""
import base64
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CLI = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >>"$MOCK_DIR/calls"
case "$1" in
  usage) printf 'Current balance: 10.00 compute units\nUsage rate: 0/hr\nActive assignments: 0\n' ;;
  new|stop|upload) exit 0 ;;
  ssh) shift; while [ "$1" != "--" ]; do shift; done; shift
       if [ "$1" = nvidia-smi ]; then echo "NVIDIA L4, 23034"
       elif [ "$1" = cat ]; then cat "$MOCK_DIR/result.tar"
       fi ;;
  *) exit 1 ;;
esac
'''


def wav(path, seconds):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
                    str(path)], check=True)


def main(temp):
    mock = temp / "mock"
    mock.mkdir()
    (mock / "cli").write_text(CLI)
    (mock / "cli").chmod(0o755)
    takes = mock / "takes"
    takes.mkdir()
    wav(takes / "take1.wav", 15)
    wav(takes / "take2.wav", 1)  # too short, must be dropped
    subprocess.run(["tar", "-cf", str(mock / "result.tar"), "-C", str(takes), "."], check=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    env = dict(os.environ, COLAB_WORKBENCH_STATE=str(temp / "state"), COLAB_WORKBENCH_CLI=str(mock / "cli"),
               MOCK_DIR=str(mock))

    def request(path, data=None):
        headers = {"Content-Type": "application/json", "X-Workbench": "1", "Origin": origin} if data is not None else {}
        body = json.dumps(data).encode() if data is not None else None
        with urlopen(Request(origin + path, data=body, headers=headers), timeout=10) as response:
            return json.load(response)

    def wait(job):
        for _ in range(200):
            state = request("/api/job/" + job["id"])
            if state["status"] not in {"queued", "running"}:
                return state
            time.sleep(.15)
        raise AssertionError("voice job did not finish")

    server = subprocess.Popen([sys.executable, str(ROOT / "workbench/app.py"), "serve", "--port", str(port)], env=env)
    try:
        for _ in range(70):
            try:
                request("/api/health")
                break
            except OSError:
                time.sleep(.1)
        catalog = request("/api/voice")
        assert {row["id"] for row in catalog["models"]} >= {"qwen3-design", "qwen3-custom", "qwen3-clone", "api"}
        assert catalog["plan"]["gpus"] == ["L4", "T4"]
        actor = next(iter(request("/api/project/demo")["manifest"]["characters"]))
        job = request("/api/run", {"action": "voice", "project": "demo", "actor": actor, "model": "qwen3-design",
                                   "text": "你好，很高兴见到你，我们好好聊一聊。", "instruct": "二十多岁的女性，声音温柔",
                                   "count": 2})
        assert job["session"] and job["gpu_plan"]["gpus"] == ["L4", "T4"]
        state = wait(job)
        assert state["status"] == "complete", state
        assert len(state["voice_candidates"]) == 1 and state["voice_candidates"][0]["seconds"] <= 12.05, state
        assert not state["may_own_runtime"] and state["gpu"]["gpu"] == "L4"
        calls = (mock / "calls").read_text()
        assert "new -s" in calls and "--gpu L4" in calls and "stop -s" in calls and "--high-mem" not in calls
        stored = temp / "state/projects/demo" / state["voice_candidates"][0]["path"]
        assert stored.is_file()

        class TTS(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert self.path == "/v1/audio/speech" and body["voice"] == "nova" and body["response_format"] == "wav"
                payload = (takes / "take1.wav").read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with ThreadingHTTPServer(("127.0.0.1", 0), TTS) as api:
            threading.Thread(target=api.serve_forever, daemon=True).start()
            saved = request("/api/voice/save", {"base_url": f"http://127.0.0.1:{api.server_port}/v1", "model": "tts-1",
                                                "voice": "nova", "key": "secret-key"})
            assert saved["has_key"] and "secret-key" not in json.dumps(request("/api/voice"))
            state = wait(request("/api/run", {"action": "voice", "project": "demo", "actor": actor, "model": "api",
                                              "text": "测试一下兼容接口的音色合成。", "count": 1}))
            assert state["status"] == "complete" and not state.get("session") and len(state["voice_candidates"]) == 1, state
            api.shutdown()
        print("Voice PASS: small-GPU grab, Colab fetch, take trimming, short-take filter, release, compatible TTS API.")
    finally:
        server.terminate()
        server.wait(timeout=5)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="voice-job-") as directory:
        main(Path(directory))
