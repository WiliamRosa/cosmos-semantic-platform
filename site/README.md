# Website

The project website, served at [cosmosdata.ai](https://cosmosdata.ai/) from Cloudflare Pages
(project `cosmos-semantic-site`). One static page, no framework and no build dependencies.

- `src/` holds the page, styles, script and the files Cloudflare Pages reads (`_headers`).
- `build.py` copies `src/` to `dist/` and adds the media the repository already keeps: the social
  preview from `docs/assets/`, and the screenshots, slides and videos of the published LinkedIn
  posts in `marketing/linkedin/posts/`. Media is never committed twice.

```bash
python site/build.py
python -m http.server 8000 --directory site/dist
```

The `site` workflow builds on every pull request that touches the site and deploys on `main`.
It needs the repository secrets `CLOUDFLARE_API_TOKEN` (Cloudflare Pages: Edit) and
`CLOUDFLARE_ACCOUNT_ID`.

Write the page like the README: say what the code does today, link the source, and use only
numbers the test suite or a published post backs. When a LinkedIn post becomes news worth keeping,
add its card to the "News" section with media from its post folder.
