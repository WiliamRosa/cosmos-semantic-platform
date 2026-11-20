"""Mixes the narration onto the recorded video (H.264 + AAC MP4) and writes the captions as .srt.

Usage: python mux.py <ffmpeg> <work dir> <output.mp4>
"""

import json
import subprocess
import sys
from pathlib import Path

FFMPEG, WORK, OUT = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
HERE = Path(__file__).parent
segments = json.loads((HERE / "segments.json").read_text(encoding="utf-8"))
durations = json.loads((WORK / "durations.json").read_text(encoding="utf-8"))
timeline = json.loads((WORK / "timeline.json").read_text(encoding="utf-8"))
OFFSET = max(0.0, timeline["starts"][segments[0]["id"]] - 0.4)  # skip the first page load
starts = {k: v - OFFSET for k, v in timeline["starts"].items()}

inputs = ["-ss", f"{OFFSET:.3f}", "-i", timeline["video"]]
filters = []
for i, seg in enumerate(segments, start=1):
    inputs += ["-i", str(WORK / "audio" / f"{seg['id']}.wav")]
    ms = int(starts[seg["id"]] * 1000)
    filters.append(f"[{i}:a]aresample=48000,adelay={ms}|{ms}[a{i}]")
mix = "".join(f"[a{i}]" for i in range(1, len(segments) + 1))
filters.append(f"{mix}amix=inputs={len(segments)}:normalize=0,loudnorm=I=-16:TP=-1.5[aout]")

subprocess.run(
    [FFMPEG, "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filters),
     "-map", "0:v", "-map", "[aout]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
     "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "160k", "-movflags", "+faststart", "-shortest", str(OUT)],
    check=True,
)


def ts(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02}:{int(m):02}:{int(s):02},{int((s % 1) * 1000):03}"


srt = []
for n, seg in enumerate(segments, start=1):
    a = starts[seg["id"]]
    b = a + durations[seg["id"]] + 0.3
    srt.append(f"{n}\n{ts(a)} --> {ts(b)}\n{seg['narration']}\n")
OUT.with_suffix(".srt").write_text("\n".join(srt), encoding="utf-8")
print("wrote", OUT, "and", OUT.with_suffix(".srt"))
