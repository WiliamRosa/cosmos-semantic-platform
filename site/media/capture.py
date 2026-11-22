"""Capture the console media used by the website, the README and link previews, from the live demo.

Writes, next to this file:
  console-demo.mp4        45 s captioned walkthrough (website hero), H.264
  console-overview.png    the overview page (video poster)
  console-answer.png      an audited answer
  console-process.png     a business process graph
and in docs/assets/:
  console-tour.gif        the README hero, from the same walkthrough
  social-preview.png      1280x640 link preview (GitHub social preview, og:image), from
                          social-preview.html with the logo from marketing/brand

Run it after the console changes and the demo has been deployed:

    uv run --no-project --with playwright --with imageio-ffmpeg python site/media/capture.py

Needs Google Chrome. The walkthrough follows the script of the published LinkedIn demo
(marketing/linkedin/posts/2026-11-20-demo/record.py).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import Page, sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
ASSETS = ROOT / "docs" / "assets"
BASE = "https://wiliamrosa.github.io/cosmos-semantic-platform/"

CAPTION_JS = """(text) => {
  let el = document.getElementById('__cap');
  if (!el) {
    el = document.createElement('div'); el.id = '__cap';
    el.style.cssText = 'position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:99999;'
      + 'background:#0b1220;color:#f8fafc;font:600 22px Inter,system-ui,sans-serif;padding:14px 26px;'
      + 'border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.35);border:2px solid #38bdf8;'
      + 'max-width:1100px;text-align:center';
    document.body.appendChild(el);
  }
  el.textContent = text;
}"""


def open_page(pg: Page, path: str, wait: int = 600) -> None:
    pg.goto(BASE + path)
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(wait)


def first_answer(pg: Page) -> None:
    """Open the first audited answer: query ids change whenever the demo is rebuilt."""
    open_page(pg, "answers/")
    pg.locator("main a[href*='answers/?id=']").first.click()
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(900)
    if "not in the demo" in pg.locator("main").inner_text():
        raise SystemExit("the first answer did not load")


def first_process(pg: Page) -> None:
    open_page(pg, "processes/")
    links = pg.locator("main a[href*='processes/?']")
    if links.count():
        links.first.click()
        pg.wait_for_load_state("networkidle")
        pg.locator(".react-flow__node").first.wait_for(timeout=15000)
        pg.wait_for_timeout(900)


def screenshots(browser) -> None:
    pg = browser.new_page(viewport={"width": 1440, "height": 900}, locale="en-US", color_scheme="light")
    open_page(pg, "", 1200)
    pg.screenshot(path=str(HERE / "console-overview.png"))
    first_answer(pg)
    pg.screenshot(path=str(HERE / "console-answer.png"))
    first_process(pg)
    pg.screenshot(path=str(HERE / "console-process.png"))
    pg.close()


def walkthrough(browser, out_dir: Path) -> Path:
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 720},
        record_video_dir=str(out_dir),
        record_video_size={"width": 1280, "height": 720},
        locale="en-US",
        color_scheme="light",
    )
    pg = ctx.new_page()
    pg.mouse.move(640, 360)

    def cap(text: str) -> None:
        pg.evaluate(CAPTION_JS, text)

    def go(path: str, text: str, wait: int = 900) -> None:
        open_page(pg, path, 300)
        cap(text)
        pg.wait_for_timeout(wait)

    def scroll(dy: int, steps: int = 12, pause: int = 60) -> None:
        for _ in range(steps):
            pg.mouse.wheel(0, dy / steps)
            pg.wait_for_timeout(pause)

    go("", "Can we trust the numbers? Every governed metric, its health and its owner", 3000)
    scroll(520)
    pg.wait_for_timeout(2200)
    go("answers/", "Every answer an AI agent gets is audited", 2500)
    first_answer(pg)
    cap("The evidence: metric, rules applied, row filters, reference date")
    pg.wait_for_timeout(3200)
    scroll(640)
    pg.wait_for_timeout(1800)
    cap("Fingerprints and the exact SQL, so anyone can reproduce the number")
    scroll(420)
    pg.wait_for_timeout(2600)
    go("answers/", "Personal data never reaches an agent", 1200)
    pg.get_by_text("customers.email").first.click()
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(300)
    cap("Personal data never reaches an agent: the request is denied and logged")
    pg.wait_for_timeout(3200)
    go("processes/", "Business processes as a graph", 1200)
    links = pg.locator("main a[href*='processes/?']")
    if links.count():
        links.first.click()
        pg.wait_for_load_state("networkidle")
        pg.locator(".react-flow__node").first.wait_for(timeout=15000)
        pg.wait_for_timeout(600)
        cap("Business processes as a graph: colors show health, from source system to process")
    pg.wait_for_timeout(3800)
    go("catalog/?id=measure%3Arevenue", "One definition of revenue, with owner, rules and tests", 3200)
    scroll(500)
    pg.wait_for_timeout(2200)
    go("", "Open source · pip install cosmos-semantic · try the live demo", 2600)

    video = pg.video
    ctx.close()
    return Path(video.path())


def encode(webm: Path) -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    run = lambda *args: subprocess.run([ffmpeg, "-y", "-loglevel", "error", *args], check=True)  # noqa: E731
    run("-i", str(webm), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26", "-preset", "slow",
        "-movflags", "+faststart", "-an", str(HERE / "console-demo.mp4"))
    # README GIF: 800 px wide, 5 fps, palette per video, sped up 1.5x to stay a few MB.
    palette = webm.with_suffix(".png")
    filters = "setpts=PTS/1.5,fps=5,scale=800:-1:flags=lanczos"
    run("-i", str(webm), "-vf", f"{filters},palettegen=max_colors=96:stats_mode=diff", str(palette))
    run("-i", str(webm), "-i", str(palette), "-lavfi",
        f"{filters}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
        str(ASSETS / "console-tour.gif"))


def social_preview(browser) -> None:
    pg = browser.new_page(viewport={"width": 1280, "height": 640}, device_scale_factor=1)
    pg.goto((HERE / "social-preview.html").as_uri())
    pg.wait_for_load_state("networkidle")
    pg.evaluate("document.fonts.ready")
    pg.wait_for_timeout(300)
    pg.screenshot(path=str(ASSETS / "social-preview.png"))
    pg.close()


def main() -> None:
    with sync_playwright() as p, tempfile.TemporaryDirectory() as tmp:
        browser = p.chromium.launch(channel="chrome")
        screenshots(browser)
        social_preview(browser)  # uses console-overview.png
        webm = walkthrough(browser, Path(tmp))
        browser.close()
        encode(webm)
        shutil.rmtree(tmp, ignore_errors=True)
    for f in ["console-demo.mp4", "console-overview.png", "console-answer.png", "console-process.png"]:
        print(f"site/media/{f}: {(HERE / f).stat().st_size / 1e6:.1f} MB")
    for f in ["console-tour.gif", "social-preview.png"]:
        print(f"docs/assets/{f}: {(ASSETS / f).stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
