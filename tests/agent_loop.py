"""Agent loop end to end: read tools run, a gated tool waits for approval, decisions are audited."""
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
JOB = "0123456789abcdef"


class Model(BaseHTTPRequestHandler):
    """Scripted model: decides the next step from what the transcript already contains."""
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = body["messages"][-1]["content"]
        if "用户审批：拒绝" in text:
            step = {"reply": "好的，不释放。可以稍后在任务区手动释放。", "tool_calls": [], "actions": []}
        elif "用户审批：批准 release_gpu" in text:
            step = {"reply": "已提交释放。下次可以把 H100 加进候选并增加轮数。", "tool_calls": [], "actions": [
                {"type": "set_gpu", "label": "改为 G4 → H100", "args_json": json.dumps({"gpus": ["G4", "H100"], "rounds": 5})}]}
        elif "工具结果（read_job）" in text:
            step = {"reply": "日志显示 G4 申请超时，GPU 还没确认释放，需要先释放。", "actions": [],
                    "tool_calls": [{"name": "release_gpu", "args_json": json.dumps({"job_id": JOB})}]}
        elif "工具结果（workbench_status）" in text:
            step = {"reply": "", "actions": [], "tool_calls": [{"name": "read_job", "args_json": json.dumps({"job_id": JOB})}]}
        else:
            step = {"reply": "", "actions": [], "tool_calls": [{"name": "workbench_status", "args_json": "{}"},
                                                              {"name": "rm_rf", "args_json": "{}"}]}
        payload = json.dumps({"choices": [{"message": {"content": json.dumps(step, ensure_ascii=False)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main(temp):
    state = temp / "state"
    job = state / "jobs" / JOB
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({
        "id": JOB, "action": "generate", "project": "demo", "shots": ["s01"], "session": "cvw-" + JOB,
        "status": "failed", "step": "release", "error": "allocate 超时", "may_own_runtime": True, "profile": "workbench",
        "created": time.time() - 60, "updated": time.time() - 50}))
    (job / "events.log").write_text("[allocate]\n[colab] Creating session...\n[抢卡] 申请超时\n")
    cli = temp / "cli"
    cli.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"$(dirname "$0")/calls"\nexit 0\n')
    cli.chmod(0o755)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"

    def call(path, data=None):
        headers = {"Content-Type": "application/json", "X-Workbench": "1", "Origin": origin} if data is not None else {}
        body = json.dumps(data).encode() if data is not None else None
        with urlopen(Request(origin + path, data=body, headers=headers), timeout=30) as response:
            return json.load(response)

    with ThreadingHTTPServer(("127.0.0.1", 0), Model) as model:
        threading.Thread(target=model.serve_forever, daemon=True).start()
        state.mkdir(exist_ok=True)
        (state / "gateway.json").write_text(json.dumps({"active": "m", "profiles": [{
            "id": "m", "kind": "api", "name": "fake", "model": "fake/agent", "key": "",
            "base_url": f"http://127.0.0.1:{model.server_port}/v1"}]}))
        env = dict(os.environ, COLAB_WORKBENCH_STATE=str(state), COLAB_WORKBENCH_CLI=str(cli))
        server = subprocess.Popen([sys.executable, str(ROOT / "workbench/app.py"), "serve", "--port", str(port)], env=env)
        try:
            for _ in range(70):
                try:
                    call("/api/health")
                    break
                except OSError:
                    time.sleep(.1)
            first = call("/api/assistant", {"message": "上次为什么失败了？", "state": {}})
            assert first["trace"] == ["workbench_status", "read_job"], first["trace"]
            assert first["pending"] == {"name": "release_gpu", "args": {"job_id": JOB}, "title": "释放 GPU"}, first
            assert "超时" in first["reply"]
            assert json.loads((job / "job.json").read_text())["may_own_runtime"]  # nothing ran yet
            denied = call("/api/assistant", {"session": first["session"], "transcript": first["transcript"],
                                             "decision": dict(first["pending"], allow=False)})
            assert denied["pending"] is None and "不释放" in denied["reply"]
            approved = call("/api/assistant", {"session": first["session"], "transcript": first["transcript"],
                                               "decision": dict(first["pending"], allow=True)})
            assert approved["actions"][0]["type"] == "set_gpu" and approved["actions"][0]["args"]["rounds"] == 5
            for _ in range(100):
                if not json.loads((job / "job.json").read_text())["may_own_runtime"]:
                    break
                time.sleep(.1)
            else:
                raise AssertionError("release job did not clear the runtime")
            assert "stop -s cvw-" + JOB in (temp / "calls").read_text()
            log = [json.loads(line) for line in (state / "assistant-audit.jsonl").read_text().splitlines()]
            assert [row["event"] for row in log] == ["request", "decision", "decision", "result"], log
            assert [row.get("allow") for row in log if row["event"] == "decision"] == [False, True]
            try:
                call("/api/assistant", {"transcript": [], "decision": {"name": "workbench_status", "allow": True}})
            except Exception as error:  # read tools never go through approval
                assert "400" in str(error)
            else:
                raise AssertionError("ungated decision accepted")
            print("Agent PASS: read tools, unknown tool ignored, approval gate, deny, approve-and-run, audit log.")
        finally:
            server.terminate()
            server.wait(timeout=5)
            model.shutdown()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="agent-loop-") as directory:
        main(Path(directory))
