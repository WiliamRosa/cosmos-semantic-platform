# Website

The project website, served at [cosmosdata.ai](https://cosmosdata.ai/) by the Cloudflare Worker
`cosmos-semantic-site` (configured in `wrangler.jsonc`, which also attaches the domain). One static
page, no framework and no build dependencies.

- `src/` holds the page, styles, script and the files Cloudflare reads (`_headers`, `404.html`).
- `build.py` copies `src/` to `dist/` and adds the media and logos the repository already keeps:
  the logo from `marketing/brand/`, the social preview from `docs/assets/`, the console
  screenshots and video from `media/`, and the slides and walkthrough of the published LinkedIn
  posts. Media is never committed twice.
- `media/capture.py` recaptures the console screenshots and video, the README GIF
  (`docs/assets/console-tour.gif`) and the social preview (`docs/assets/social-preview.png`,
  from `media/social-preview.html`) from the live demo. Run it after console changes deploy.

```bash
python site/build.py
cd site && npx wrangler dev      # page + contact form; emails are written to .wrangler/tmp/email
```

## Contact form

`worker/index.js` serves the assets, redirects `www` to the apex, and handles `POST /api/contact`:
it validates the form (JSON from the page script, or a plain form post without JavaScript) and mails
it to `CONTACT_TO` through the Cloudflare Email Routing binding, with the visitor's address as
Reply-To. Spam guards: a hidden honeypot field, a minimum of three seconds on the page, a
same-origin check and size limits. Nothing is stored. Email Routing must stay enabled on
cosmosdata.ai. Cloudflare sends only to a *verified destination address* of Email Routing (the
mailbox that administrator@cosmosdata.ai forwards to), so `CONTACT_TO` comes from the repository
secret of the same name, passed by the workflow with `wrangler deploy --var`; the address is never
committed. Failures are logged (Workers observability). For `wrangler dev`, put
`CONTACT_TO=you@example.com` in `site/.dev.vars`.

The `site` workflow builds on every pull request that touches the site and deploys on `main`.
It needs the repository secrets `CLOUDFLARE_API_TOKEN` (template "Edit Cloudflare Workers") and
`CLOUDFLARE_ACCOUNT_ID`.

Write the page like the README: say what the code does today, link the source, and use only
numbers the test suite or a published post backs. When a LinkedIn post becomes news worth keeping,
add its card to the "News" section with media from its post folder.
