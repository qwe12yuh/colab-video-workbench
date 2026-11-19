"""Local provider registry, direct compatible APIs, and the official Codex CLI adapter."""
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from jobs import STATE, gate
from studio import load_json, save_json

CONFIG = STATE / "gateway.json"
SYSTEM = ("将用户的中文视频要求润色为适合参考图生成单镜头视频的中文提示词。"
          "保留原意、人物、对白和否定要求，具体化动作和运镜。只输出润色后的文字，不添加解说。")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


OPENER = build_opener(ProxyHandler({}), NoRedirect())


def registry():
    value = load_json(CONFIG) if CONFIG.is_file() else {}
    return value if isinstance(value.get("profiles"), list) else {"active": "", "profiles": []}


def public(value):
    return {"active": value["active"], "profiles": [
        {**{k: v for k, v in row.items() if k != "key"}, "has_key": bool(row.get("key"))}
        for row in value["profiles"]
    ]}


def settings():
    return public(registry())


def compatible_url(value):
    if not isinstance(value, str) or not value or len(value) > 400 or any(ch.isspace() for ch in value):
        raise ValueError("请输入兼容 API 地址，例如 https://api.example.com/v1。")
    url = urlsplit(value.rstrip("/"))
    host = url.hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if (not host or url.username or url.password or url.query or url.fragment
            or (url.scheme != "https" and not (url.scheme == "http" and loopback))):
        raise ValueError("云端连接须使用 HTTPS。本机接口可用 http://127.0.0.1。")
    if not url.path.endswith("/v1"):
        raise ValueError("兼容 API 地址应以 /v1 结尾。")
    return url.geturl()


def prepare(data, config):
    existing_id = data.get("id", "")
    old = next((row for row in config["profiles"] if row["id"] == existing_id), None)
    if existing_id and not old:
        raise ValueError("请重新选择要编辑的模型连接。")
    kind = data.get("kind")
    if kind not in {"api", "codex"}:
        raise ValueError("请选择一种连接方式。")
    name = str(data.get("name", "")).strip() or ("本机 Codex" if kind == "codex" else "兼容 API")
    model = str(data.get("model", "")).strip()
    if len(name) > 50 or len(model) > 180 or (model and not re.fullmatch(r"[\w./:@+-]+", model)):
        raise ValueError("连接名或模型 ID 格式不正确。")
    row = {"id": old["id"] if old else uuid4().hex[:12], "kind": kind, "name": name, "model": model}
    if kind == "api":
        row["base_url"] = compatible_url(data.get("base_url"))
        entered = data.get("key") or ""
        if not isinstance(entered, str) or len(entered) > 4000 or any(ch in entered for ch in "\r\n"):
            raise ValueError("API 密钥格式不正确。")
        remembered = old.get("key", "") if old and old.get("base_url") == row["base_url"] else ""
        row["key"] = "" if data.get("clear_key") else entered.strip() or remembered
    return row


def api_request(row, endpoint, data=None, timeout=8):
    encoded = None if data is None else json.dumps(data, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "colab-video-workbench/1.1"}
    if row["key"]:
        headers["Authorization"] = "Bearer " + row["key"]
    req = Request(row["base_url"] + endpoint, data=encoded, headers=headers)
    try:
        with OPENER.open(req, timeout=timeout) as response:
            return json.loads(response.read(2_000_000))
    except HTTPError as error:
        raise RuntimeError(f"模型接口返回 HTTP {error.code}。请检查地址、密钥和模型。") from None
    except (URLError, OSError, TimeoutError, json.JSONDecodeError):
        raise RuntimeError("无法读取模型接口，请检查服务是否在线。") from None


def codex_executable():
    path = Path.home() / ".local/bin/codex"
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    if path := shutil.which("codex"):
        return path
    raise ValueError("在 WSL 安装官方 Codex CLI 后再连接。")


def codex_env():
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        env.pop(key, None)
    return env


def codex_status():
    result = subprocess.run([codex_executable(), "login", "status"], env=codex_env(),
                            text=True, capture_output=True, timeout=15)
    if result.returncode or "Logged in" not in result.stdout + result.stderr:
        raise ValueError("本机 Codex 尚未登录。可使用官方登录按钮。")
    return ("已通过 ChatGPT 登录" if "using ChatGPT" in result.stdout + result.stderr else "本机 Codex 已登录")


