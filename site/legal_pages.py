"""Render the CosmosProof privacy policy and terms of use as pages of cosmosdata.ai.

Their source is Markdown in the CosmosProof repository (deploy/azure/marketplace), where they are
reviewed with the Marketplace offer. After changing them there, regenerate the pages:

    python site/legal_pages.py ../cosmosproof/deploy/azure/marketplace

Handles only what those files use: an HTML comment header, #/## headings, paragraphs, '- ' lists,
**bold**, `code` and bare https URLs.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "src" / "proof"


def inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)

    def link(m):
        url = m.group(1)
        trail = ""
        while url[-1] in ".,;)":
            trail = url[-1] + trail
            url = url[:-1]
        href = url.replace("https://cosmosdata.ai", "") or "/"
        rel = "" if href.startswith("/") else ' rel="noopener"'
        return f'<a href="{href}"{rel}>{url}</a>{trail}'

    return re.sub(r"(https://[^\s<]+)", link, text)


def render(md: str) -> tuple[str, str]:
    md = re.sub(r"<!--.*?-->\s*", "", md, flags=re.S)
    out, title, para, items = [], "", [], []

    def flush():
        nonlocal para, items
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para = []
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>")
            items = []

    for line in md.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("## "):
            flush()
            out.append(f"<h2>{inline(line[3:].strip())}</h2>")
        elif line.startswith("- "):
            if para:
                flush()
            items.append(line[2:].strip())
        elif not line.strip():
            flush()
        elif items and line.startswith("  "):
            items[-1] += " " + line.strip()
        else:
            para.append(line.strip())
    flush()
    return title, "\n        ".join(out)


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <meta name="description" content="{description}">
  <link rel="canonical" href="https://cosmosdata.ai/proof/{slug}">
  <meta name="theme-color" content="#0b1220">
  <link rel="icon" href="/assets/cosmosproof/icon.svg" type="image/svg+xml">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;800&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/styles.css">
</head>
<body>
  <!-- Generated from deploy/azure/marketplace/{source} in github.com/WiliamRosa/cosmosproof. Change it there first. -->
  <header class="site-header">
    <div class="wrap header-inner">
      <a class="brand" href="/proof/" aria-label="CosmosProof">
        <img class="brand-logo" src="/assets/cosmosproof/logo-horizontal-dark.svg" alt="CosmosProof" width="154" height="36">
      </a>
      <nav class="nav" aria-label="Main">
        <a href="/proof/">CosmosProof</a>
        <a href="/proof/privacy">Privacy</a>
        <a href="/proof/terms">Terms</a>
        <a href="/proof/#contact">Contact</a>
      </nav>
    </div>
  </header>
  <main id="main" class="section">
    <article class="wrap doc">
        <h1>{title}</h1>
        {body}
    </article>
  </main>
  <footer class="site-footer">
    <div class="wrap footer-inner">
      <p class="footer-brand"><img src="/assets/cosmosproof/icon.svg" alt="" width="22" height="22"> © 2026 <a href="/">Cosmos Data</a></p>
      <nav aria-label="Footer">
        <a href="https://github.com/WiliamRosa/cosmosproof" rel="noopener">GitHub</a>
        <a href="/proof/privacy">Privacy</a>
        <a href="/proof/terms">Terms</a>
        <a href="/proof/#contact">Contact</a>
      </nav>
    </div>
  </footer>
</body>
</html>
"""


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python site/legal_pages.py <cosmosproof>/deploy/azure/marketplace")
    folder = Path(sys.argv[1])
    for source, slug, description in PAGES:
        title, body = render((folder / source).read_text(encoding="utf-8"))
        page = PAGE.format(
            title=html.escape(title), description=description, slug=slug, source=source, body=body
        )
        (OUT / f"{slug}.html").write_text(page, encoding="utf-8")
        print(f"wrote site/src/proof/{slug}.html")


PAGES = [
    (
        "privacy-policy.md",
        "privacy",
        "What CosmosProof reads and writes inside your tenant, and what Cosmos Data does and does not receive.",
    ),
    ("terms-of-use.md", "terms", "Terms of use of the CosmosProof offer on the Azure Marketplace."),
]

if __name__ == "__main__":
    main()
