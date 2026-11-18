"""Check AI storyboard and rewrite against a fake OpenAI-compatible API, including output sanitizing."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading

state = tempfile.TemporaryDirectory(prefix="director-test-")
os.environ["COLAB_WORKBENCH_STATE"] = state.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workbench"))
import director
import gateway

PLAN = {"title": "雨夜重逢", "style": "冷色调电影感写实", "locations": [
    {"key": "Lobby Night!", "description": "雨夜酒店大堂，旋转门外霓虹倒影"}],
    "shots": [
        {"title": "推门", "seconds": 15, "shot_size": "全景", "camera": "缓慢推近", "location": "lobby-night",
         "cast": ["lin", "ghost", "lin"], "action": "林晚推开旋转门走进大堂，抖落伞上的雨水。",
         "sound": "雨声、门轴转动", "dialogue": [
             {"speaker": "lin", "text": "我回来了。", "start": 1, "end": 2.5},
             {"speaker": "ghost", "text": "不存在的人", "start": 3, "end": 4},
             {"speaker": "lin", "text": "太晚了", "start": 2, "end": 11.9}]},
        {"title": "", "seconds": 2.4, "shot_size": "特写", "camera": "", "location": "unknown",
         "cast": [], "action": "她的眼神落在前台。", "sound": "", "dialogue": []},
        {"title": "空", "seconds": 6, "action": ""}]}
replies = []


class API(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system = body["messages"][0]["content"]
        if "里的助手" in system:
            assert "工作台当前状态" in body["messages"][-1]["content"]
            content = json.dumps({"reply": "可以先用 AI 分镜拆成 4 个镜头。", "actions": [
                {"type": "director", "label": "打开 AI 分镜", "args_json": json.dumps({"idea": "雨夜重逢", "shots": 4})},
                {"type": "rm_rf", "label": "坏操作", "args_json": "{}"},
                {"type": "generate", "label": "生成 S01（消耗算力）", "args_json": "not json"}]}, ensure_ascii=False)
        elif "分镜师" in system:
            content = "好的：\n```json\n" + json.dumps(PLAN, ensure_ascii=False) + "\n```"
        else:
            content = json.dumps({"action": "近景，镜头缓慢推近，她握紧伞柄，目光坚定。"}, ensure_ascii=False)
        replies.append(system)
        payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


with ThreadingHTTPServer(("127.0.0.1", 0), API) as server:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    gateway.save({"kind": "api", "name": "fake", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                  "key": "", "model": "fake/model"})
    chars = [{"id": "lin", "label": "林晚", "description": "黑色长发、米色风衣"}]
    plan = director.storyboard({"idea": "雨夜，林晚回到旧酒店", "shots": 4, "characters": chars})
    first, second = plan["shots"]
    assert len(plan["shots"]) == 2 and plan["model"] == "fake/model"
    assert first["seconds"] == 12 and first["cast"] == ["lin"] and first["location"] == "lobby-night"
    assert first["action"].startswith("全景，缓慢推近。林晚")
    # Unknown speaker dropped, overlap pushed after the previous line and capped before the shot end.
    assert first["dialogue"] == [{"speaker": "lin", "text": "我回来了。", "start": 1.0, "end": 2.5},
                                 {"speaker": "lin", "text": "太晚了", "start": 2.5, "end": 11.2}], first["dialogue"]
    assert second["seconds"] == 3 and second["cast"] == ["lin"] and second["location"] == "lobby-night"
    assert second["title"] == "镜头 2" and set(plan["locations"]) == {"lobby-night"}
    result = director.rewrite({"action": "她走进来", "preset": "cinematic", "cast": ["林晚：黑色长发"]})
    assert "推近" in result["action"] and result["model"] == "fake/model" and "分镜编辑" in replies[-1]
    answer = director.assistant({"messages": [{"role": "user", "content": "我想做个雨夜重逢的短片"}],
                                 "state": {"project": {"title": "测试"}}})
    assert answer["reply"].startswith("可以") and [row["type"] for row in answer["actions"]] == ["director", "generate"]
    assert answer["actions"][0]["args"] == {"idea": "雨夜重逢", "shots": 4} and answer["actions"][1]["args"] == {}
    for bad in ({"idea": ""}, {"idea": "x", "shots": 9, "characters": chars}, {"idea": "x", "characters": []}):
        try:
            director.storyboard(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    server.shutdown()
print("Director PASS: storyboard sanitizing (cast, seconds, locations, dialogue cues), fenced JSON, rewrite, assistant actions.")
