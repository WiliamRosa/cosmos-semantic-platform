"""Render a LinkedIn post's card.html to card.png.

Usage: python marketing/linkedin/render.py marketing/linkedin/posts/<YYYY-MM-DD>-<slug>

The card's root element is `#card`; its CSS size sets the image size (1080x1350 for a
portrait post). Rendered at 2x for sharp text. Needs `pip install playwright` and
`playwright install chromium`.
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main(folder: str) -> None:
    root = Path(folder)
    html = (root / "card.html").resolve()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(device_scale_factor=2)
        page.goto(html.as_uri())
        page.wait_for_load_state("networkidle")
        page.evaluate("document.fonts.ready")
        page.locator("#card").screenshot(path=str(root / "card.png"))
        browser.close()
    print(root / "card.png")


if __name__ == "__main__":
    main(sys.argv[1])
