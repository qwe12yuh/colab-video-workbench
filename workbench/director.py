"""AI director: turn an idea into a shot plan, and rewrite one shot, using the active model gateway.

The model only proposes text. Results are sanitized to the manifest rules in studio.Project and are
returned to the editor as unsaved changes, so the normal save validation and review gates still apply.
"""
import json
import re

import gateway

SHOT_SIZES = ["大远景", "远景", "全景", "中景", "近景", "特写", "大特写"]
STORY_SYSTEM = (
    "你是短片导演兼分镜师，为 MiniMax H3 参考图生视频模型写分镜。每个镜头是一次独立生成，3–12 秒，"
    "画面中最多 3 个角色，只能使用给定的角色 ID。为每个镜头给出：标题、秒数、景别、运镜、场景 key、出场角色、"
    "具体的动作与表演（写成可直接拍摄的中文描述，不写镜头编号）、环境声，以及可选的中文对白"
    "（每句给出开始和结束秒数，按时间顺序，最后一句结束后留约 1 秒）。"
    "场景用简短英文 key 和中文描述，跨镜头复用同一场景以保持连续性。全片风格写一句中文。"
)
STORY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "style", "locations", "shots"],
    "properties": {
        "title": {"type": "string"},
        "style": {"type": "string"},
        "locations": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["key", "description"],
            "properties": {"key": {"type": "string"}, "description": {"type": "string"}}}},
        "shots": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["title", "seconds", "shot_size", "camera", "location", "cast", "action", "sound", "dialogue"],
            "properties": {
                "title": {"type": "string"}, "seconds": {"type": "integer"},
                "shot_size": {"type": "string"}, "camera": {"type": "string"},
                "location": {"type": "string"}, "cast": {"type": "array", "items": {"type": "string"}},
                "action": {"type": "string"}, "sound": {"type": "string"},
                "dialogue": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["speaker", "text", "start", "end"],
                    "properties": {"speaker": {"type": "string"}, "text": {"type": "string"},
                                   "start": {"type": "number"}, "end": {"type": "number"}}}},
            }}},
    },
}
REWRITE_SYSTEM = (
    "你是视频分镜编辑。按用户的修改要求改写一个镜头的画面描述，用于 MiniMax H3 参考图生视频。"
    "保留人物身份、场景和原有意图，写成可直接拍摄的中文：景别、动作表演、运镜、光线与声音。"
    "不要写时间码、镜头编号或对白时间表，不超过 400 字。"
)
REWRITE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["action"],
                  "properties": {"action": {"type": "string"}}}
PRESETS = {
    "cinematic": "更有电影感：明确景别和光线，加入有动机的运镜。",
    "emotion": "强化人物情绪和表演细节，让观众看得出内心变化。",
    "camera": "补全运镜：写清起幅、运动方式和落幅。",
    "concise": "精简到可拍摄的要点，去掉抽象形容。",
    "continuity": "和前后镜头保持连贯：动作、视线和道具位置衔接自然。",
}


def text(value, limit, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"请填写{name}。")
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > limit:
        raise ValueError(f"{name}不能超过 {limit} 字。")
    return value


def clip(value, limit):
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def characters_from(data):
    rows = data.get("characters")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 12:
        raise ValueError("项目里至少要有一个角色，AI 分镜才能安排出场人物。")
    found = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not re.fullmatch(r"[\w-]{1,40}", row["id"]):
            raise ValueError("角色列表格式不正确。")
        found[row["id"]] = {"label": clip(row.get("label") or row["id"], 40),
                            "description": clip(row.get("description"), 240)}
    return found


def slug(value, index):
    key = re.sub(r"[^a-z0-9_-]+", "-", str(value).lower()).strip("-_")[:32]
    return key if key and key[0].isalnum() else f"scene-{index}"


def compose_action(shot):
    lead = "，".join(part for part in (shot["shot_size"], shot["camera"]) if part)
    return (lead + "。" if lead else "") + shot["body"]


def sanitize_plan(raw, cast_ids, max_shots):
    locations = {}
    for index, row in enumerate(raw.get("locations") or [], 1):
        if isinstance(row, dict) and clip(row.get("description"), 400) and len(locations) < 8:
            key = slug(row.get("key"), index)
            while key in locations:
                key += "-2"
            locations[key] = clip(row["description"], 400)
    shots = []
    for row in (raw.get("shots") or [])[:max_shots]:
        if not isinstance(row, dict) or not clip(row.get("action"), 1200):
            continue
        seconds = row.get("seconds")
        seconds = min(12, max(3, round(seconds))) if type(seconds) in (int, float) else 6
        cast = [name for name in dict.fromkeys(row.get("cast") or []) if name in cast_ids][:3] or [cast_ids[0]]
        location = slug(row.get("location"), len(locations) + 1)
        if location not in locations:
            location = next(iter(locations), None)
            if location is None:
                locations["scene"] = "根据镜头描述布置场景，与参考图风格一致。"
                location = "scene"
        dialogue, last = [], 0.0
        for line in row.get("dialogue") or []:
            if not isinstance(line, dict) or line.get("speaker") not in cast or not clip(line.get("text"), 120):
                continue
            start, end = line.get("start"), line.get("end")
            if type(start) not in (int, float) or type(end) not in (int, float):
                continue
            start, end = round(max(float(start), last), 2), round(min(float(end), seconds - 0.8), 2)
            if end - start < 0.6:
                continue
            dialogue.append({"speaker": line["speaker"], "text": clip(line["text"], 120), "start": start, "end": end})
            last = end
        size = clip(row.get("shot_size"), 12)
        shot = {"title": clip(row.get("title"), 40) or f"镜头 {len(shots) + 1}", "seconds": seconds,
                "shot_size": size, "camera": clip(row.get("camera"), 60), "location": location, "cast": cast,
                "body": clip(row["action"], 1100), "sound": clip(row.get("sound"), 200), "dialogue": dialogue}
        shot["action"] = compose_action(shot)
        del shot["body"]
        shots.append(shot)
    if not shots:
        raise ValueError("模型没有给出可用的镜头，请把创意写得更具体后重试。")
    return {"title": clip(raw.get("title"), 60), "style": clip(raw.get("style"), 300),
            "locations": {key: value for key, value in locations.items() if any(s["location"] == key for s in shots)},
            "shots": shots}


