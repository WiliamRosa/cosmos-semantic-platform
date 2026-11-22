"""Generate the Cosmos Semantic logo set: SVG sources and PNG exports.

The mark keeps the idea of the original avatar (marketing/linkedin/brand): a sky-blue planet,
a ring tilted -24 degrees and a small moon. For print and large formats the ring now passes
behind the planet at the top and in front of it at the bottom, with a thin gap where it crosses,
and the wordmark is Inter converted to outlines, so no file depends on an installed font.

Usage (Inter TTFs named inter-500.ttf and inter-800.ttf in FONT_DIR; PNGs need Google Chrome):

    uv run --no-project --with uharfbuzz --with fonttools --with playwright \
        python marketing/brand/build_logo.py FONT_DIR
"""

from __future__ import annotations

import base64
import math
import sys
from pathlib import Path
from xml.etree import ElementTree

import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

OUT = Path(__file__).resolve().parent

NAVY = "#0b1220"
INK = "#f8fafc"
SKY = "#38bdf8"
SKY_DEEP = "#0284c7"
GREEN = "#34d399"
GREEN_DEEP = "#059669"
SLATE = "#0f172a"

# Mark geometry, around the centre (16, 16).
TILT = -24  # ring tilt, degrees
RING_RX, RING_RY, RING_W = 11.4, 3.5, 1.5
PLANET_R = 6.0
GAP = 0.6  # clear space around the ring where it crosses the planet, and around the moon
MOON_T = -20  # moon position along the ring, degrees
MOON_R = 1.5

THEMES = {
    # name: (planet, ring, moon, "Cosmos", "Semantic")
    "dark": (SKY, INK, GREEN, INK, INK),       # on dark backgrounds
    "light": (SKY_DEEP, SLATE, GREEN_DEEP, SLATE, SLATE),  # on light backgrounds
    "white": ("#ffffff",) * 5,                  # one colour, on photos or dark colour
    "black": ("#000000",) * 5,                  # one colour, for print and stamps
}


def ring_point(t_deg: float) -> tuple[float, float]:
    t, a = math.radians(t_deg), math.radians(TILT)
    x, y = RING_RX * math.cos(t), RING_RY * math.sin(t)
    return 16 + x * math.cos(a) - y * math.sin(a), 16 + x * math.sin(a) + y * math.cos(a)


def mark_bbox() -> tuple[float, float, float, float]:
    """Visual bounds of the mark (x0, y0, x1, y1), ring stroke and moon included."""
    pts = [ring_point(t / 4) for t in range(0, 1440)]
    half = RING_W / 2
    xs = [x for x, _ in pts]
    ys = [y for _, y in pts]
    mx, my = ring_point(MOON_T)
    x0 = min(min(xs) - half, 16 - PLANET_R, mx - MOON_R)
    x1 = max(max(xs) + half, 16 + PLANET_R, mx + MOON_R)
    y0 = min(min(ys) - half, 16 - PLANET_R, my - MOON_R)
    y1 = max(max(ys) + half, 16 + PLANET_R, my + MOON_R)
    return x0, y0, x1, y1


