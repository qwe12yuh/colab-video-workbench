"""Voice sources for characters: preset TTS models run on a small Colab GPU, or any compatible TTS API.

A synthesized take only becomes a character's reference voice; H3 still speaks the dialogue itself with
that timbre and synchronized lips. Synthesis runs as its own queued job, so it never overlaps a render.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request

from jobs import STATE, gate
from studio import atomic, load_json, probe, save_json

# Preset models. Add an entry here and a branch in voice_tts.py to offer another engine.
MODELS = {
    "qwen3-design": {"label": "Qwen3-TTS 音色设计 1.7B", "repo": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
                     "kind": "design", "engine": "colab", "vram": "约 8 GB",
                     "note": "用一句话描述想要的声音（年龄、性别、音色、语气），从零生成新音色。"},
    "qwen3-custom": {"label": "Qwen3-TTS 预置音色 1.7B", "repo": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                     "kind": "custom", "engine": "colab", "vram": "约 8 GB",
                     "note": "从 9 个内置音色里选，可再加一句语气指令。"},
    "qwen3-custom-lite": {"label": "Qwen3-TTS 预置音色 0.6B（快）", "repo": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
                          "kind": "custom", "engine": "colab", "vram": "约 4 GB",
                          "note": "同上，更快更省，音质略低。"},
    "qwen3-clone": {"label": "Qwen3-TTS 克隆录音 1.7B", "repo": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                    "kind": "clone", "engine": "colab", "vram": "约 8 GB",
                    "note": "用一段 3 秒以上的录音，生成一段干净、念指定台词的参考音色。"},
    "qwen3-clone-lite": {"label": "Qwen3-TTS 克隆录音 0.6B（快）", "repo": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
                         "kind": "clone", "engine": "colab", "vram": "约 4 GB",
                         "note": "同上，更快更省。"},
    "api": {"label": "兼容 TTS 接口", "kind": "api", "engine": "api", "vram": "不占 Colab",
            "note": "调用任意 OpenAI 兼容的 /v1/audio/speech 服务（自建或云端），按该服务计费。"},
}
SPEAKERS = [
    ["Serena", "温柔的年轻女声"], ["Vivian", "明亮、略带个性的年轻女声"], ["Uncle_Fu", "低沉醇厚的成熟男声"],
    ["Dylan", "清亮自然的北京青年男声"], ["Eric", "略带沙哑的成都男声"], ["Ryan", "节奏感强的男声（英语母语）"],
    ["Aiden", "阳光的美式男声（英语母语）"], ["Ono_Anna", "轻快俏皮的女声（日语母语）"],
    ["Sohee", "情感丰富的温暖女声（韩语母语）"],
]
SAMPLE = "你好，很高兴见到你。今天外面下了一整天的雨，我一直在想，我们是不是该好好聊一聊了。"
CONFIG = STATE / "tts.json"
MAX_SECONDS = 12


def clip(value, limit):
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def settings():
    value = load_json(CONFIG) if CONFIG.is_file() else {}
    return {"base_url": value.get("base_url", ""), "model": value.get("model", ""),
            "voice": value.get("voice", ""), "has_key": bool(value.get("key"))}


def catalog():
    return {"models": [dict(spec, id=key) for key, spec in MODELS.items()], "speakers": SPEAKERS,
            "sample": SAMPLE, "api": settings()}


def save(data):
    import gateway
    with gate("tts.lock"):
        old = load_json(CONFIG) if CONFIG.is_file() else {}
        url = gateway.compatible_url(data.get("base_url"))
        model = clip(data.get("model"), 120)
        voice = clip(data.get("voice"), 80)
        if not model or not re.fullmatch(r"[\w./:@+-]+", model):
            raise ValueError("请填写 TTS 模型 ID。")
        key = data.get("key") or ""
        if not isinstance(key, str) or len(key) > 4000 or any(ch in key for ch in "\r\n"):
            raise ValueError("API 密钥格式不正确。")
        remembered = old.get("key", "") if old.get("base_url") == url else ""
        save_json(CONFIG, {"base_url": url, "model": model, "voice": voice,
                           "key": "" if data.get("clear_key") else key.strip() or remembered})
    return settings()


def validate(data, project):
    """Normalize a synthesis request against the project's characters and assets."""
    model = data.get("model")
    if model not in MODELS:
        raise ValueError("请选择一个音色模型。")
    spec = MODELS[model]
    actor = data.get("actor")
    if actor not in project.actors:
        raise ValueError("请选择要配音色的角色。")
    text = clip(data.get("text"), 120)
    if len(text) < 6:
        raise ValueError("试听台词至少 6 个字，建议 30–60 字（约 8–12 秒）。")
    count = data.get("count", 2)
    if type(count) is not int or not 1 <= count <= 3:
        raise ValueError("候选数须在 1–3 之间。")
    request = {"model": model, "kind": spec["kind"], "engine": spec["engine"], "repo": spec.get("repo"),
               "actor": actor, "text": text, "count": count, "instruct": clip(data.get("instruct"), 300),
               "seed": int.from_bytes(hashlib.sha256(text.encode() + actor.encode()).digest()[:3], "big")}
    if spec["kind"] == "design" and len(request["instruct"]) < 4:
        raise ValueError("请描述想要的声音，例如：二十多岁的女性，声音清澈温柔，语速偏慢。")
    if spec["kind"] == "custom":
        speaker = data.get("speaker")
        if speaker not in {name for name, _ in SPEAKERS}:
            raise ValueError("请选择一个内置音色。")
        request["speaker"] = speaker
    if spec["kind"] == "clone":
        ref = data.get("ref")
        if not isinstance(ref, str) or not re.fullmatch(r"assets/[\w.-]{1,80}\.(wav|mp3)", ref) \
                or not (project.root / ref).is_file():
            raise ValueError("请先上传一段 3 秒以上的 WAV/MP3 录音作为克隆来源。")
        request.update(ref=ref, ref_text=clip(data.get("ref_text"), 200))
    if spec["kind"] == "api":
        config = load_json(CONFIG) if CONFIG.is_file() else {}
        if not config.get("base_url"):
            raise ValueError("请先在音色面板保存兼容 TTS 接口的地址和模型。")
        request["voice"] = clip(data.get("speaker"), 80) or config.get("voice") or "alloy"
    return request


