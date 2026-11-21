# Website

The project website, served at [cosmosdata.ai](https://cosmosdata.ai/) by the Cloudflare Worker
`cosmos-semantic-site` (static assets only, configured in `wrangler.jsonc`). One static page, no framework and no build dependencies.

- `src/` holds the page, styles, script and the files Cloudflare Pages reads (`_headers`, `404.html`).
- `build.py` copies `src/` to `dist/` and adds the media the repository already keeps: the social
  preview from `docs/assets/`, and the screenshots, slides and videos of the published LinkedIn
  posts in `marketing/linkedin/posts/`. Media is never committed twice.

```bash
python site/build.py
python -m http.server 8000 --directory site/dist
```

The `site` workflow builds on every pull request that touches the site and deploys on `main`.
It needs the repository secrets `CLOUDFLARE_API_TOKEN` (template "Edit Cloudflare Workers") and
`CLOUDFLARE_ACCOUNT_ID`.

Write the page like the README: say what the code does today, link the source, and use only
numbers the test suite or a published post backs. When a LinkedIn post becomes news worth keeping,
add its card to the "News" section with media from its post folder.
