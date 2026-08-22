# Deploying Agent Game Review

Two things get hosted, and they are independent:

| What | Lives in | Kind of host | File(s) |
|------|----------|--------------|---------|
| **Marketing landing page** | `site/index.html` | Any static host | `site/` |
| **Live evidence-browser demo** | `agr serve` (FastAPI + SPA) | Any Python/Docker host | `Dockerfile`, `render.yaml` |

The landing page's "Live demo" buttons point at `https://demo.your-domain.com` — a
placeholder. Once the app is deployed (below), replace that string in
`site/index.html` (three occurrences) with the real URL.

---

## 1. The landing page (static)

`site/index.html` is a single self-contained file — no build step, no backend.
Pick whichever host owns your domain.

### Option A — Vercel
```bash
npm i -g vercel
cd site
vercel deploy --prod      # follow the prompts; set the output dir to "."
```
Then add your domain under **Project → Settings → Domains** and point your DNS
(`CNAME` → `cname.vercel-dns.com`, or the apex `A` record Vercel shows you).

### Option B — Netlify
```bash
npm i -g netlify-cli
cd site
netlify deploy --prod --dir .
```
Add the domain under **Site settings → Domain management**.

### Option C — GitHub Pages (free, same repo)
1. Repo **Settings → Pages**.
2. Source: **Deploy from a branch**, pick your branch, folder **`/site`**
   (Pages can serve from `/` or `/docs`; if it insists, rename `site` → `docs`).
3. Add your custom domain in that same screen and set the DNS record it prints
   (`CNAME` → `<user>.github.io`).

All three serve the one file at `/` with zero config.

---

## 2. The live app (Python / Docker)

`agr serve` is a FastAPI app that also serves the browser SPA. The included
`Dockerfile` bakes the read-only **synthetic demo store** into the image at build
time (`agr --store /app/.agr-demo demo-store`) and serves it — so the deployed
site has data on first boot with no credentials and no model calls.

The server reads `$PORT` (the port the platform assigns) and binds `0.0.0.0`
automatically via `AGR_HOST`, both set in the image.

### Option A — Render (simplest; blueprint included)
1. Push this repo to GitHub.
2. Render dashboard → **New + → Blueprint** → pick this repo. It reads
   `render.yaml`, builds the `Dockerfile`, and deploys a free web service with a
   health check on `/healthz`.
3. **Settings → Custom Domains** → add `demo.your-domain.com` and set the DNS
   `CNAME` Render shows you.

### Option B — Fly.io
```bash
fly launch --no-deploy      # detects the Dockerfile; edit the generated fly.toml
fly deploy
fly certs add demo.your-domain.com
```
Fly injects `$PORT` (default 8080) — the image already honours it.

### Option C — Railway
1. **New Project → Deploy from GitHub repo.** Railway auto-detects the
   `Dockerfile`.
2. It sets `$PORT` for you; nothing else to configure.
3. **Settings → Networking → Custom Domain** → add your subdomain + DNS `CNAME`.

### Run it locally the same way the container does
```bash
docker build -t agr-demo .
docker run -p 8000:8000 agr-demo      # open http://localhost:8000
```

---

## Notes

- **The public demo is intentionally synthetic.** It serves the
  `source_type=synthetic_demo` store — safe to expose, nothing real in it. To
  demo real runs, ingest them into a store and point `--store` at it (e.g. mount
  a volume and `agr --store /data/store ingest-pi …`).
- **The model reviewer is not wired into the public demo** and needs no key to
  run. If you ever want it server-side, add `ANTHROPIC_API_KEY` (or
  `OPENAI_API_KEY`) as a host secret — never bake it into the image.
- **Custom domain, both halves:** a common split is `your-domain.com` → landing
  page, `demo.your-domain.com` → the app. Set the two DNS records accordingly and
  update the demo URL in `site/index.html`.
