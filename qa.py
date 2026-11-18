#!/usr/bin/env python3
"""Check spoken lines locally with Faster Whisper and keep an auditable review record."""
import argparse
import difflib
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import wave

from studio import Project, ffmpeg, file_sha, media_info, save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--model", default="small")
    parser.add_argument("--full-take", action="store_true", help="Check uncropped source audio.")
    args = parser.parse_args()
    try:
        import numpy as np
        from faster_whisper import WhisperModel

        project = Project(args.project)
        jobs = project.select(args.ids)
        model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=4)
        for job in jobs:
            media_info(job, for_cut=not args.full_take)
            trim = 0 if args.full_take else job.row.get("trim_start", 0)
            limit = [] if args.full_take else ["-t", job.row["seconds"]]
            with tempfile.TemporaryDirectory() as tmp:
                wav = Path(tmp) / "speech.wav"
                ffmpeg(["-ss", trim, "-i", job.media, "-map", "0:a:0", *limit, "-ar", 16000, "-ac", 1, wav])
                with wave.open(str(wav), "rb") as stream:
                    audio = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float32) / 32768
                segments, _ = model.transcribe(audio, language="zh", beam_size=5, condition_on_previous_text=False)
                segments = [{"start": part.start, "end": part.end, "text": part.text.strip()} for part in segments]
            lines = job.row.get("dialogue", [])
            expected = "".join(line["text"] for line in lines)
            heard = "".join(part["text"] for part in segments)
            norm = lambda value: re.sub(r"[\W_]+", "", value)
            score = round(difflib.SequenceMatcher(None, norm(expected), norm(heard)).ratio(), 3)
            name = "asr-full.json" if args.full_take else "asr.json"
            save_json(job.folder / name, {"key": job.key, "sha256": file_sha(job.media),
                "model": args.model, "trim_start": trim, "expected": expected, "heard": heard, "similarity": score, "segments": segments})
            print(f"{job.id} · {score:.3f} · {heard}", flush=True)
    except (ValueError, RuntimeError, ImportError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Dialogue check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
