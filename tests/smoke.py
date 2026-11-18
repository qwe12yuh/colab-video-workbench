"""Local integration check using the project's two synthetic source clips."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from studio import Project, ffmpeg, load_json, probe, save_json


def main():
    sample = ROOT / "examples" / "demo"
    with tempfile.TemporaryDirectory(prefix="video-smoke-") as directory:
        folder = Path(directory)
        shutil.copytree(sample / "assets", folder / "assets")
        manifest = folder / "project.json"
        data = load_json(sample / "project.json")
        # The test fixture uses a generated tone to exercise the separate music mix.
        ffmpeg(["-f", "lavfi", "-i", "sine=frequency=110:duration=1", folder / "assets" / "tone.wav"])
        data["music"] = {"file": "assets/tone.wav", "gain": 0.03}
        data["shots"][0]["native_captions"] = False
        data["shots"][0]["subtitles"] = [{"text": "测试字幕", "start": .5, "end": 2.0}]
        save_json(manifest, data)

        def cli(*args, code=0):
            result = subprocess.run([sys.executable, str(ROOT / "studio.py"), str(manifest), *args],
                                    text=True, capture_output=True)
            if result.returncode != code:
                raise AssertionError(f"{args}: {result.returncode}\n{result.stdout}\n{result.stderr}")
            return result.stdout, result.stderr

        out, err = cli("cut", code=1)
        assert "Review and approve" in err
        cli("prepare")
        cli("approve", "s01", "s02")
        cli("review")
        assert (folder / "review.html").read_text().count("<video ") == 2
        output = Path(cli("cut")[0].strip())
        video, audio, subtitle = probe(output, count=True)
        assert (video["width"], video["height"], int(video["nb_read_frames"])) == (720, 1280, 432)
        assert float(audio["duration"]) == 18.0 and subtitle["codec_name"] == "mov_text"
        assert "00:00:00,500 --> 00:00:02,000" in (output.parent / "timeline.srt").read_text()
        assert Path(cli("cut")[0].strip()) == output

        # Changed source bytes invalidate approval and prevent silent replacement.
        source = folder / "assets" / "01.mp4"
        with source.open("ab") as stream:
            stream.write(b"changed")
        assert "s01" in cli("cut", code=1)[1]
        assert output.is_file()

        # Subtitle-only edits don't invalidate a model request, but a seed change does.
        slate = copy.deepcopy(data)
        row = slate["shots"][0]
        row.pop("source")
        row["seed"] = 123
        save_json(manifest, slate)
        first = Project(manifest).jobs["s01"].key
        row["subtitles"][0]["text"] = "调整字幕"
        save_json(manifest, slate)
        assert Project(manifest).jobs["s01"].key == first
        row["seed"] = 124
        save_json(manifest, slate)
        assert Project(manifest).jobs["s01"].key != first
        row["subtitles"][0]["end"] = 9
        save_json(manifest, slate)
        assert "inside the shot" in cli("prepare", code=1)[1]
        print(json.dumps({"seconds": 18, "frames": 432, "checks": "pass"}))


if __name__ == "__main__":
    main()
