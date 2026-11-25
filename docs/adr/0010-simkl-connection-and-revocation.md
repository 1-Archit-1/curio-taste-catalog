# 10. Simkl connection, tokens and revocation
Status: Accepted. Builds on [ADR 0002](0002-per-user-api-keys.md) and [ADR 0009](0009-source-ownership-and-history-provenance.md).
## Context
Simkl is the main source of series taste. A user connects their own account with OAuth, and Curio syncs their library, ratings and statuses (completed, watching, dropped).
Three facts from the Simkl docs shape the design:
- **A grant is one access token and one refresh token.** Refreshing replaces the access token and kills the old one at once, so two processes refreshing the same grant keep cutting each other off. The refresh token doesn't rotate. Access tokens last 7 days, refresh tokens 180 days, sliding.
- **Every authorization creates a new, independent grant.** Connecting again doesn't end the earlier one.
- **Revoking either token ends the grant, and `/oauth2/revoke` always answers 200.** There is nothing to check afterwards.
ADR 0002 says only workers can read a stored credential, and the API holds only the public key. That rules out revoking in the API, which can't read the tokens it sealed.
## Decision
### 1. AUTH V2, authorization code with PKCE
The server registers one "Server apps & services" app. The flow uses PKCE (S256), a `state` bound to the user and the browser session, and an `iss` check, and asks for `media:read` only. The granted scope is read from the token response. AUTH V1 retires around April 2027 and isn't used.
`POST /api/simkl/connect` stores a `simkl_oauth_states` row (hash of the state, session hash, PKCE verifier, 10 minute expiry) and returns the consent URL. The callback deletes the row in the statement that reads it, so a state works once whatever happens next, then checks user, session, expiry and `iss` before it believes an `error` or exchanges the code. The code is exchanged once and never retried, because Simkl destroys a code on every failed attempt. The `redirect_uri` is server configuration (`SIMKL_REDIRECT_URI`, or `PUBLIC_BASE_URL` + `/api/simkl/callback`) and is never taken from a request.
### 2. Tokens are sealed and only workers read them
`simkl_connections` holds the sealed access and refresh tokens, one row per user. Workers read them through one audited key-access function, and the audit log accepts the key type `simkl`. `KeyType.SIMKL` is the pause key: a rejected grant (`invalid_grant` on refresh, or a 401 that survives a refresh) sets `needs_reconnect`, pauses the user's queued Simkl jobs and the running one, and the UI asks the user to reconnect. It is not shown in Settings and doesn't set `users.jobs_paused_reason`, because there is no key to replace there.
The sync job is the only refresher. It locks the connection row, refreshes when the access token has a day or less left, and commits the new token at once, since the old one no longer works.
### 3. Simkl is an owned source kind
Kind `simkl`, key `account`, owned by the user. Its items use the item kinds `watched` (Simkl's completed), `watching` and `dropped`; `plantowatch` and `hold` aren't history and are skipped, so a title moved there disappears. Ratings are Simkl's 1-10, stored as they are. History is written only by `resync_watch_history`, which maps the item kinds to history statuses, so ADR 0009's provenance rules apply and dropped series become anti-taste.
Where several Simkl anime entries share one TMDB title (a show split into seasons or parts), one item is kept: `watching` over `watched` over `dropped`, with the rating of the winner or, failing that, of another entry.
### 4. Disconnecting and reconnecting
The API deletes the source and its subscription at once. The items, the history the source wrote and the connection go with it, and the user's history is rebuilt from their other `self` sources. Before that, the sealed refresh token moves to `simkl_revocations`, and a `simkl_revoke` job whose payload is only the row's id revokes it at Simkl and deletes the row. Revoking the refresh token ends the whole grant, so the access token isn't kept. Reconnecting does the same for the grant it replaces, and starts over with a full sync.
A revoke that keeps failing is retried up to `max_attempts`, then its ciphertext is deleted: the grant expires by itself within 180 days, and keeping a token for ever is worse. A user can also remove Curio at simkl.com/settings/connected-apps.
### 5. Syncing
Syncing follows Simkl's two-phase model and runs only when the user asks (on connect and **Sync now**), never on a timer, as the API rules require. `/sync/activities` is read first. The first sync pulls each type in full, and later ones fetch only the buckets that moved, with `date_from` set to the saved snapshot's `all`. Ratings are fetched with `date_from` only when `rated_at` moved. A moved `removed_from_list` triggers an `ids_only` fetch of all three types and a diff, because removals never appear in a delta. The snapshot is saved in the same transaction as the items, so it is never ahead of them.
## Alternatives considered
- **Revoke in the API.** Impossible without the private key.
- **Put the sealed token in the revoke job's payload.** Job rows are shown in the UI and copied around; ADR 0002 keeps credentials out of them.
- **Keep the source until the revoke succeeds.** Leaves the user's Simkl data in Curio after they disconnected.
- **A `user_api_keys` row for Simkl.** The table is for keys the user pastes, with a last-4 display. Tokens need an expiry, a scope and the sync state next to them.
- **Refresh on every API call, or in the API.** The API can't read the tokens, and parallel refreshes invalidate each other.
## Consequences
- The Simkl source can't be removed or given another role through the generic source routes. Disconnect is the only way, so the tokens always reach the revoke job.
- Deleting a Curio user deletes their sealed tokens without revoking them. The grant lapses within 180 days.
- A rotation of the sealed-box keypair re-encrypts Simkl tokens and pending revocations together with the API keys.
- Rows for titles Simkl has without a TMDB ID are skipped and counted in the sync result.
- A delta that changes only one of several Simkl anime entries sharing a TMDB title decides that title from the changed entry alone, until the next full sync (a reconnect).
