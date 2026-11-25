# 9. Source ownership and watch history provenance

Status: Accepted. Amends [ADR 0003](0003-typed-sources.md).

## Context
ADR 0003 made every source shared and keyed it by `(kind, external_key)`. That is right for a public diary RSS feed, which anyone can fetch. It is wrong for an uploaded export. The key for an export source was the username written inside the uploaded profile file, so a user could upload a file claiming to be someone else and write into that person's source.

Several sources can also write to one user's watch history, because a user may have more than one `self` source. With one row per title and no record of who wrote it, a second source silently overwrote the first, a source could never remove what it had written, and nothing separated automatic rows from manual ones.

Letterboxd stars (0.5 to 5) and Curio's `watch_history` ratings (0 to 10) were stored on different scales in different tables.

## Decision
### 1. Sources can have an owner
`sources.owner_user_id` is null for public kinds (`lb_diary_rss`) and set for export kinds (`lb_profile_csv`, `lb_list_csv`). A check constraint ties the two together. The unique key is `(kind, owner_user_id, external_key)` with `NULLS NOT DISTINCT`, so public sources stay unique by feed and an export source is unique per uploader and file (`profile`, `list:<slug>`). The username inside a file is only a label.

- A public source is fetched once, however many users subscribe, and polled by the worker (`next_refresh_at`).
- An owned source is private. Only its owner can subscribe, list, change, refresh or delete it; anyone else gets 404. Removing it deletes it, with its items, issues and the history it wrote.
- Account integrations that need credentials (Simkl, next) are owned source kinds, for the same reason: their data belongs to one user. Simkl is described in [ADR 0010](0010-simkl-connection-and-revocation.md).

### 2. Watch history records its source
`watch_history.source_id` references the source that wrote the row, with `ON DELETE CASCADE`. Null means manual or another origin.

- A source inserts rows that don't exist and updates or deletes rows it owns. It never changes a manual row or another source's row.
- When a source stops being `self` for a user (role change or unsubscribe), the user's rows it owns are deleted. The user's remaining `self` sources are then written again from their stored items, so another source can take over the titles. One service function, `resync_watch_history`, does this and needs no network. Every change to a source's items or a user's roles calls it.
- Syncing a source makes its owned rows match its current items. Removed titles and removed ratings disappear instead of keeping the old value.
- Within one source, the rating comes from `ratings.csv` first, then the diary entry with the latest `watched_at`, then the watched list.

### 3. One rating scale
Every stored rating uses Curio's 0 to 10 scale. Letterboxd stars are multiplied by 2 where the file or feed is parsed. `source_items`, `import_issues` and `watch_history` all check `0..10`.

## Alternatives considered
- **Keep sources shared, key exports by a hash of the file.** Stops the impersonation, but two uploads of the same person's export would still collide and any user could subscribe to an export by ID.
- **Verify the username in the file.** Letterboxd offers no way to prove who made an export.
- **Last writer wins in `watch_history`.** Simple, but a re-sync of one source would overwrite another source and manual rows, and removals could never be told from rows other sources wrote.
- **Rank sources by priority.** More predictable than first-writer ownership, but needs a UI and a setting nobody has asked for yet.

## Consequences
- Re-uploading an export replaces the user's own source and can never affect another user.
- Which of two `self` sources owns a title depends on which wrote it first. Removing that source hands the title to the other.
- Sharing an export with a friend means the friend uploads it themselves and gets their own copy.
- Deleting a user deletes their owned sources. Public sources they followed stay.
