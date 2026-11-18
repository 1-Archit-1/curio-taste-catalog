# 2. Per-user API keys

Status: Accepted

## Context

Curio is multi-user. TMDB and Gemini calls cost quota or money. A single operator-owned key would put every user's usage on the operator and make one user's heavy use affect everyone.

## Decision

Each user supplies their own TMDB and Gemini API keys. Keys are encrypted at rest with a server key taken from the environment, and they are write-only through the API: responses only show masked values.

## Consequences

- Usage and cost are attributed to the user who causes them.
- A user without valid keys cannot generate catalogs; the control plane needs a way to test keys.
- The server encryption key becomes critical: losing it makes stored keys unreadable, and leaking it exposes them. Keys must never appear in logs or responses.
