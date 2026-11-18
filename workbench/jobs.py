"""Persistent jobs, a fixed operation registry, and ownership of the allocated Colab VM."""
import fcntl
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from studio import Project, load_json, pending, save_json

STATE = Path(os.getenv("COLAB_WORKBENCH_STATE", Path.home() / ".local/share/colab-video-workbench"))
CLI = HERE / "colab.sh"
ACTIVE = {"queued", "running"}
ACTIONS = {"generate", "qa", "approve", "cut", "release"}
ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
URL = re.compile(r"https?://[^\s\"'<>]+")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def clean(value):
    text = URL.sub("[链接已隐藏]", ANSI.sub("", value))
    return re.sub(r"(?i)(authorization:\s*bearer|access_token|refresh_token|client_secret|id_token)([=: ]+)\S+",
                  r"\1\2[已隐藏]", text)


def project_path(name):
    if not isinstance(name, str) or not ID.fullmatch(name):
        raise ValueError("无效的项目 ID")
    return STATE / "projects" / name / "project.json"


def job_path(job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{16}", job_id):
        raise ValueError("无效的任务 ID")
    return STATE / "jobs" / job_id / "job.json"


def list_jobs():
    return sorted((load_json(path) for path in (STATE / "jobs").glob("*/job.json")),
                  key=lambda row: row["created"], reverse=True)


def cli(args):
    result = subprocess.run([str(CLI), *args], capture_output=True, stdin=subprocess.DEVNULL,
                            text=True, errors="replace", timeout=35)
    output = clean(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(output.strip()[-1400:] or "Colab CLI 返回错误")
    return output


def usage():
    output = cli(["usage"])
    fields = {}
    for key, pattern, convert in [
        ("balance", r"Current balance:\s*([\d.,]+) compute units", float),
        ("rate", r"Usage rate:\s*([\d.,]+)/hr", float),
        ("assignments", r"Active assignments:\s*(\d+)", int),
    ]:
        match = re.search(pattern, output)
        if not match:
            raise RuntimeError("Colab 未返回可识别的算力数据")
        fields[key] = convert(match[1].replace(",", ""))
    return fields


@contextmanager
def gate(name, blocking=True):
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / name).open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def live(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].strip()[0] != "Z"
    except (OSError, IndexError):
        return False


def recover_jobs():
    # The worker owns the state after startup. An orphan only exposes its own runtime for cleanup.
    for row in list_jobs():
        if row["status"] in ACTIVE and time.time() - row["updated"] > 12 and not live(row.get("pid", 0)):
            row.update(status="interrupted", error="后台进程已中断，可重新提交。若运行时仍占用，请释放。",
                       updated=time.time())
            save_json(job_path(row["id"]), row)


def enqueue(data):
    action = data.get("action")
    if action not in ACTIONS:
        raise ValueError("未知的任务")
    with gate("control.lock"):
        recover_jobs()
        rows = list_jobs()
        if any(row["status"] in ACTIVE for row in rows):
            raise ValueError("正在执行任务，请等待或取消当前任务")
        job_id = uuid4().hex[:16]
        if action == "release":
            target = load_json(job_path(data.get("target")))
            if not target.get("may_own_runtime") or not target.get("session"):
                raise ValueError("没有待释放的工作台运行时")
            row = {"session": target["session"], "target": target["id"]}
        else:
            name = data.get("project")
            project = Project(project_path(name))
            ids = data.get("shots", [])
            if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                raise ValueError("无效的镜头列表")
            shots = project.select(ids)
            if action == "generate":
                if any(old.get("may_own_runtime") for old in rows):
                    raise ValueError("请先释放上一个未确认释放的运行时")
                shots = pending(shots)
                if not shots:
                    raise ValueError("选中的镜头已经生成，可从审片区播放")
            row = {"project": name, "shots": [shot.id for shot in shots],
                   "session": f"cvw-{job_id}" if action == "generate" else None}
        row.update(id=job_id, action=action, status="queued", step="queued",
                   created=time.time(), updated=time.time(), may_own_runtime=False)
        path = job_path(job_id)
        save_json(path, row)
        try:
            child = subprocess.Popen([sys.executable, str(__file__), job_id], cwd=ROOT,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as error:
            row.update(status="failed", error=str(error), updated=time.time())
            save_json(path, row)
            raise
        # Worker takes the control lock before taking ownership of this record.
        row["pid"] = child.pid
        save_json(path, row)
        return row


class Cancelled(Exception):
    pass


def execute(path, row, tag, args, limit, log, cancellable=True, check=True):
    row.update(step=tag, updated=time.time())
    save_json(path, row)
    log.write((f"\n[{tag}]\n").encode())
    child = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace",
                             start_new_session=True)

    def stream_output():
        with child.stdout as stream:
            written = 0
            for line in stream:
                if written < 300_000:
                    payload = clean(line).encode()
                    log.write(payload)
                    written += len(payload)

    reader = threading.Thread(target=stream_output, daemon=True)
    reader.start()
    deadline = time.monotonic() + limit
    try:
        while child.poll() is None:
            if cancellable and path.with_name("cancel").exists():
                raise Cancelled("任务已取消")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{tag} 超时")
            time.sleep(.4)
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        reader.join(timeout=4)
    if check and child.returncode:
        raise RuntimeError(f"{tag} 失败（{child.returncode}）。请查看任务日志。")
    return child.returncode


def generate(path, row, log):
    manifest = project_path(row["project"])
    project = Project(manifest)
    current = pending(project.select(row["shots"]))
    if not current:
        return
    if path.with_name("cancel").exists():
        raise Cancelled("任务已取消")
    execute(path, row, "auth", [str(CLI), "usage"], 45, log)
    if path.with_name("cancel").exists():
        raise Cancelled("任务已取消")
    row["may_own_runtime"] = True
    try:
        # Finish the allocation request before reacting to cancel so stop can find the assignment.
        execute(path, row, "allocate", [str(CLI), "new", "-s", row["session"], "--gpu", "G4"], 105, log,
                cancellable=False)
        if path.with_name("cancel").exists():
            raise Cancelled("任务已取消")
        identity = STATE / "ssh_ed25519"
        args = [sys.executable, "-u", str(ROOT / "cloud.py"), str(manifest), *row["shots"],
                "--session", row["session"], "--cli", str(CLI), "--identity", str(identity),
                "--timeout-minutes", "90"]
        execute(path, row, "render", args, 5650, log)
        pending_after = pending(Project(manifest).select(row["shots"]))
        if pending_after:
            raise RuntimeError("部分镜头未成功回传")
    finally:
        clear = execute(path, row, "release", [str(CLI), "stop", "-s", row["session"]], 55,
                        log, cancellable=False, check=False)
        if not clear:
            row["may_own_runtime"] = False
            save_json(path, row)


def work(job_id):
    path = job_path(job_id)
    with gate("worker.lock"):
        with gate("control.lock"):
            row = load_json(path)
            row.update(status="running", pid=os.getpid(), updated=time.time())
            save_json(path, row)
        with path.with_name("events.log").open("ab", buffering=0) as log:
            try:
                action = row["action"]
                if action == "release":
                    target = load_json(job_path(row["target"]))
                    execute(path, row, "release", [str(CLI), "stop", "-s", row["session"]], 55,
                            log, cancellable=False)
                    target.update(may_own_runtime=False, updated=time.time())
                    save_json(job_path(target["id"]), target)
                else:
                    manifest = str(project_path(row["project"]))
                    if action == "generate":
                        generate(path, row, log)
                    elif action == "qa":
                        uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
                        execute(path, row, "qa", [uv, "run", "--python", "3.12", "--with",
                            "faster-whisper==1.2.1", "python", str(ROOT / "qa.py"), manifest, *row["shots"]], 900, log)
                    else:
                        args = [sys.executable, "-u", str(ROOT / "studio.py"), manifest, action]
                        execute(path, row, action, args + (row["shots"] if action == "approve" else []), 3600, log)
                if row["may_own_runtime"]:
                    raise RuntimeError("结果已处理，但 GPU 未确认释放")
                row.update(status="complete", step="done")
            except Cancelled as error:
                row.update(status="cancelled", error=str(error))
            except Exception as error:
                row.update(status="failed", error=clean(str(error))[-1000:])
            finally:
                row["updated"] = time.time()
                save_json(path, row)


def cancellation_requested(row):
    return row["status"] in ACTIVE and job_path(row["id"]).with_name("cancel").exists()


def job_details(job_id):
    path = job_path(job_id)
    row = load_json(path)
    logs = []
    for name in ("events.log",):
        file = path.with_name(name)
        try:
            with file.open("rb") as stream:
                stream.seek(max(0, file.stat().st_size - 16_000))
                logs.append(clean(stream.read().decode("utf-8", errors="replace")).replace("\r", "\n"))
        except FileNotFoundError:
            pass
    return dict(row, cancel_requested=cancellation_requested(row), log="".join(logs)[-20000:])


if __name__ == "__main__":
    work(sys.argv[1])
