"""Synthesizes each segment's narration with Piper (offline neural TTS) and records its length.

Usage: uv run --with piper-tts python tts.py <voice.onnx> <espeak-ng-data dir> <work dir>
Writes <work>/audio/<id>.wav and <work>/durations.json.
"""

import json
import sys
import wave
from pathlib import Path

from piper import PiperVoice

voice_path, espeak_dir, work = sys.argv[1], sys.argv[2], Path(sys.argv[3])
segments = json.loads((Path(__file__).parent / "segments.json").read_text(encoding="utf-8"))
voice = PiperVoice.load(voice_path, espeak_data_dir=espeak_dir)
(work / "audio").mkdir(parents=True, exist_ok=True)
durations = {}
for seg in segments:
    path = work / "audio" / f"{seg['id']}.wav"
    with wave.open(str(path), "wb") as w:
        voice.synthesize_wav(seg["narration"], w)
    with wave.open(str(path), "rb") as r:
        durations[seg["id"]] = r.getnframes() / r.getframerate()
    print(f"{seg['id']:12} {durations[seg['id']]:5.1f}s")
(work / "durations.json").write_text(json.dumps(durations, indent=2), encoding="utf-8")
print(f"total narration {sum(durations.values()):.1f}s")
