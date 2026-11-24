# 3. Typed sources

Status: Accepted. "Sources are shared across users" is amended by [ADR 0009](0009-source-ownership-and-history-provenance.md): only public sources are shared.

## Context

Taste comes from several inputs: a user's own Letterboxd diary, their profile export, lists, and friends' feeds. They mean different things to different users. A friend's diary is taste signal for one user and simply someone else's history for its owner.

## Decision

Every taste input is a **source** with a **kind**: `lb_diary_rss`, `lb_profile_csv`, or `lb_list_csv`. Each user links sources with a per-user **role**: `self` (my own history), `taste` (someone whose taste I follow), or `candidates` (a pool of titles to recommend from). Sources are shared across users; the role belongs to the user-source link.

## Consequences

- A public feed or list is fetched and stored once, however many users follow it.
- Pipelines select inputs by role, not by where the data came from.
- Deleting a user removes their links, not the shared source data other users still use.
