"""Build the project website (cosmosdata.ai) into site/dist.

The pages live in site/src. Media is copied from where the repository already keeps it
(docs/assets and the published LinkedIn posts), so it is never committed twice.

Usage: python site/build.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "site" / "src"
DIST = ROOT / "site" / "dist"
POSTS = ROOT / "marketing" / "linkedin" / "posts"

# Published path under dist/assets -> source in the repository.
MEDIA = {
    "social-preview.png": ROOT / "docs" / "assets" / "social-preview.png",
    "console-overview.png": POSTS / "2026-11-20-carousel" / "assets" / "overview-clean.png",
    "console-answer.png": POSTS / "2026-11-20-carousel" / "assets" / "answer.png",
    "console-process.png": POSTS / "2026-11-20-carousel" / "assets" / "process.png",
    "cosmos-semantic-carousel.pdf": POSTS / "2026-11-20-carousel" / "cosmos-semantic-carousel.pdf",
    "console-demo.mp4": POSTS / "2026-11-20-demo" / "cosmos-semantic-demo.mp4",
    "walkthrough.mp4": POSTS / "2026-11-20-walkthrough" / "cosmos-semantic-walkthrough.mp4",
}


def main() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.copytree(SRC, DIST)

    assets = DIST / "assets"
    assets.mkdir(exist_ok=True)
    missing = [str(path.relative_to(ROOT)) for path in MEDIA.values() if not path.is_file()]
    if missing:
        raise SystemExit("missing media: " + ", ".join(missing))
    for name, path in MEDIA.items():
        shutil.copy2(path, assets / name)

    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"built {DIST.relative_to(ROOT)} ({size / 1_000_000:.1f} MB)")


if __name__ == "__main__":
    main()
