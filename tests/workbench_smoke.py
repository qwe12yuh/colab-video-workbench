"""Exercise the desktop service and persistent local jobs without Colab allocation."""
import base64
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def exercise_gateway(request):
    # Test the gateway transport without consuming a user's subscription.
    class CompatibleAPI(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, value):
            payload = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/v1/models":
                self.reply({"data":[{"id":"local/test","display_name":"Test"}]})
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.headers.get("Authorization") == "Bearer local-test-key"
            assert data["model"] == "local/test" and data["messages"][-1]["content"] == "女子端杯"
            self.reply({"choices":[{"message":{"content":"女子端起咖啡杯，镜头慢慢推进。"}}]})

    with ThreadingHTTPServer(("127.0.0.1", 0), CompatibleAPI) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_port
            profile = {"kind":"api","name":"本机测试模型","base_url":f"http://127.0.0.1:{port}/v1",
                       "key":"local-test-key","model":"local/test"}
            models = request("/api/gateway/models",profile)
            assert models["models"] == [{"id":"local/test","label":"Test"}]
            state = request("/api/gateway/save",profile)
            assert state["active"] and state["profiles"][0]["has_key"]
            assert "local-test-key" not in json.dumps(state)
            assert "local-test-key" not in json.dumps(request("/api/gateway"))
            result = request("/api/gateway/polish",{"prompt":"女子端杯"})
            assert result == {"prompt":"女子端起咖啡杯，镜头慢慢推进。","model":"local/test"}
        finally:
            server.shutdown()
            thread.join()


def smoke(temp):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    env = dict(os.environ, COLAB_WORKBENCH_STATE=str(temp))
    proc = None

    def request(path, data=None, allowed=True, request_headers=None):
        headers = dict(request_headers or {})
        encoded = None
        if data is not None:
            headers.update({"Content-Type": "application/json", "X-Workbench": "1",
                            "Origin": origin if allowed else "https://unrelated.invalid"})
            encoded = json.dumps(data).encode()
        with urlopen(Request(origin + path, data=encoded, headers=headers), timeout=5) as response:
            payload = response.read()
            if request_headers:
                return response.status, payload
            return json.loads(payload)

    def failed(code, path, value, allowed=True):
        try:
            request(path, value, allowed)
        except HTTPError as error:
            assert error.code == code
            return
        raise AssertionError(f"Expected HTTP {code}")

    def start():
        child = subprocess.Popen([sys.executable, str(ROOT / "workbench/app.py"), "serve",
                                  "--port", str(port)], env=env)
        for _ in range(70):
            try:
                if request("/api/health")["app"] == "colab-video-workbench":
                    return child
            except OSError:
                time.sleep(.1)
        child.terminate()
        child.wait(timeout=5)
        raise RuntimeError("Could not start test server.")

    try:
        proc = start()
        assert {row["id"] for row in request("/api/projects")} == {"demo"}
        def wait_job(job):
            for _ in range(120):
                state = request("/api/job/" + job["id"])
                if state["status"] not in {"queued", "running"}:
                    assert state["status"] == "complete", state
                    return state
                time.sleep(.15)
            raise AssertionError(f"Job {job['id']} did not finish")

        wait_job(request("/api/run", {"action":"approve","project":"demo","shots":["s01","s02"]}))
        wait_job(request("/api/run", {"action":"cut","project":"demo"}))
        demo = request("/api/project/demo")
        code, prefix = request(demo["videos"][0]["url"], request_headers={"Range": "bytes=0-31"})
        assert code == 206 and len(prefix) == 32 and b"ftyp" in prefix
        assert demo["exports"][0]["current"] and not demo["exports"][0]["has_subtitles"]
        edited = demo["manifest"]
        edited["shots"][0]["native_captions"] = False
        edited["shots"][0]["subtitles"] = [{"text":"测试字幕","start":0.5,"end":1.5}]
        revised = request("/api/project/demo", {"revision":demo["revision"],"manifest":edited})
        assert revised["videos"][0]["state"] == "approved" and not revised["exports"][0]["current"]

        asset = ROOT / "examples/gpu-smoke/assets/reference.png"
        prompt = "参考图人物望向窗外，轻轻微笑，镜头缓慢前推。"
        params = {"prompt":prompt,"extension":".png","base64":base64.b64encode(asset.read_bytes()).decode()}
        quick = request("/api/quick",params)
        assert quick["manifest"]["aspect"] == "portrait"
        assert quick["manifest"]["shots"][0]["action"] == prompt
        assert quick["manifest"]["shots"][0]["input_mode"] == "reference_prompt"
        assert quick["manifest"]["shots"][0]["seconds"] == 6 and quick["videos"][0]["state"] == "pending"
        spec = temp / "projects" / quick["id"] / "project.json"
        sys.path.insert(0, str(ROOT))
        from studio import Project
        video = Project(spec).jobs["s01"]
        assert prompt in video.prompt and video.refs[0][1].read_bytes() == asset.read_bytes()
        before = len(request("/api/projects"))
        failed(400, "/api/quick", dict(params,base64=base64.b64encode(b"broken image").decode()))
        assert len(request("/api/projects")) == before

        exercise_gateway(request)
        failed(403, "/api/new", {"title":"test"}, allowed=False)
        created = request("/api/new", {"title":"测试项目"})
        name = created["id"]
        image = ROOT / "examples/gpu-smoke/assets/reference.png"
        upload = request(f"/api/asset/{name}", {"extension":".png", "base64":base64.b64encode(image.read_bytes()).decode()})
        manifest = created["manifest"]
        manifest["characters"]["subject"]["image"] = upload["path"]
        manifest["shots"][0]["seconds"] = 2
        failed(400, f"/api/project/{name}", {"revision":created["revision"],"manifest":manifest})
        assert request(f"/api/project/{name}")["manifest"]["shots"][0]["seconds"] == 6
        manifest["shots"][0]["seconds"] = 6
        manifest["shots"][0]["action"] = "The actor closes the folder and looks at the camera."
        saved = request(f"/api/project/{name}", {"revision":created["revision"],"manifest":manifest})
        assert created["revision"] != saved["revision"]
        failed(400, f"/api/project/{name}", {"revision":created["revision"],"manifest":manifest})
        job = request("/api/run", {"action":"approve","project":"demo","shots":["s01"]})
        proc.terminate()
        proc.wait(timeout=5)
        proc = start()
        state = wait_job(job)
        assert "Approved s01" in state["log"]
        print("Workbench PASS: quick reference images, gateway transport, current export, CSRF, media range, job persistence.")
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="workbench-smoke-") as directory:
        smoke(Path(directory))
