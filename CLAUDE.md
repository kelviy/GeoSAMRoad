## Static analysis

Ruff (lint + format) and Pyright (type checking) are installed in the `dev` dependency group. Run them straight from the venv:

- `.venv/bin/ruff check <paths>` / `.venv/bin/ruff format <paths>`
- `.venv/bin/pyright <paths>`

Don't use `uv run` for these. It syncs `.venv` to the default groups only and uninstalls the extras (torch, terratorch, etc.).

## Explainers

Features are documented by **explainers** in `docs/explainers/`. An explainer is a short doc a human reads to understand how the feature works and to critique its design. It leads with the core idea, then the details that matter, then weak spots.

When a change alters how a feature behaves, update its explainer in the same change. When you substantially change a feature that has none, write one. Read `docs/explainers/README.md` for the outline and rules before writing or editing one, and run the `pstack:unslop` skill over the explainer before you finish.

## Docs site

`docs/` is published to <https://kelviy.github.io/GeoSAMRoad/> with Zensical, which reads `mkdocs.yml`. Every push to `semi-automation` builds and deploys it. Guides in `docs/guides/` tell teammates how to do the work. Explainers say how it works.

When a change alters a command, file or workflow that a guide describes, update the guide in the same change. A new page goes in the `nav` of `mkdocs.yml`.

Before finishing any docs change, run `.venv/bin/python tools/build_docs.py`, the same check CI runs. It fails on broken links and anchors, and on local paths, Google Drive or Docs links and credentials. The site is public. Write `<repo>` or `<data-dir>` for paths, "Ask kelviy for the link" for shared links, and use no real names except `kelviy`. Install the tools with `uv pip install -p .venv/bin/python zensical`, never `uv sync`.

## Agent skills

### Issue tracker

Issues live in Linear (team TheWay / `KEL`, project GeoSamRoad / `P-KEL-1`), accessed via the Linear MCP tools. See `docs/agents/issue-tracker.md`.

### Triage labels

Default canonical labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`) on the KEL team. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
