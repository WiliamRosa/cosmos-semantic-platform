# Demo video

Builds the narrated product walkthrough: the GitHub repository and its quickstart, `pip install` in a terminal, the same metric on five databases in Docker plus DuckDB, an AI agent refused personal data, and the live demo console.

Every video follows the same rules:

- **Dark mode** everywhere: GitHub, the terminal and the console (`color_scheme="dark"`).
- **Captions burned in**, plus an `.srt` file to upload to LinkedIn as the video's captions.
- **Narration** in English, synthesized offline with [Piper](https://github.com/OHF-Voice/piper1-gpl), voice `en_US-ryan-high`.
- **Real output only.** The terminal replays what `capture.sh` recorded from the real commands, never typed-in results.

## Files

| File | What it does |
| --- | --- |
| `segments.json` | The script: one entry per scene, with the narration (also the caption) and what the scene shows |
| `capture.sh` | Starts the databases, installs from PyPI in a fresh environment and saves every command's output |
| `terminal.html` | The dark terminal that types each command and prints its captured output |
| `tts.py` | Synthesizes each segment's narration and records its length |
| `record.py` | Records the browser in dark mode; each segment lasts as long as its narration |
| `mux.py` | Mixes the narration onto the video (H.264 + AAC) and writes the `.srt` |

## Make it

Needs Docker, [uv](https://docs.astral.sh/uv/), Python with Playwright and Chromium, and ffmpeg (or `uv run --with imageio-ffmpeg`).

```bash
W=/tmp/cosmos-video
bash marketing/linkedin/video/capture.sh $W
uv run --with piper-tts python -m piper.download_voices en_US-ryan-high --download-dir $W
uv run --with piper-tts python marketing/linkedin/video/tts.py $W/en_US-ryan-high.onnx <piper's espeak-ng-data dir> $W
python marketing/linkedin/video/record.py $W $W/captured
python marketing/linkedin/video/mux.py ffmpeg $W walkthrough.mp4
```

On Windows, Piper's espeak-ng data must sit on a path without accents: copy the package's `espeak-ng-data` folder to such a path and pass it to `tts.py`.

To change the story, edit `segments.json`. Keep each narration one or two short sentences, and only claim what the repository shows.
