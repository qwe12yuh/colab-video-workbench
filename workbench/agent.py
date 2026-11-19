"""Workbench agent: a small tool loop with one-time approvals, adapted from the DeepSeek Harness design.

Each model step returns structured JSON (works with compatible APIs and the Codex CLI alike): either
tool calls or a final reply with optional action cards. Read tools run immediately. Tools that spend
compute or change jobs stop the loop and return an approval request; nothing runs until the user
answers, each approval covers exactly one call, and every request and decision goes to an audit log.
The transcript travels with the client, so the server keeps no conversation state.
"""
import json
import os
import shutil
import subprocess
import time
from uuid import uuid4

import director
import gateway
import jobs
from jobs import STATE

MAX_STEPS = 5
AUDIT = STATE / "assistant-audit.jsonl"


def job_summary(row):
    return {key: row.get(key) for key in ("id", "action", "status", "step", "project", "shots", "gpu_note",
                                          "may_own_runtime", "created")} | {
        "error": (row.get("error") or "")[:300], "gpu": (row.get("gpu") or {}).get("name"),
        "voice_actor": (row.get("voice") or {}).get("actor")}


def tool_status(_args, ctx):
    jobs.recover_jobs()
    rows = jobs.list_jobs()
    return {"projects": ctx["projects"](), "jobs": [job_summary(row) for row in rows[:8]],
            "active": next((row["id"] for row in rows if row["status"] in jobs.ACTIVE), None),
            "unreleased_gpu_jobs": [row["id"] for row in rows if row.get("may_own_runtime")
                                    and row["status"] not in jobs.ACTIVE]}


def tool_read_job(args, _ctx):
    detail = jobs.job_details(args.get("job_id"))
    return job_summary(detail) | {"gpu_plan": detail.get("gpu_plan"), "log_tail": detail.get("log", "")[-5000:]}


def tool_read_project(args, ctx):
    data = ctx["read_project"](args.get("project_id"))
    manifest = data["manifest"]
    states = {row["id"]: row["state"] for row in data["videos"]}
    return {"id": data["id"], "title": manifest["title"], "aspect": manifest.get("aspect"),
            "style": manifest.get("style"),
            "characters": {key: {"label": actor.get("label", key), "has_voice": "voice" in actor}
                           for key, actor in manifest["characters"].items()},
            "shots": [{"id": row["id"], "title": row["title"], "seconds": row["seconds"], "state": states.get(row["id"]),
                       "cast": row.get("cast", []), "imported": "source" in row, "action": row.get("action", "")[:200],
                       "dialogue": row.get("dialogue", [])} for row in manifest["shots"]],
            "exports": len(data["exports"]), "current_export": any(row["current"] for row in data["exports"])}


def tool_usage(_args, _ctx):
    try:
        return jobs.usage()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        return {"error": str(error)[:400]}


def tool_doctor(_args, _ctx):
    """Read-only environment checks for the most common setup problems."""
    found = {name: bool(shutil.which(name)) for name in ("ffmpeg", "ffprobe", "ssh-keygen")}
    found["uv"] = bool(shutil.which("uv") or os.path.exists(os.path.expanduser("~/.local/bin/uv")))
    found["colab_cli"] = bool(shutil.which("colab") or os.path.exists(os.path.expanduser("~/.local/bin/colab")))
    found["workbench_ssh_key"] = (STATE / "ssh_ed25519").is_file()
    try:
        found["colab"] = "ok · " + json.dumps(jobs.usage(), ensure_ascii=False)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        found["colab"] = "error · " + str(error)[:300]
    found["model_gateway"] = bool(gateway.settings().get("active"))
    free = shutil.disk_usage(STATE)
    found["state_disk_free_gib"] = round(free.free / 2**30, 1)
    found["hints"] = {"install": "bash workbench/bootstrap.sh", "auth": "bash workbench/bootstrap.sh auth",
                      "note": "安装依赖和 Google 授权需要用户在终端或授权页完成。"}
    return found


def tool_release(args, _ctx):
    return job_summary(jobs.enqueue({"action": "release", "target": args.get("job_id")}))


def tool_cancel(args, _ctx):
    with jobs.gate("control.lock"):
        path = jobs.job_path(args.get("job_id"))
        row = jobs.load_json(path)
        if row["status"] not in jobs.ACTIVE:
            raise ValueError("任务已结束。")
        path.with_name("cancel").write_text("cancel\n")
    return {"accepted": True}


