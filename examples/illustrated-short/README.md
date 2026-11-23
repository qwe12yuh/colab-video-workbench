# Illustrated Shorts

Render a vertical motion comic locally from illustrations and recorded or
generated voice clips. This standalone example uses Python 3.10+, FFmpeg
with H.264 and AAC encoders, and a TrueType font. It does not use the
workbench server, Colab, or a voice synthesis service.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python render.py story.json out/short.mp4
```

On a system without DejaVu Sans at the default Linux location, pass
`--font /path/to/your/font.ttf`. Use a font that covers the caption language.

Put your illustrations and audio next to `story.json` and reference them by
filename. The same shot can appear in multiple consecutive beats:

```json
{
  "title": "The Spare Key",
  "shots": {
    "door": {"image": "door.jpg", "focus": [0.5, 0.45], "zoom": 0.1},
    "close-up": {"image": "key.jpg", "focus": [0.57, 0.7]}
  },
  "beats": [
    {"shot": "door", "audio": "take-01.wav", "caption": "Where'd you hide the spare key?", "hold": 0.2},
    {"shot": "close-up", "audio": "take-02.wav", "caption": "Under the "no spare key" sign.", "color": "#ffbf6e"},
    {"shot": "close-up", "duration": 0.6}
  ]
}
```

Each audio clip sets a beat's minimum duration; `duration` can extend it.
`hold` adds silence after that, and `gain` scales the clip. If you use a
contact sheet, give a shot a `crop` rectangle `[left, top, right, bottom]`
in image pixels. `focus` uses normalized image coordinates, and `zoom`
is the amount of the push-in. Use short captions and split long lines.

The output is 1080×1920 at 30 fps with burned-in captions and a separate
SRT with matching timing. To verify the pipeline without media assets or
external services, run `.venv/bin/python smoke.py` (requires `ffprobe`).
