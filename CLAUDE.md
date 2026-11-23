## Static analysis

Ruff (lint + format) and Pyright (type checking) are installed in the `dev` dependency group. Run them straight from the venv:

- `.venv/bin/ruff check <paths>` / `.venv/bin/ruff format <paths>`
- `.venv/bin/pyright <paths>`

Don't use `uv run` for these. It syncs `.venv` to the default groups only and uninstalls the extras (torch, terratorch, etc.).

## Explainers

Features are documented by **explainers** in `docs/explainers/`. An explainer is a short doc a human reads to understand how the feature works and to critique its design. It leads with the core idea, then the details that matter, then weak spots.

When a change alters how a feature behaves, update its explainer in the same change. When you substantially change a feature that has none, write one. Read `docs/explainers/README.md` for the outline and rules before writing or editing one, and run the `pstack:unslop` skill over the explainer before you finish.

## Agent skills

### Issue tracker

Issues live in Linear (team TheWay / `KEL`, project GeoSamRoad / `P-KEL-1`), accessed via the Linear MCP tools. See `docs/agents/issue-tracker.md`.

### Triage labels

Default canonical labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`) on the KEL team. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