def tool_retry(args, _ctx):
    row = jobs.load_json(jobs.job_path(args.get("job_id")))
    if row["status"] in jobs.ACTIVE or row["action"] == "release":
        raise ValueError("只能重试已结束的生成、音色、审片或导出任务。")
    data = {"action": row["action"], "project": row.get("project"), "shots": row.get("shots", [])}
    if row["action"] in {"generate", "voice"} and row.get("gpu_plan"):
        plan = dict(row["gpu_plan"])
        if isinstance(args.get("gpus"), list) and args["gpus"]:
            plan["gpus"] = args["gpus"]
        if type(args.get("rounds")) is int:
            plan["rounds"] = args["rounds"]
        data["gpu"] = plan
    if row["action"] == "voice":
        data.update({key: value for key, value in row["voice"].items()
                     if key in {"model", "actor", "text", "count", "instruct", "speaker", "ref", "ref_text"}})
        if row["voice"].get("engine") == "api":
            data["speaker"] = row["voice"].get("voice")
    return job_summary(jobs.enqueue(data))


# name: (handler, needs approval, description shown to the model, approval title for the user)
TOOLS = {
    "workbench_status": (tool_status, False, "列出项目、最近 8 个任务（含错误）、正在运行和未释放 GPU 的任务", None),
    "read_job": (tool_read_job, False, "读取任务详情和日志末尾，args: job_id", None),
    "read_project": (tool_read_project, False, "读取项目分镜、角色、镜头状态，args: project_id", None),
    "colab_usage": (tool_usage, False, "查询 Colab 余额、消耗和运行中的机器", None),
    "doctor": (tool_doctor, False, "检查 ffmpeg/uv/Colab CLI/SSH 密钥/Colab 授权/模型网关/磁盘空间", None),
    "release_gpu": (tool_release, True, "释放某个任务未确认释放的 GPU，args: job_id", "释放 GPU"),
    "cancel_job": (tool_cancel, True, "取消正在运行的任务，args: job_id", "取消任务"),
    "retry_job": (tool_retry, True, "按原参数重试已失败或取消的任务，可改抢卡顺序，args: job_id, gpus[], rounds；"
                  "生成和音色会消耗算力", "重试任务"),
}
SYSTEM = (
    "你是「Colab 影像工作台」里的助手 Agent。工作台流程：参考图/角色 → 分镜（每镜 3–12 秒，最多 3 个角色）→ 在 Colab 抢 "
    "G4/H100/A100 80G 渲染 MiniMax H3（画面和声音一起出）→ 审片确认 → 导出。角色音色可在 L4/T4 小卡上用 Qwen3-TTS 合成。"
    "\n工作方式：遇到错误或不确定时先调用只读工具查清（看任务日志用 read_job，查环境用 doctor），再用简洁的中文说明原因和解决办法。"
    "能直接修的就修：需要释放、取消或重试任务时调用对应工具，系统会先请用户批准；需要改分镜、打开某个面板时给出操作卡片 actions。"
    "需要用户本人完成的（Google 授权、安装系统依赖、充值）就清楚告诉他怎么做。没拿到工具结果前不要声称已完成，不要编造任务 ID。"
    "\n每一步只输出一个 JSON：要调用工具时填 tool_calls，reply 留空；查清后给出 reply 和 0–3 个 actions，tool_calls 留空。"
    "\n工具：" + "；".join(f"{name}{'（需用户批准）' if spec[1] else ''}：{spec[2]}" for name, spec in TOOLS.items())
    + "\n操作卡片：" + "；".join(f"{key}：{value}" for key, value in director.ACTIONS.items())
)
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["reply", "tool_calls", "actions"],
    "properties": {
        "reply": {"type": "string"},
        "tool_calls": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "args_json"],
            "properties": {"name": {"type": "string", "enum": list(TOOLS)}, "args_json": {"type": "string"}}}},
        "actions": director.ASSIST_SCHEMA["properties"]["actions"],
    },
}


def audit(entry):
    try:
        with AUDIT.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(entry, time=time.time()), ensure_ascii=False) + "\n")
    except OSError:
        pass


def parse_args(text):
    try:
        value = json.loads(text) if isinstance(text, str) and text.strip() else {}
    except json.JSONDecodeError:
        value = {}
    return value if isinstance(value, dict) else {}


