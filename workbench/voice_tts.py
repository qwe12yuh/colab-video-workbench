"""Runs on the Colab VM: synthesize candidate reference voices with a preset Qwen3-TTS model."""
import json
from pathlib import Path
import sys

import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel


def main():
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    print(f"[voice] loading {request['repo']} on {torch.cuda.get_device_name()}", flush=True)
    model = Qwen3TTSModel.from_pretrained(request["repo"], device_map="cuda:0", dtype=dtype)
    kind, prompt = request["kind"], None
    if kind == "clone":
        ref_text = request.get("ref_text") or None
        prompt = model.create_voice_clone_prompt(ref_audio=request["ref"], ref_text=ref_text,
                                                 x_vector_only_mode=ref_text is None)
    for index in range(request["count"]):
        torch.manual_seed(request["seed"] + index)
        print(f"[voice] take {index + 1}/{request['count']}", flush=True)
        if kind == "design":
            wavs, rate = model.generate_voice_design(text=request["text"], language="Chinese",
                                                     instruct=request["instruct"])
        elif kind == "custom":
            extra = {"instruct": request["instruct"]} if request.get("instruct") else {}
            wavs, rate = model.generate_custom_voice(text=request["text"], language="Chinese",
                                                     speaker=request["speaker"], **extra)
        elif kind == "clone":
            wavs, rate = model.generate_voice_clone(text=request["text"], language="Chinese",
                                                    voice_clone_prompt=prompt)
        else:
            raise SystemExit(f"Unknown voice kind: {kind}")
        sf.write(out / f"take{index + 1}.wav", wavs[0], rate)
    print("[voice] done", flush=True)


if __name__ == "__main__":
    main()
