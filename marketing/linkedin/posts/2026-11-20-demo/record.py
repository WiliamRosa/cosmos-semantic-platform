"""Records a captioned walkthrough of the live demo console to a .webm."""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "https://wiliamrosa.github.io/cosmos-semantic-platform/"
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)

CAPTION_JS = """(text) => {
  let el = document.getElementById('__cap');
  if (!el) {
    el = document.createElement('div'); el.id = '__cap';
    el.style.cssText = 'position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:99999;'
      + 'background:#0b1220;color:#f8fafc;font:600 22px Inter,system-ui,sans-serif;padding:14px 26px;'
      + 'border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.35);border:2px solid #38bdf8;'
      + 'max-width:1100px;text-align:center;transition:opacity .25s';
    document.body.appendChild(el);
  }
  el.textContent = text;
}"""


def cap(pg, text):
    pg.evaluate(CAPTION_JS, text)


def go(pg, path, text, wait=900):
    pg.goto(BASE + path)
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(300)
    cap(pg, text)
    pg.wait_for_timeout(wait)


def scroll(pg, dy, steps=12, pause=60):
    for _ in range(steps):
        pg.mouse.wheel(0, dy / steps)
        pg.wait_for_timeout(pause)


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(
        viewport={"width": 1280, "height": 720},
        record_video_dir=str(OUT),
        record_video_size={"width": 1280, "height": 720},
        locale="en-US",
    )
    pg = ctx.new_page()
    pg.mouse.move(640, 360)

    go(pg, "", "Can we trust the numbers? Every governed metric, its health and its owner", 3000)
    scroll(pg, 520)
    pg.wait_for_timeout(2200)

    go(pg, "answers/", "Every answer an AI agent gets is audited", 2500)
    go(pg, "answers/?id=q_53b61f2a0ce74791a04b", "The evidence: metric, rules applied, row filters, reference date", 3200)
    scroll(pg, 640)
    pg.wait_for_timeout(1800)
    cap(pg, "Fingerprints and the exact SQL, so anyone can reproduce the number")
    scroll(pg, 420)
    pg.wait_for_timeout(2600)

    go(pg, "answers/", "Personal data never reaches an agent", 1200)
    pg.get_by_text("customers.email").first.click()
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(300)
    cap(pg, "Personal data never reaches an agent: the request is denied and logged")
    pg.wait_for_timeout(3200)

    go(pg, "processes/", "Business processes as a graph", 1200)
    links = pg.locator("main a[href*='processes/?']")
    if links.count():
        links.first.click()
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(600)
        cap(pg, "Business processes as a graph: colors show health, from source system to process")
    pg.wait_for_timeout(3800)

    go(pg, "catalog/?id=metric%3Arevenue", "One definition of revenue, with owner, rules and tests", 3200)
    scroll(pg, 500)
    pg.wait_for_timeout(2200)

    go(pg, "", "Open source · pip install cosmos-semantic · try the live demo", 2600)

    video = pg.video
    ctx.close()
    b.close()
    print(video.path())