def storyboard(data):
    idea = text(data.get("idea"), 1500, "故事创意")
    count = data.get("shots", 4)
    if type(count) is not int or not 1 <= count <= 8:
        raise ValueError("镜头数须在 1–8 之间。")
    characters = characters_from(data)
    aspect = "16:9 横屏" if data.get("aspect") == "landscape" else "9:16 竖屏"
    tone = clip(data.get("tone"), 80)
    existing = data.get("locations") if isinstance(data.get("locations"), dict) else {}
    brief = "\n".join([
        f"创意：{idea}",
        f"镜头数：{count}；画幅：{aspect}" + (f"；基调：{tone}" if tone else ""),
        "可用角色（只用这些 ID）：" + "；".join(f"{key}（{row['label']}：{row['description']}）" for key, row in characters.items()),
        "已有场景（可复用）：" + ("；".join(f"{clip(k, 32)}：{clip(v, 160)}" for k, v in list(existing.items())[:8]) or "无"),
        "景别可选：" + "、".join(SHOT_SIZES),
    ])
    raw, model = gateway.ask_json(STORY_SYSTEM, brief, STORY_SCHEMA, max_tokens=3500, timeout=180)
    return dict(sanitize_plan(raw, list(characters), count), model=model)


def rewrite(data):
    action = text(data.get("action"), 1800, "镜头描述")
    preset = data.get("preset")
    instruction = PRESETS.get(preset) if preset else text(data.get("instruction"), 200, "修改要求")
    if not instruction:
        raise ValueError("未知的改写方式。")
    cast = data.get("cast") if isinstance(data.get("cast"), list) else []
    brief = "\n".join([
        f"修改要求：{instruction}",
        f"时长：{data.get('seconds', 6)} 秒；场景：{clip(data.get('location'), 300) or '未指定'}",
        "全片风格：" + (clip(data.get("style"), 300) or "未指定"),
        "出场角色：" + ("；".join(clip(item, 120) for item in cast[:3]) or "未指定"),
        "相邻镜头：" + (clip(data.get("neighbors"), 600) or "无"),
        f"原描述：{action}",
    ])
    raw, model = gateway.ask_json(REWRITE_SYSTEM, brief, REWRITE_SCHEMA, max_tokens=900, timeout=120)
    result = clip(raw.get("action"), 1800)
    if not result:
        raise ValueError("模型没有返回改写结果，原文已保留。")
    return {"action": result, "model": model}


# In-app assistant: understands the request, explains, and proposes actions the user confirms by clicking.
ACTIONS = {
    "open_quick": "打开快速生成（参考图 + 中文要求），args: prompt, seconds(3-12), aspect(portrait|landscape)",
    "director": "打开 AI 分镜并预填，args: idea, shots(1-8), tone",
    "select_shot": "切到某个镜头，args: shot",
    "edit_shot": "修改镜头（未保存的修改），args: shot, title, seconds(3-12), action",
    "rewrite_shot": "AI 改写镜头描述，args: shot, preset(cinematic|emotion|camera|concise|continuity) 或 instruction",
    "save": "保存当前修改",
    "generate": "抢卡并生成镜头（消耗 Colab 算力），args: shots[]",
    "approve": "确认已审片的镜头，args: shots[]",
    "export": "导出成片 MP4 + SRT",
    "open_voice": "打开音色合成并预填，args: actor, model(qwen3-design|qwen3-custom|qwen3-clone|api), instruct",
    "add_actor": "打开导入角色",
    "set_gpu": "修改抢卡顺序，args: gpus[](G4|H100|A100), rounds(1-30)",
    "release": "释放未确认释放的 GPU",
    "open_gateway": "打开模型网关设置",
    "open_setup": "打开 Colab 连接设置",
}
ASSIST_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["reply", "actions"],
    "properties": {
        "reply": {"type": "string"},
        "actions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["type", "label", "args_json"],
            "properties": {"type": {"type": "string", "enum": list(ACTIONS)}, "label": {"type": "string"},
                           "args_json": {"type": "string"}}}},
    },
}