def models(data):
    row = prepare(data, registry())
    if row["kind"] == "codex":
        return {"status": codex_status(), "models": []}
    catalog = api_request(row, "/models")
    rows = catalog.get("data") if isinstance(catalog, dict) else None
    if not isinstance(rows, list):
        raise ValueError("接口未返回模型列表，可以手动填写模型 ID。")
    items = [{"id": item["id"], "label": item.get("display_name") or item["id"]} for item in rows
             if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]]
    return {"status": f"连接正常，找到 {len(items)} 个模型。", "models": items}


def save(data):
    with gate("gateway.lock"):
        config = registry()
        row = prepare(data, config)
        if row["kind"] == "codex":
            codex_status()
        elif not row["model"]:
            raise ValueError("请输入要使用的模型 ID。")
        profiles = [item for item in config["profiles"] if item["id"] != row["id"]]
        config = {"active": row["id"], "profiles": profiles + [row]}
        save_json(CONFIG, config)
        return public(config)


def active_profile():
    config = registry()
    row = next((row for row in config["profiles"] if row["id"] == config["active"]), None)
    if not row:
        raise ValueError("请先在「模型网关」保存一个模型连接。")
    return row


def parse_json(text):
    """Accept a bare JSON object, or one wrapped in prose or a code fence."""
    if not isinstance(text, str):
        return None
    start, end = text.find("{"), text.rfind("}")
    try:
        value = json.loads(text[start:end + 1]) if 0 <= start < end else None
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def ask_json(system, text, schema, max_tokens=1500, timeout=120):
    """Ask the active model for one JSON object that follows schema. Returns (object, model label)."""
    row = active_profile()
    if row["kind"] == "codex":
        value = codex_json(system + "\n\n" + text, schema, row, timeout)
    else:
        reply = api_request(row, "/chat/completions", {"model": row["model"], "stream": False,
            "max_tokens": max_tokens, "messages": [
                {"role": "system", "content": system + "\n只输出一个 JSON 对象，结构为：" + json.dumps(schema, ensure_ascii=False)},
                {"role": "user", "content": text}]}, timeout=timeout)
        try:
            value = parse_json(reply["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            value = None
    if not isinstance(value, dict):
        raise ValueError("模型没有返回可用的结构化结果，请重试或换一个模型。")
    return value, row["model"] or row["name"]


def codex_json(prompt, schema_value, row, timeout=100):
    codex_status()
    with tempfile.TemporaryDirectory(prefix="workbench-agent-") as scratch:
        root = Path(scratch)
        output = root / "reply.json"
        schema = root / "schema.json"
        schema.write_text(json.dumps(schema_value), encoding="utf-8")
        command = [codex_executable(), "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                   "--skip-git-repo-check", "-s", "read-only",
                   "--disable", "shell_tool", "--disable", "unified_exec", "--disable", "plugins",
                   "--disable", "multi_agent", "--disable", "apps", "-c", 'web_search="disabled"',
                   "-c", 'approval_policy="never"', "--output-schema", str(schema), "-o", str(output)]
        if row["model"]:
            command += ["-m", row["model"]]
        result = subprocess.run(command + ["-"], cwd=root, env=codex_env(), input=prompt,
                                text=True, capture_output=True, timeout=timeout)
        if result.returncode or not output.is_file():
            raise RuntimeError("官方 Codex CLI 没有返回可用结果。请检查登录和模型。")
        return parse_json(output.read_text(encoding="utf-8"))


def polish_with_codex(text, row):
    value = codex_json(SYSTEM + "\n\n" + text, {"type":"object","additionalProperties":False,
        "properties":{"prompt":{"type":"string"}},"required":["prompt"]}, row)
    return (value or {}).get("prompt")


def polish(data):
    text = data.get("prompt")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1800:
        raise ValueError("先输入视频要求，再进行润色。")
    row = active_profile()
    if row["kind"] == "codex":
        output = polish_with_codex(text.strip(), row)
    else:
        reply = api_request(row, "/chat/completions", {"model": row["model"], "stream": False, "max_tokens": 650,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text.strip()}]},
            timeout=60)
        try:
            output = reply["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            output = None
    if not isinstance(output, str) or not 1 <= len(output.strip()) <= 1800:
        raise ValueError("模型没有返回可用的中文提示词。原文已保留。")
    return {"prompt": output.strip(), "model": row["model"] or row["name"]}
