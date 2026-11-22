# 2. Per-user API keys

Status: Accepted. The pause scope in §5 is narrowed by [ADR 0008](0008-preset-prompts-compile-to-recipes.md).

## Context

Curio is multi-user. TMDB and Gemini calls cost quota or money. A single operator-owned key would put every user's usage on the operator and make one user's heavy use affect everyone.

Generation runs in background workers, often with no user request involved (low-pool regeneration, later scheduled refresh), so workers need access to a user's keys while the user is away. Stremio addons that use debrid keys avoid storing them because every use is triggered by a Stremio request; Curio's LLM work isn't, so that pattern doesn't apply.

Any design that lets the server use a key unattended also lets whoever operates the server use it. Encryption protects keys from outside attackers, leaked database backups, and a compromised API process, not from the operator.

## Decision

Each user supplies their own TMDB key and LLM API key (supporting Gemini, OpenAI, Anthropic, and OpenRouter). Keys are stored, encrypted, and used by workers for background runs.

1. **Public-key encryption split by process.** Keys are encrypted with a sealed box (libsodium via PyNaCl). API processes hold only the public key, so they can encrypt and store a key but never read one. Only worker processes hold the private key. A database leak or a compromised API process exposes ciphertext only.
2. **Test before storing.** When a user submits a key, the API makes one test call to the provider, encrypts the key, stores it, and discards the plaintext. The last 4 characters are stored separately for masked display. The API never returns a key.
3. **Just-in-time decryption.** Workers get keys only through one key-access function (`get_llm_key(user, job)` and its TMDB equivalent), called per job. The plaintext goes straight into the HTTP client and is never written anywhere. In particular, it never appears in:
   - job rows, run records, or traces;
   - logs (auth headers are redacted in HTTP client logging);
   - LangGraph state or config, because the Postgres checkpointer persists state. Tools receive clients through closures or dependency injection.
4. **Decrypt audit log.** Every decryption is recorded with user, key type, job, and reason, and is visible to the user next to the token counts of the runs it served.
5. **Usage controls.** A per-user monthly token budget, enforced from recorded run token counts. On a provider 401 or 429, background runs for that user pause and the reason is shown in the UI.
6. **Guidance and notice.** The Settings tab tells users to create a dedicated Gemini key restricted to the Gemini API with a quota cap in their Google Cloud project, and states plainly that the server operator can technically access stored keys. The repo is public, so users who don't want that can self-host.
7. **Key management.** The worker's private key comes from the environment. A documented rotation command re-encrypts every stored key under a new keypair. A secrets manager or cloud KMS can replace the environment variable later without code changes.

## Alternatives considered

- **Key in the addon URL** (as Torrentio-style debrid addons do): only works when every use is request-triggered, and makes a shared URL a leaked, billable key.
- **Session-only keys** (supplied from the browser per manual run, attached to one job, deleted after): limits operator access to the length of a job, at the cost of a second key path, job-scoped key storage and cleanup, browser-held keys, and baseline-only auto-refresh for those users. **Deferred.** Revisit when real signup is built or when someone outside the operator's circle uses an instance. The single key-access function exists so this can be added as a second branch plus one migration.
- **Never storing Gemini keys** (background always uses the no-LLM baseline): same mechanism as session-only, and removes LLM auto-refresh for everyone.
- **Operator-owned keys with per-user quotas**: rejected; usage and cost should belong to the user.

## Consequences

- Usage and cost are attributed to the user who causes them, and background regeneration can use the agent.
- Users must trust the operator. The notice, audit log, and restricted, capped keys make that trust visible and limit the damage of misuse.
- The worker host is the sensitive component: compromising it exposes every stored key. Losing the private key makes stored keys unreadable, so users would have to re-enter them.
- A user without valid keys cannot generate catalogs.
