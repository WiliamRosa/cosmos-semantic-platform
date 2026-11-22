"""Build the project website (cosmosdata.ai) into site/dist.

The pages live in site/src. Media and logos are copied from where the repository already keeps
them (docs/assets, the published LinkedIn posts and marketing/brand), so nothing is committed twice.

Usage: python site/build.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "site" / "src"
DIST = ROOT / "site" / "dist"
POSTS = ROOT / "marketing" / "linkedin" / "posts"
BRAND = ROOT / "marketing" / "brand"
MEDIA_DIR = ROOT / "site" / "media"

# Published path under dist/ -> logo file; the logo's single source is marketing/brand.
LOGOS = {
    "favicon.svg": BRAND / "cosmos-semantic-icon.svg",
    "assets/apple-touch-icon.png": BRAND / "png" / "cosmos-semantic-icon-180.png",
    "assets/logo-horizontal-dark.svg": BRAND / "cosmos-semantic-horizontal-dark.svg",
}

# Published path under dist/assets -> source in the repository.
MEDIA = {
    "social-preview.png": ROOT / "docs" / "assets" / "social-preview.png",
    # Current console, captured from the live demo by site/media/capture.py.
    "console-overview.png": MEDIA_DIR / "console-overview.png",
    "console-answer.png": MEDIA_DIR / "console-answer.png",
    "console-process.png": MEDIA_DIR / "console-process.png",
    "console-demo.mp4": MEDIA_DIR / "console-demo.mp4",
    # As published on LinkedIn.
    "cosmos-semantic-carousel.pdf": POSTS / "2026-11-20-carousel" / "cosmos-semantic-carousel.pdf",
    "walkthrough.mp4": POSTS / "2026-11-20-walkthrough" / "cosmos-semantic-walkthrough.mp4",
}


def main() -> None:
    # Empty dist/ instead of deleting it: on Windows an editor or shell holding the folder open
    # blocks removing the folder itself, but not its contents.
    DIST.mkdir(exist_ok=True)
    for entry in DIST.iterdir():
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    shutil.copytree(SRC, DIST, dirs_exist_ok=True)

    assets = DIST / "assets"
    assets.mkdir(exist_ok=True)
    sources = list(MEDIA.values()) + list(LOGOS.values())
    missing = [str(path.relative_to(ROOT)) for path in sources if not path.is_file()]
    if missing:
        raise SystemExit("missing media: " + ", ".join(missing))
    for name, path in MEDIA.items():
        shutil.copy2(path, assets / name)
    for name, path in LOGOS.items():
        shutil.copy2(path, DIST / name)

    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"built {DIST.relative_to(ROOT)} ({size / 1_000_000:.1f} MB)")


if __name__ == "__main__":
    main()
