#!/usr/bin/env python3
"""Project, review and editing commands; Python and FFmpeg only."""
import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote

FPS = 24
BASE_REVISION = "42ed227ee7df40d41602854ae760620d6eb651fe"
ADAPTER_REVISION = "ca45d99f9b1d1859ffb9dc1cd6bfaaa994257f56"
PROFILE = ("h3-ref2va-six-step-staged-v1", BASE_REVISION, ADAPTER_REVISION)
CANVAS = {"portrait": (768, 1344, 720, 1280), "landscape": (1344, 768, 1280, 720)}
SLUG = r"[a-z0-9][a-z0-9_-]{0,39}"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_json(path, data):
    atomic(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def required_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text.")
    return value.strip()


def allowed_keys(value, keys, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object.")
    extra = set(value) - set(keys.split())
    if extra:
        raise ValueError(f"{name}: unknown fields {', '.join(sorted(extra))}.")


def asset(root, value):
    name = required_text(value, "asset path")
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts or ":" in name or "\\" in name:
        raise ValueError(f"Use a relative asset path: {name}")
    path = (root / rel).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"Asset is missing or outside the project: {name}")
    return path


@dataclass
class Job:
    row: dict
    key: str
    folder: Path
    media: Path
    prompt: str
    refs: list
    source: bool

    @property
    def id(self):
        return self.row["id"]

    @property
    def frames(self):
        return self.row["seconds"] * FPS


def build_prompt(style, shot, cast, actors, location):
    tags = {name: f"<Subject {i}>" for i, name in enumerate(cast, 1)}
    image_refs = [("image", actors[name]["image"]) for name in cast]
    definitions = [f"{tags[name]} is {actors[name]['description']} in <Picture {i}>."
                   for i, name in enumerate(cast, 1)]
    if shot.get("input_mode") == "reference_prompt":
        prompt = "\n".join([
            "subject_definitions:", *definitions,
            "retention_analysis:",
            "Use the reference image for subject identity and appearance. Follow any requested changes.",
            "detailed_description:", style, "[Shot 1] 中文视频要求：" + shot["action"],
            "Apply the requested motion, camera, setting, and sound. Keep the subject coherent throughout.",
            "If dialogue is requested, speak the specified words naturally in Mandarin with synchronized lips.",
        ])
        return prompt, image_refs
    speakers = dict.fromkeys(line["speaker"] for line in shot.get("dialogue", []))
    speaker_ids = {name: f"(S{i})" for i, name in enumerate(speakers, 1)}
    audio_refs = []
    for name in speakers:
        if voice := actors[name].get("voice"):
            audio_refs.append(("audio", voice))
            definitions.append(f"<Audio {len(audio_refs)}> is the voice reference for {tags[name]} {speaker_ids[name]}.")
    lines = []
    for line in shot.get("dialogue", []):
        name = line["speaker"]
        text = line["text"]
        lines.append(f"Between {line['start']:.2f}s and {line['end']:.2f}s, {tags[name]} "
                     f"{speaker_ids[name]} says naturally in Mandarin: <d>[Chinese] {text}</d>")
    prompt = "\n".join([
        "subject_definitions:", *definitions, "summary:", location, "retention_analysis:",
        "Preserve each reference actor's face, hairstyle and wardrobe. Replace the studio backgrounds.",
        "Keep voice timbre and accent; use the new lines only.",
        "detailed_description:", style, "[Shot 1] " + location + " " + shot["action"], *lines,
        "Keep the room and prop positions continuous across camera changes. No text or subtitles on screen.",
        "overall_soundscape:", shot.get("sound", "Natural room tone and synchronized action sounds."),
        "No extra speech.", "non_diegetic_music:", "None."
    ])
    return prompt, image_refs + audio_refs


class Project:
    def __init__(self, manifest):
        self.root = manifest.resolve().parent
        self.data = data = load_json(manifest)
        allowed_keys(data, "title aspect style characters locations shots target_seconds music", "project")
        self.title = required_text(data.get("title"), "title")
        style = required_text(data.get("style"), "style")
        aspect = data.get("aspect", "portrait")
        if aspect not in CANVAS:
            raise ValueError("aspect must be portrait or landscape.")
        self.canvas = CANVAS[aspect]
        self.actors = {}
        for name, actor in data.get("characters", {}).items():
            allowed_keys(actor, "description image voice label", name)
            if "label" in actor:
                required_text(actor["label"], name)
            self.actors[name] = {
                "description": required_text(actor.get("description"), name),
                "image": asset(self.root, actor.get("image")),
                "voice": asset(self.root, actor["voice"]) if "voice" in actor else None,
            }
        locations = data.get("locations", {})
        self.music = None
        if "music" in data:
            music = data["music"]
            allowed_keys(music, "file gain", "music")
            gain = music.get("gain", 0.12)
            if type(gain) not in (int, float) or not 0 <= gain <= 1:
                raise ValueError("music.gain must be between 0 and 1.")
            self.music = (asset(self.root, music.get("file")), gain)
        self.jobs = {}
        hashes = {}
        for row in data["shots"]:
            allowed_keys(row, "id title seconds location cast action dialogue seed source native_captions sound subtitles trim_start input_mode", "shot")
            id = required_text(row.get("id"), "shot.id")
            if not re.fullmatch(SLUG, id) or id in self.jobs:
                raise ValueError(f"Invalid or duplicate shot id: {id}")
            required_text(row.get("title"), f"{id}.title")
            required_text(row.get("action"), f"{id}.action")
            row["id"] = id
            location = required_text(locations[row["location"]], f"{id}.location")
            seconds = row["seconds"]
            if type(seconds) is not int or not 3 <= seconds <= 12:
                raise ValueError(f"{id}: seconds must be an integer from 3 to 12.")
            cast = row.get("cast", [])
            if (not isinstance(cast, list) or len(cast) > 3 or len(set(cast)) != len(cast)
                    or any(name not in self.actors for name in cast)):
                raise ValueError(f"{id}: give up to 3 distinct known characters.")
            trim = row.get("trim_start", 0)
            if type(trim) not in (int, float) or not 0 <= trim <= 60:
                raise ValueError(f"{id}: trim_start must be between 0 and 60 seconds.")
            if "native_captions" in row and type(row["native_captions"]) is not bool:
                raise ValueError(f"{id}: native_captions must be true or false.")
            if "sound" in row:
                required_text(row["sound"], f"{id}.sound")
            end = 0
            for line in row.get("dialogue", []):
                allowed_keys(line, "speaker text start end", f"{id} dialogue")
                line["text"] = required_text(line.get("text"), f"{id} dialogue")
                start, stop = line["start"], line["end"]
                if (line["speaker"] not in cast or type(start) not in (int, float)
                        or type(stop) not in (int, float) or not end <= start < stop <= seconds):
                    raise ValueError(f"{id}: dialogue needs known speakers and ordered cues inside the shot.")
                end = stop
            if "subtitles" in row:
                last = 0
                for cue in row["subtitles"]:
                    allowed_keys(cue, "text start end", f"{id} subtitle")
                    cue["text"] = required_text(cue.get("text"), f"{id} subtitle")
                    start, stop = cue["start"], cue["end"]
                    if (type(start) not in (int, float) or type(stop) not in (int, float)
                            or not last <= start < stop <= seconds):
                        raise ValueError(f"{id}: subtitle cues must be ordered and inside the shot.")
                    last = stop
            mode = row.get("input_mode", "storyboard")
            if mode not in {"storyboard", "reference_prompt"}:
                raise ValueError(f"{id}: unknown input mode.")
            if mode == "reference_prompt" and row.get("dialogue"):
                raise ValueError(f"{id}: put speech in the request, or switch to storyboard mode for timed dialogue.")
            imported = "source" in row
            if imported:
                media = asset(self.root, row["source"])
                key = digest(["source", file_sha(media), seconds, aspect])
                prompt, refs = "", []
            else:
                seed = row["seed"]
                if not cast or type(seed) is not int or not 0 <= seed < 2**32:
                    raise ValueError(f"{id}: generated shots need a cast and a 32-bit nonnegative seed.")
                prompt, refs = build_prompt(style, row, cast, self.actors, location)
                for kind, path in refs:
                    if path not in hashes:
                        hashes[path] = file_sha(path)
                key = digest([PROFILE, self.canvas[:2], seconds, seed, prompt,
                              [(kind, hashes[path]) for kind, path in refs]])
                media = self.root / "work" / "takes" / id / key / "clip.mp4"
            folder = self.root / "work" / "takes" / id / key
            self.jobs[id] = Job(row, key, folder, media, prompt, refs, imported)
        self.seconds = sum(job.row["seconds"] for job in self.jobs.values())
        if not self.jobs or ("target_seconds" in data and data["target_seconds"] != self.seconds):
            raise ValueError(f"Shot lengths total {self.seconds}s; verify target_seconds.")

    def select(self, ids):
        if len(ids) != len(set(ids)):
            raise ValueError("Repeated shot ids.")
        return [self.jobs[id] for id in ids] if ids else list(self.jobs.values())


def probe(path, count=False):
    cmd = ["ffprobe", "-v", "error", "-show_streams", "-of", "json"]
    if count:
        cmd.append("-count_frames")
    return json.loads(subprocess.check_output([*cmd, str(path)]))["streams"]


def media_info(job, for_cut=False):
    if not job.source:
        record = load_json(job.folder / "render.json")
        if record["key"] != job.key or record["sha256"] != file_sha(job.media):
            raise ValueError(f"{job.id}: generated take checksum mismatch.")
    streams = probe(job.media)
    v = next((s for s in streams if s["codec_type"] == "video"), {})
    a = next((s for s in streams if s["codec_type"] == "audio"), {})
    seconds = job.row["seconds"] + (job.row.get("trim_start", 0) if for_cut else 0)
    if float(v.get("duration", 0)) + 0.005 < seconds or float(a.get("duration", 0)) + 0.05 < seconds:
        raise ValueError(f"{job.id}: expected video and audio lasting at least {seconds}s.")
    return v, a


def approve(project, ids):
    if not ids:
        raise ValueError("Specify shot ids after reviewing the takes.")
    jobs = project.select(ids)
    for job in jobs:
        media_info(job, for_cut=True)
        ffmpeg(["-i", job.media, "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"])
    for job in jobs:
        save_json(job.folder / "approval.json", approval_record(job))
        print(f"Approved {job.id} ({job.key[:8]})")


def approval_record(job):
    record = {"key": job.key, "sha256": file_sha(job.media)}
    if trim := job.row.get("trim_start", 0):
        record["trim_start"] = trim
    return record


def approved(job):
    path = job.folder / "approval.json"
    return (path.is_file() and job.media.is_file()
            and load_json(path) == approval_record(job))


def pending(jobs):
    missing = []
    for job in jobs:
        if job.source:
            raise ValueError(f"{job.id} uses an imported clip; select generated shots for rendering.")
        if (job.folder / "render.json").exists():
            media_info(job)
        else:
            missing.append(job)
    return missing


def prepare(project, jobs):
    for job in jobs:
        if not job.source:
            atomic(job.folder / "prompt.txt", job.prompt.encode())
        state = "approved" if approved(job) else "ready for review" if job.media.is_file() else "pending render"
        print(f"{job.id:12} {job.row['seconds']:2}s  {state:16} {job.key[:8]}")
    print(f"{len(project.jobs)} shots / {project.seconds}s total")


def review(project):
    esc = html.escape
    relative = lambda path: quote(path.relative_to(project.root).as_posix())
    cards = []
    for job in project.jobs.values():
        status = "待生成"
        content = ""
        if job.media.is_file():
            status = "已确认" if approved(job) else "待审片"
            content = f'<video controls preload="metadata" src="{relative(job.media)}"></video>'
        dialogue = "".join(f"<li>{esc(line['speaker'])} · {esc(line['text'])}</li>"
                           for line in job.row.get("dialogue", []))
        asr = ""
        reports = [("按成片时长转写", "asr.json"), ("原始镜头转写", "asr-full.json")]
        if job.media.is_file():
            expected = "".join(line["text"] for line in job.row.get("dialogue", []))
            checksum = file_sha(job.media)
            for label, name in reports:
                report = job.folder / name
                if report.is_file():
                    record = load_json(report)
                    if record["sha256"] == checksum and record["expected"] == expected and (
                            name == "asr-full.json" or record.get("trim_start", 0) == job.row.get("trim_start", 0)):
                        heard = esc(record["heard"]) or "未识别到对白"
                        asr += f'<p>{label} {record["similarity"]:.0%} · {heard}</p>'
        details = f"<details><summary>H3 提示词</summary><pre>{esc(job.prompt)}</pre></details>" if job.prompt else ""
        cards.append(f'<article><h2>{job.id} · {esc(job.row["title"])}</h2>'
                     f'<small>{status} · {job.row["seconds"]} 秒 · {job.key[:8]}</small>{content}'
                     f'<p>{esc(job.row["action"])}</p><ol>{dialogue}</ol>{asr}{details}</article>')
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{background:#101418;color:#eaf1f2;margin:2rem auto;padding:0 1.25rem;max-width:72rem;font:16px/1.5 system-ui,sans-serif}
h1{font-size:2rem} h2{font-size:1.125rem;margin:.1rem 0}
small{color:#a4d7cc} main{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,19rem),1fr));gap:1rem}
article{background:#1a2026;border:1px solid #34424d;border-radius:10px;padding:1rem}
video{max-width:100%;height:280px;width:100%;background:#101418;margin-top:1rem} pre{white-space:pre-wrap;overflow-wrap:anywhere}
</style>"""
    title = esc(project.title)
    document += (f"<title>{title} · 审片</title><h1>{title}</h1>"
                 f"<p>{len(cards)} 镜 · {project.seconds} 秒</p><main>{''.join(cards)}</main></html>")
    path = project.root / "review.html"
    atomic(path, document.encode())
    print(path)


def ffmpeg(args, cwd=None):
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-xerror", "-nostdin", "-n", *map(str, args)],
                   check=True, cwd=cwd)


def timestamp(seconds):
    milliseconds = round(seconds * 1000)
    h, rest = divmod(milliseconds, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def export_key(project):
    cuts = [(job.key, file_sha(job.media)) for job in project.jobs.values()]
    music_hash = file_sha(project.music[0]) if project.music else None
    return digest(["cut-v1", project.data, cuts, music_hash])[:16]


def export_cut(project):
    jobs = list(project.jobs.values())
    unapproved = [job.id for job in jobs if not approved(job)]
    if unapproved:
        raise ValueError(f"Review and approve current takes first: {', '.join(unapproved)}")
    key = export_key(project)
    out = project.root / "dist" / key
    if out.exists():
        result = load_json(out / "report.json")
        if result["sha256"] != file_sha(out / "film.mp4"):
            raise ValueError(f"Existing export has been modified: {out}")
        print(out / "film.mp4")
        return
    out.parent.mkdir(exist_ok=True)
    _, _, width, height = project.canvas
    with tempfile.TemporaryDirectory(prefix=".cut-", dir=out.parent) as temp:
        work = Path(temp)
        cues, offset = [], 0
        for num, job in enumerate(jobs, 1):
            media_info(job, for_cut=True)
            duration = job.row["seconds"]
            inputs = ["-ss", job.row.get("trim_start", 0), "-i", job.media]
            ffmpeg([*inputs, "-map", "0:v:0", "-an", "-vf",
                    f"fps={FPS},scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2,"
                    f"crop={width}:{height},setsar=1,format=yuv420p",
                    "-frames:v", job.frames, "-c:v", "libx264", "-threads", 2, "-crf", 19,
                    work / f"v{num}.mp4"])
            ffmpeg([*inputs, "-map", "0:a:0", "-vn", "-af", f"apad,atrim=duration={duration}",
                    "-ar", 48000, "-ac", 2, "-c:a", "pcm_s16le", work / f"a{num}.wav"])
            if not job.row.get("native_captions"):
                for line in job.row.get("subtitles", job.row.get("dialogue", [])):
                    start, end = (timestamp(offset + line[field]) for field in ("start", "end"))
                    cues.append(f"{len(cues) + 1}\n{start} --> {end}\n{line['text']}\n")
            offset += duration
        for prefix, ext in [("v", "mp4"), ("a", "wav")]:
            (work / f"{prefix}.txt").write_text("".join(f"file {prefix}{n}.{ext}\n" for n in range(1, len(jobs)+1)))
        (work / "subtitles.srt").write_text("\n".join(cues), encoding="utf-8")
        args = ["-f", "concat", "-safe", 1, "-i", "v.txt", "-f", "concat", "-safe", 1, "-i", "a.txt"]
        maps = ["-map", "0:v:0", "-map", "1:a:0"]
        next_input = 2
        if project.music:
            music, gain = project.music
            args += ["-stream_loop", -1, "-i", music]
            options = subprocess.check_output(["ffmpeg", "-hide_banner", "-h", "filter=alimiter"], text=True)
            # Older limiters need manual compensation for 5 ms lookahead at 48 kHz.
            limiter = "alimiter=limit=0.95:latency=1" if re.search(r"^\s+latency\s", options, re.M) else (
                "apad=pad_len=240,alimiter=limit=0.95:attack=5,atrim=start_sample=240,asetpts=PTS-STARTPTS")
            maps = ["-filter_complex",
                    f"[2:a]volume={gain},aresample=48000,aformat=channel_layouts=stereo[bed];"
                    f"[1:a][bed]amix=inputs=2:duration=first:normalize=0,{limiter}[mix]",
                    "-map", "0:v:0", "-map", "[mix]"]
            next_input += 1
        if cues:
            args += ["-i", "subtitles.srt"]
            maps += ["-map", f"{next_input}:s:0", "-c:s", "mov_text", "-metadata:s:s:0", "language=zho"]
        ffmpeg([*args, *maps, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", "film.mp4"], cwd=work)
        v, a = probe(work / "film.mp4", count=True)[:2]
        if (int(v["nb_read_frames"]) != project.seconds * FPS or (v["width"], v["height"]) != (width, height)
                or Fraction(v["avg_frame_rate"]) != FPS or Fraction(v["duration"]) != project.seconds
                or abs(float(a["duration"]) - project.seconds) > 0.03):
            raise ValueError("Final export failed frame count, canvas, FPS or audio duration verification.")
        ffmpeg(["-i", work / "film.mp4", "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"])
        delivery = work / "delivery"
        delivery.mkdir()
        (work / "film.mp4").rename(delivery / "film.mp4")
        (work / "subtitles.srt").rename(delivery / "timeline.srt")
        save_json(delivery / "report.json", {"seconds": project.seconds, "frames": int(v["nb_read_frames"]),
            "fps": FPS, "width": width, "height": height, "audio_seconds": float(a["duration"]),
            "sha256": file_sha(delivery / "film.mp4"), "shots": [{"id": j.id, "key": j.key} for j in jobs]})
        delivery.rename(out)
    print(out / "film.mp4")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("prepare", "approve", "pending"):
        sub.add_parser(action).add_argument("ids", nargs="*")
    sub.add_parser("review")
    sub.add_parser("cut")
    args = parser.parse_args()
    try:
        project = Project(args.project)
        if args.action == "prepare":
            prepare(project, project.select(args.ids))
        elif args.action == "pending":
            for job in pending(project.select(args.ids)):
                print(job.id)
        elif args.action == "review":
            review(project)
        elif args.action == "approve":
            approve(project, args.ids)
        else:
            export_cut(project)
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        print(f"Workflow error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
