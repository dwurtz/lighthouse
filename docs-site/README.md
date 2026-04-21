# Deja — How It Works (docs site)

A static teaching site for Deja, built with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/). Separate from the repo's raw technical doc (`docs/ARCHITECTURE.md`) and from the marketing page (`site/`). Source of truth for this site is the current `docs/ARCHITECTURE.md`; the pages here are a narrative teaching layer on top.

## Local development

This site has its own dependencies. Keep them out of the main Deja venv.

```bash
cd docs-site
python -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/mkdocs serve     # http://127.0.0.1:8000
```

Edit any Markdown file under `docs/` and the browser hot-reloads.

## Building static HTML

```bash
./venv/bin/mkdocs build     # output → docs-site/site/
```

The `docs-site/site/` directory is ignored at the repo root. Never commit it.

## Deployment

- **GitHub Pages**: `./venv/bin/mkdocs gh-deploy` pushes the built site to a `gh-pages` branch. Hook it to a workflow if you want it automated.
- **Render / Netlify / Vercel**: point a static-site service at this folder with build command `pip install -r requirements.txt && mkdocs build` and publish directory `site/`.

## What's on it

Nine pages grouped into four sections:

1. Intro — what Deja is, design commitments, quickstart.
2. Architecture — pipelines, the wiki, chief of staff, signals, MCP tools.
3. In practice — a narrative day in the life.
4. Principles — why the system is shaped the way it is.

If the architecture shifts, update `docs/ARCHITECTURE.md` first, then reflect the change here in prose. Don't let the two drift.