def mark(theme: str, uid: str) -> str:
    """The mark as SVG elements in user units around (16, 16), no <svg> wrapper."""
    planet, ring, moon, *_ = THEMES[theme]
    mx, my = ring_point(MOON_T)

    def ellipse(stroke: str, width: float = RING_W) -> str:
        return (
            f'<ellipse cx="16" cy="16" rx="{RING_RX}" ry="{RING_RY}" fill="none" stroke="{stroke}" '
            f'stroke-width="{width:.2f}" transform="rotate({TILT} 16 16)"/>'
        )

    full = 'x="-8" y="-8" width="48" height="48"'
    return f"""
  <defs>
    <clipPath id="front-{uid}"><rect x="-8" y="16" width="48" height="24" transform="rotate({TILT} 16 16)"/></clipPath>
    <mask id="planet-{uid}" maskUnits="userSpaceOnUse" {full}>
      <rect {full} fill="#fff"/>
      <g clip-path="url(#front-{uid})">{ellipse("#000", RING_W + 2 * GAP)}</g>
    </mask>
    <mask id="moon-{uid}" maskUnits="userSpaceOnUse" {full}>
      <rect {full} fill="#fff"/>
      <circle cx="{mx:.3f}" cy="{my:.3f}" r="{MOON_R + GAP:.2f}" fill="#000"/>
    </mask>
  </defs>
  <g mask="url(#moon-{uid})">{ellipse(ring)}</g>
  <circle cx="16" cy="16" r="{PLANET_R}" fill="{planet}" mask="url(#planet-{uid})"/>
  <g clip-path="url(#front-{uid})"><g mask="url(#moon-{uid})">{ellipse(ring)}</g></g>
  <circle cx="{mx:.3f}" cy="{my:.3f}" r="{MOON_R}" fill="{moon}"/>"""


def placed_mark(theme: str, uid: str, x: float, y: float, height: float) -> tuple[str, float]:
    """The mark scaled so its visual height is `height`, top-left of its bounds at (x, y)."""
    x0, y0, x1, y1 = mark_bbox()
    s = height / (y1 - y0)
    body = f'\n  <g transform="translate({x - x0 * s:.3f} {y - y0 * s:.3f}) scale({s:.5f})">{mark(theme, uid)}\n  </g>'
    return body, (x1 - x0) * s


class Outliner:
    def __init__(self, font_dir: Path):
        self.fonts = {}
        for weight in (500, 800):
            path = font_dir / f"inter-{weight}.ttf"
            self.fonts[weight] = (hb.Font(hb.Face(hb.Blob.from_file_path(str(path)))), TTFont(path))

    def text(self, text: str, weight: int, size: float, x: float, baseline: float, tracking: float = 0.0):
        """SVG path data for the text, shaped with kerning, and its advance width."""
        hb_font, tt = self.fonts[weight]
        upem = tt["head"].unitsPerEm
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(hb_font, buf, {"kern": True, "liga": True})
        glyphs, order = tt.getGlyphSet(), tt.getGlyphOrder()
        scale = size / upem
        pen = SVGPathPen(glyphs)
        cursor = 0.0
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            gx = x + (cursor + pos.x_offset) * scale
            glyphs[order[info.codepoint]].draw(TransformPen(pen, (scale, 0, 0, -scale, gx, baseline)))
            cursor += pos.x_advance + tracking * upem
        return pen.getCommands(), (cursor - tracking * upem) * scale

    def cap_height(self, size: float) -> float:
        tt = self.fonts[800][1]
        return tt["OS/2"].sCapHeight / tt["head"].unitsPerEm * size


def wordmark(o: Outliner, theme: str, x: float, baseline: float, size: float) -> tuple[str, float]:
    first, second = THEMES[theme][3], THEMES[theme][4]
    a, wa = o.text("Cosmos", 800, size, x, baseline, tracking=-0.025)
    space = size * 0.24
    b, wb = o.text("Semantic", 500, size, x + wa + space, baseline, tracking=-0.015)
    return f'\n  <path fill="{first}" d="{a}"/>\n  <path fill="{second}" d="{b}"/>', wa + space + wb


def svg(width: float, height: float, body: str, background: str | None = None) -> str:
    bg = f'\n  <rect width="{width:.2f}" height="{height:.2f}" fill="{background}"/>' if background else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.2f} {height:.2f}" '
        f'width="{width:.2f}" height="{height:.2f}" role="img" aria-label="Cosmos Semantic">'
        f"\n  <title>Cosmos Semantic</title>{bg}{body}\n</svg>\n"
    )