def api_synthesize(request, out, log):
    import gateway
    config = load_json(CONFIG)
    out.mkdir(parents=True, exist_ok=True)
    for index in range(request["count"]):
        body = {"model": config["model"], "input": request["text"], "voice": request["voice"],
                "response_format": "wav"}
        if request["instruct"]:
            body["instructions"] = request["instruct"]
        headers = {"Content-Type": "application/json", "User-Agent": "colab-video-workbench/1.1"}
        if config.get("key"):
            headers["Authorization"] = "Bearer " + config["key"]
        log.write(f"[voice] 调用 TTS 接口 · 候选 {index + 1}/{request['count']}\n".encode())
        try:
            with gateway.OPENER.open(Request(config["base_url"] + "/audio/speech",
                                             data=json.dumps(body, ensure_ascii=False).encode(),
                                             headers=headers), timeout=120) as response:
                payload = response.read(30_000_000)
        except HTTPError as error:
            raise RuntimeError(f"TTS 接口返回 HTTP {error.code}，请检查地址、密钥、模型和音色名。") from None
        except (URLError, OSError, TimeoutError):
            raise RuntimeError("无法连接 TTS 接口，请检查服务是否在线。") from None
        atomic(out / f"take{index + 1}.wav", payload)


def store(project_root, staging):
    """Normalize each take to mono WAV of at most MAX_SECONDS and keep it as a project asset."""
    found = []
    for take in sorted(staging.glob("take[0-9].wav")):
        with tempfile.TemporaryDirectory(dir=project_root) as scratch:
            target = Path(scratch) / "voice.wav"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(take),
                            "-t", str(MAX_SECONDS), "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(target)],
                           check=True, capture_output=True, timeout=60)
            streams = probe(target)
            seconds = max((float(row.get("duration", 0)) for row in streams if row.get("codec_type") == "audio"),
                          default=0)
            if seconds < 2:
                continue
            payload = target.read_bytes()
            name = hashlib.sha256(payload).hexdigest()[:24] + ".wav"
            path = project_root / "assets" / name
            if not path.exists():
                atomic(path, payload)
            found.append({"path": f"assets/{name}", "seconds": round(seconds, 2)})
    if not found:
        raise RuntimeError("没有得到可用的音色（每段至少 2 秒）。请把试听台词写长一些再试。")
    return found