def run_tool(name, args, ctx):
    try:
        result = TOOLS[name][0](args, ctx)
    except (ValueError, KeyError, TypeError, FileNotFoundError, RuntimeError, OSError) as error:
        result = {"error": jobs.clean(str(error))[:400] or type(error).__name__}
    return json.dumps(result, ensure_ascii=False, default=str)[:6000]


def clean_transcript(rows):
    if not isinstance(rows, list) or len(rows) > 60:
        raise ValueError("对话太长，请清空后重新开始。")
    out = []
    for row in rows:
        if not isinstance(row, dict) or row.get("role") not in {"user", "assistant", "tool", "decision"}:
            raise ValueError("对话格式不正确。")
        out.append({"role": row["role"], "content": director.clip(row.get("content"), 6000),
                    **({"name": row["name"]} if isinstance(row.get("name"), str) else {})})
    return out


def render(transcript):
    labels = {"user": "用户", "assistant": "助手", "tool": "工具结果", "decision": "用户审批"}
    return "\n".join(f"{labels[row['role']]}{'（' + row['name'] + '）' if row.get('name') else ''}：{row['content']}"
                     for row in transcript[-30:])


def run(data, read_project, projects):
    """One user turn, or the continuation after an approval decision."""
    ctx = {"read_project": read_project, "projects": projects}
    transcript = clean_transcript(data.get("transcript") or [])
    session = data.get("session") if isinstance(data.get("session"), str) else uuid4().hex[:12]
    decision = data.get("decision")
    if isinstance(decision, dict):
        name, args = decision.get("name"), decision.get("args") if isinstance(decision.get("args"), dict) else {}
        if name not in TOOLS or not TOOLS[name][1]:
            raise ValueError("无效的审批请求。")
        allowed = decision.get("allow") is True
        audit({"session": session, "event": "decision", "tool": name, "args": args, "allow": allowed})
        if allowed:
            result = run_tool(name, args, ctx)
            transcript += [{"role": "decision", "content": f"批准 {name} {json.dumps(args, ensure_ascii=False)}"},
                           {"role": "tool", "name": name, "content": result}]
            audit({"session": session, "event": "result", "tool": name, "result": result[:500]})
        else:
            transcript.append({"role": "decision", "content": f"拒绝 {name}，不要再次请求同一操作，改为说明其他办法。"})
    else:
        text = director.clip(data.get("message"), 1200)
        if not text:
            raise ValueError("请输入问题。")
        transcript.append({"role": "user", "content": text})
    state = json.dumps(data.get("state") if isinstance(data.get("state"), dict) else {}, ensure_ascii=False)[:5000]
    trace, model = [], ""
    for _ in range(MAX_STEPS):
        raw, model = gateway.ask_json(SYSTEM, "界面当前状态：" + state + "\n\n对话与工具记录：\n" + render(transcript),
                                      SCHEMA, max_tokens=1500, timeout=150)
        calls = [row for row in raw.get("tool_calls") or [] if isinstance(row, dict) and row.get("name") in TOOLS][:4]
        reads = [row for row in calls if not TOOLS[row["name"]][1]]
        gated = next((row for row in calls if TOOLS[row["name"]][1]), None)
        for row in reads:
            args = parse_args(row.get("args_json"))
            transcript.append({"role": "tool", "name": row["name"], "content": run_tool(row["name"], args, ctx)})
            trace.append(row["name"])
        if gated:
            name, args = gated["name"], parse_args(gated.get("args_json"))
            reply = director.clip(raw.get("reply"), 2000) or f"需要你批准：{TOOLS[name][3]}。"
            transcript.append({"role": "assistant", "content": reply})
            audit({"session": session, "event": "request", "tool": name, "args": args})
            return {"session": session, "transcript": transcript, "reply": reply, "trace": trace, "model": model,
                    "actions": [], "pending": {"name": name, "args": args, "title": TOOLS[name][3]}}
        if not calls:
            break
    reply = director.clip(raw.get("reply"), 2000) or "我查看了相关信息，但没能得出结论，请换个说法再试。"
    actions = []
    for row in raw.get("actions") or []:
        if isinstance(row, dict) and row.get("type") in director.ACTIONS and len(actions) < 3:
            actions.append({"type": row["type"], "label": director.clip(row.get("label"), 40) or row["type"],
                            "args": parse_args(row.get("args_json"))})
    transcript.append({"role": "assistant", "content": reply})
    return {"session": session, "transcript": transcript, "reply": reply, "trace": trace, "model": model,
            "actions": actions, "pending": None}
