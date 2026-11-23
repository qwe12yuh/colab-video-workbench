"""Render an illustrated vertical short without a video model or a GPU."""

import argparse
from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
import subprocess
import tempfile
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT, FPS, SAMPLE_RATE = 720, 1280, 30, 48_000
ART_BOX = (58, 77, 604, 906)


@dataclass(frozen=True)
class Beat:
    shot: str
    caption: str
    color: str
    first: int
    last: int
    samples: np.ndarray
    gain: float


def load_samples(path):
    raw = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
        "-ar", str(SAMPLE_RATE), "-f", "f32le", "-",
    ])
    if not raw:
        raise ValueError(f"No audio in {path}")
    return np.frombuffer(raw, dtype="<f4")


def frames(seconds):
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Durations must be finite and non-negative")
    return math.ceil(seconds * FPS)


def timeline(project, root):
    beats = []
    frame = 0
    for item in project["beats"]:
        shot = item["shot"]
        if shot not in project["shots"]:
            raise ValueError(f"Unknown shot: {shot}")
        audio = load_samples(root / item["audio"]) if "audio" in item else np.empty(0)
        length = max(frames(item.get("duration", 0)), frames(len(audio) / SAMPLE_RATE))
        length += frames(item.get("hold", 0))
        if not length:
            raise ValueError(f"Silent beat for {shot} needs a duration")
        gain = item.get("gain", 1.0)
        if not math.isfinite(gain) or gain < 0:
            raise ValueError(f"Invalid audio gain for {shot}")
        beats.append(Beat(shot, item.get("caption", ""), item.get("color", "#fff9ee"),
                          frame, frame + length, audio, gain))
        frame += length
    if not beats:
        raise ValueError("The project needs at least one beat")
    return beats


def mix(path, beats):
    step = SAMPLE_RATE // FPS
    output = np.zeros(beats[-1].last * step, dtype=np.float32)
    for beat in beats:
        start = beat.first * step
        output[start:start + len(beat.samples)] = beat.samples * beat.gain
    pcm = (np.clip(output, -0.96, 0.96) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        wav.writeframes(pcm.tobytes())


@lru_cache(maxsize=128)
def caption_layout(text, font_path):
    words = text.split()
    for size in range(40, 25, -2):
        font = ImageFont.truetype(font_path, size)
        width = font.getlength
        if width(text) <= 574:
            return [text], font
        pairs = [(" ".join(words[:i]), " ".join(words[i:])) for i in range(1, len(words))]
        fits = [pair for pair in pairs if max(map(width, pair)) <= 574]
        if fits:
            return min(fits, key=lambda p: abs(width(p[0]) - width(p[1]))), font
    raise ValueError(f"Split the caption into multiple beats: {text}")


def camera(image, focus, progress, zoom):
    aspect = ART_BOX[2] / ART_BOX[3]
    base_w = min(image.width, image.height * aspect)
    scale = 1 + zoom * (1 - (1 - progress) ** 2)
    w, h = base_w / scale, base_w / (aspect * scale)
    cx = min(image.width - w / 2, max(w / 2, image.width * focus[0]))
    cy = min(image.height - h / 2, max(h / 2, image.height * focus[1]))
    return image.resize(ART_BOX[2:], Image.Resampling.BILINEAR,
                        box=(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))


def shot_assets(project, root):
    assets = {}
    for key, shot in project["shots"].items():
        with Image.open(root / shot["image"]) as source:
            tile = source.convert("RGB")
        if "crop" in shot:
            x0, y0, x1, y1 = shot["crop"]
            if not (0 <= x0 < x1 <= tile.width and 0 <= y0 < y1 <= tile.height):
                raise ValueError(f"Crop for {key} is outside the image")
            tile = tile.crop((x0, y0, x1, y1))
        focus = shot.get("focus", [.5, .5])
        zoom = shot.get("zoom", .08)
        if not (len(focus) == 2 and all(0 <= value <= 1 for value in focus) and 0 <= zoom <= .5):
            raise ValueError(f"Invalid camera settings for {key}")
        assets[key] = (tile, focus, zoom)
    return assets


def image_frames(project, beats, assets, font_path):
    base = Image.new("RGB", (WIDTH, HEIGHT), "#152124")
    draw = ImageDraw.Draw(base)
    draw.text((58, 42), project["title"], font=ImageFont.truetype(font_path, 24),
              fill="#fff9ee", anchor="lm")
    draw.line((58, 64, 662, 64), fill="#52605b")
    for beat in beats:
        lines, font = caption_layout(beat.caption, font_path)
        image, focus, zoom = assets[beat.shot]
        for t in range(beat.first, beat.last):
            frame = base.copy()
            p = (t - beat.first) / max(1, beat.last - beat.first - 1)
            frame.paste(camera(image, focus, p, zoom), ART_BOX[:2])
            d = ImageDraw.Draw(frame)
            y = 1045 if len(lines) == 1 else 1025
            for i, line in enumerate(lines):
                d.text((350, y + 41 * i), line, font=font, anchor="mm", fill=beat.color)
            yield frame


def stamp(frame):
    ms = round(frame * 1000 / FPS)
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, ms = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def export(project_file, output, font_path):
    project = json.loads(project_file.read_text(encoding="utf-8"))
    beats = timeline(project, project_file.parent)
    assets = shot_assets(project, project_file.parent)
    for beat in beats:
        caption_layout(beat.caption, font_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        audio, video = Path(temp) / "mix.wav", Path(temp) / "short.mp4"
        mix(audio, beats)
        cmd = [
            "ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-", "-i", str(audio),
            "-vf", "scale=1080:1920:flags=bicubic,setsar=1", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-threads", "2",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(video),
        ]
        with subprocess.Popen(cmd, stdin=subprocess.PIPE) as process:
            for frame in image_frames(project, beats, assets, font_path):
                process.stdin.write(frame.tobytes())
            process.stdin.close()
            if process.wait():
                raise RuntimeError("FFmpeg could not encode the short")
        srt = Path(temp) / "short.srt"
        srt.write_text("\n".join(
            f"{i}\n{stamp(b.first)} --> {stamp(b.last)}\n{b.caption}\n"
            for i, b in enumerate((b for b in beats if b.caption), 1)
        ), encoding="utf-8")
        video.replace(output)
        srt.replace(output.with_suffix(".srt"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--font", default="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    args = parser.parse_args()
    if args.output.suffix.lower() != ".mp4":
        parser.error("output must have an .mp4 extension")
    export(args.project, args.output, args.font)
