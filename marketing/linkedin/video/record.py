"""Records the demo video: GitHub, a terminal replaying real command output, then the live demo console.

Everything is in dark mode, with the narration burned in as captions. Each segment lasts at least as long
as its narration (from durations.json) and its start time is saved for the audio mix and the .srt file.

Usage: python record.py <work dir> <captured outputs dir>
"""

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
WORK = Path(sys.argv[1])
CAPTURED = Path(sys.argv[2])
REPO = "https://github.com/WiliamRosa/cosmos-semantic-platform"
DEMO = "https://wiliamrosa.github.io/cosmos-semantic-platform/"
SEGMENTS = json.loads((HERE / "segments.json").read_text(encoding="utf-8"))
DURATIONS = json.loads((WORK / "durations.json").read_text(encoding="utf-8"))
PAD = 0.7  # seconds of silence after each narration

CAPTION_JS = """(text) => {
  let el = document.getElementById('__cap');
  if (!el) {
    el = document.createElement('div'); el.id = '__cap';
    el.style.cssText = 'position:fixed;left:50%;bottom:26px;transform:translateX(-50%);z-index:2147483647;'
      + 'background:rgba(2,6,23,.92);color:#f8fafc;font:600 21px/1.35 Inter,system-ui,sans-serif;padding:12px 24px;'
      + 'border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.5);border:2px solid #38bdf8;'
      + 'width:max-content;max-width:1080px;text-align:center';
    document.body.appendChild(el);
  }
  el.textContent = text;
}"""


def lines_of(seg):
    files = seg["output"] if isinstance(seg["output"], list) else [seg["output"]]
    lines = []
    for f in files:
        lines += (CAPTURED / f).read_text(encoding="utf-8").rstrip("\n").split("\n")
    if "head" in seg:
        lines = lines[: seg["head"]] + ["..."] + lines[-seg["tail"] :]
    return lines


def smooth_scroll(pg, y, ms=1200):
    pg.evaluate("(y) => window.scrollTo({top: y, behavior: 'smooth'})", y)
    pg.wait_for_timeout(ms)


with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 720},
        color_scheme="dark",
        locale="en-US",
        record_video_dir=str(WORK / "raw"),
        record_video_size={"width": 1280, "height": 720},
    )
    pg = ctx.new_page()
    t0 = time.monotonic()
    starts = {}
    on_terminal = False

    for seg in SEGMENTS:
        scene = seg["scene"]
        # Navigate first, so the segment (caption + narration) starts on a loaded page.
        if scene == "github":
            pg.goto(REPO)
            pg.wait_for_load_state("networkidle")
        elif scene == "term" and not on_terminal:
            pg.goto((HERE / "terminal.html").as_uri())
            pg.wait_for_load_state("networkidle")
            on_terminal = True
        elif scene == "web":
            pg.goto(DEMO + seg["path"])
            pg.wait_for_load_state("networkidle")
        elif scene == "answer":
            pg.goto(DEMO + "answers/")
            pg.wait_for_load_state("networkidle")
            pg.get_by_text("orders.order_size_band").first.click()
            pg.wait_for_load_state("networkidle")
        elif scene == "process":
            pg.goto(DEMO + "processes/")
            pg.wait_for_load_state("networkidle")
            pg.locator("main a[href*='processes/?']").first.click()
            pg.wait_for_load_state("networkidle")
        elif scene == "end":
            pg.goto(DEMO)
            pg.wait_for_load_state("networkidle")
        if scene != "term":
            on_terminal = False

        start = time.monotonic()
        starts[seg["id"]] = start - t0
        pg.evaluate(CAPTION_JS, seg["narration"])

        if scene == "github-quickstart":
            y = pg.evaluate("""() => { const h = [...document.querySelectorAll('h2')].find(e => e.textContent.trim() === 'Quickstart');
                                        return h.getBoundingClientRect().top + window.scrollY - 90; }""")
            smooth_scroll(pg, y, 1500)
            pg.evaluate("""() => { const h = [...document.querySelectorAll('h2')].find(e => e.textContent.trim() === 'Quickstart');
                                   let n = h.closest('.markdown-heading') || h; while (n && !n.querySelector?.('pre') && n.tagName !== 'PRE') n = n.nextElementSibling;
                                   const pre = n.tagName === 'PRE' ? n : n.querySelector('pre');
                                   (pre.closest('.highlight') || pre).style.cssText += ';outline:3px solid #38bdf8;outline-offset:6px;border-radius:6px'; }""")
        elif scene == "term":
            if seg.get("clear"):
                pg.evaluate("() => term.clear()")
            if seg.get("title"):
                pg.evaluate("(t) => term.title(t)", seg["title"])
            opt = {"prompt": seg.get("prompt", "~/shop $"), "lineMs": seg.get("lineMs", 18)}
            pg.evaluate("([c, l, o]) => term.run(c, l, o)", [seg["cmd"], lines_of(seg), opt])
        elif scene == "web" and seg.get("scroll"):
            pg.wait_for_timeout(2500)
            smooth_scroll(pg, seg["scroll"], 1500)
        elif scene == "answer":
            pg.wait_for_timeout(2500)
            smooth_scroll(pg, 650, 1500)

        remaining = DURATIONS[seg["id"]] + PAD - (time.monotonic() - start)
        if remaining > 0:
            pg.wait_for_timeout(int(remaining * 1000))
        if scene == "end":
            pg.wait_for_timeout(1500)

    total = time.monotonic() - t0
    video = pg.video
    ctx.close()
    browser.close()
    (WORK / "timeline.json").write_text(json.dumps({"starts": starts, "total": total, "video": str(video.path())}, indent=2), encoding="utf-8")
    print(json.dumps(starts, indent=1), "total", round(total, 1))
