"""Render two synthetic shots and verify the MP4 and SRT without networking."""

import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw, ImageFont

from render import export


def main():
    font = getattr(ImageFont.truetype("DejaVuSans.ttf", 16), "path")
    with TemporaryDirectory() as temp:
        path = Path(temp)
        sheet = Image.new("RGB", (640, 480), "#248578")
        ImageDraw.Draw(sheet).rectangle((320, 0, 639, 479), fill="#bf7842")
        sheet.save(path / "sheet.png")
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=0.4",
            str(path / "beep.wav"),
        ], check=True)
        project = {
            "title": "Illustration Smoke Test",
            "shots": {
                "first": {"image": "sheet.png", "crop": [0, 0, 320, 480]},
                "close": {"image": "sheet.png", "crop": [320, 0, 640, 480], "focus": [.2, .4]},
            },
            "beats": [
                {"shot": "first", "caption": "First line.", "audio": "beep.wav", "hold": .1},
                {"shot": "close", "duration": .2},
                {"shot": "close", "caption": "Last line.", "duration": .3},
            ],
        }
        project_file = path / "project.json"
        output = path / "out" / "short.mp4"
        project_file.write_text(json.dumps(project))
        export(project_file, output, font)
        meta = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output),
        ]))
        video, audio = meta["streams"]
        assert (video["codec_name"], video["width"], video["height"], video["r_frame_rate"]) == ("h264", 1080, 1920, "30/1")
        assert (audio["codec_name"], audio["sample_rate"]) == ("aac", "48000")
        assert video["nb_frames"] == "30", video["nb_frames"]
        assert output.with_suffix(".srt").read_text() == (
            "1\n00:00:00,000 --> 00:00:00,500\nFirst line.\n\n"
            "2\n00:00:00,700 --> 00:00:01,000\nLast line.\n"
        )
        subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"], check=True)
    print("Illustrated Short render passed")


if __name__ == "__main__":
    main()
