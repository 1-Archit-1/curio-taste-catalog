# AGENTS.md

Curio is a self-hosted, multi-user Stremio/Nuvio addon that serves AI-curated movie and series catalog rows built from each user's taste (Letterboxd, Simkl, friends' feeds). Work is tracked in GitHub milestones and issues; design decisions live in `docs/architecture.md` and `docs/adr/`.

## Agent skills

### Issue tracker

Issues live in GitHub Issues for `1-Archit-1/curio-taste-catalog`, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default triage labels, plus `agent-assisted` for human-driven work paired with an agent. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