def build(o: Outliner) -> list[Path]:
    written: list[Path] = []

    def write(name: str, content: str) -> None:
        ElementTree.fromstring(content)  # fail fast on invalid XML
        path = OUT / name
        path.write_text(content, encoding="utf-8")
        written.append(path)

    x0, y0, x1, y1 = mark_bbox()
    aspect = (x1 - x0) / (y1 - y0)
    size = 100.0
    cap = o.cap_height(size)

    for theme in THEMES:
        # Mark only, on a square canvas with a margin of 12% of the mark's width.
        side = 100.0
        mw = side / 1.24
        mh = mw / aspect
        body, _ = placed_mark(theme, "m", (side - mw) / 2, (side - mh) / 2, mh)
        write(f"cosmos-semantic-mark-{theme}.svg", svg(side, side, body))

        # Horizontal lockup: the mark is 2.4 cap heights tall, centred on the cap height.
        pad = cap * 0.5
        mh = cap * 2.4
        body_m, mw = placed_mark(theme, "h", pad, pad, mh)
        text_x = pad + mw + cap * 0.6
        baseline = pad + mh / 2 + cap / 2
        body_t, tw = wordmark(o, theme, text_x, baseline, size)
        write(f"cosmos-semantic-horizontal-{theme}.svg", svg(text_x + tw + pad, mh + 2 * pad, body_m + body_t))

        # Stacked lockup: the mark above the wordmark, both centred.
        _, tw = wordmark(o, theme, 0, 0, size)
        pad = cap * 0.6
        mh = cap * 3.4
        mw = mh * aspect
        width = max(tw, mw) + 2 * pad
        body_m, _ = placed_mark(theme, "s", (width - mw) / 2, pad, mh)
        baseline = pad + mh + cap * 0.8 + cap
        body_t, _ = wordmark(o, theme, (width - tw) / 2, baseline, size)
        write(f"cosmos-semantic-stacked-{theme}.svg", svg(width, baseline + pad * 1.2, body_m + body_t))

    # App icon and social avatar: the dark mark on a navy rounded square.
    side = 100.0
    mw = side * 0.72
    mh = mw / aspect
    body, _ = placed_mark("dark", "i", (side - mw) / 2, (side - mh) / 2, mh)
    write("cosmos-semantic-icon.svg", svg(side, side, f'\n  <rect width="100" height="100" rx="22" fill="{NAVY}"/>' + body))
    write("cosmos-semantic-avatar.svg", svg(side, side, body, background=NAVY))
    return written


def export_pngs(svgs: list[Path]) -> None:
    from playwright.sync_api import sync_playwright

    png_dir = OUT / "png"
    png_dir.mkdir(exist_ok=True)
    for old in png_dir.glob("*.png"):
        old.unlink()
    jobs = []
    for path in svgs:
        if any(k in path.stem for k in ("-mark-", "-icon", "-avatar")):
            jobs += [(path, f"{path.stem}-1024.png", 1024), (path, f"{path.stem}-2048.png", 2048)]
        else:
            jobs += [(path, f"{path.stem}-2000w.png", 2000), (path, f"{path.stem}-4000w.png", 4000)]
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page()
        for path, name, width in jobs:
            text = path.read_text(encoding="utf-8")
            vb = [float(v) for v in text.split('viewBox="')[1].split('"')[0].split()]
            height = round(width * vb[3] / vb[2])
            page.set_viewport_size({"width": width, "height": height})
            data = base64.b64encode(text.encode()).decode()
            page.set_content(
                '<html><body style="margin:0;background:transparent">'
                f'<img src="data:image/svg+xml;base64,{data}" style="width:{width}px;height:{height}px;display:block">'
                "</body></html>"
            )
            page.wait_for_timeout(50)
            page.screenshot(path=str(png_dir / name), omit_background=True)
        browser.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    files = build(Outliner(Path(sys.argv[1])))
    export_pngs(files)
    print(f"wrote {len(files)} SVGs and their PNGs to marketing/brand")
