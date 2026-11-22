"""Render the LinkedIn page logo and cover from brand.html.

Usage: python marketing/linkedin/brand/render_brand.py

Writes logo-400.png (400x400) and banner-1128x191.png (rendered at 2x, 2256x382) next to this
file. The logo itself comes from marketing/brand. Needs `pip install playwright` and Chrome.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent


def render(browser, scale: int, selector: str, name: str) -> None:
    page = browser.new_page(device_scale_factor=scale)
    page.goto((HERE / "brand.html").as_uri())
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")
    page.locator(selector).screenshot(path=str(HERE / name))
    page.close()


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        render(browser, 1, "#logo", "logo-400.png")
        render(browser, 2, "#banner", "banner-1128x191.png")
        browser.close()
    print("wrote logo-400.png and banner-1128x191.png")


if __name__ == "__main__":
    main()
