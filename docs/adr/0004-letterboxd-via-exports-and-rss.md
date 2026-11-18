# 4. Letterboxd via exports and RSS

Status: Accepted

## Context

Letterboxd is the main taste input. Its official API is application-only, and the API access page says Letterboxd is not granting access for recommendation, LLM, or personal projects. The terms of use prohibit scrapers and exporting other members' content.

## Decision

Get Letterboxd data only from:
- export ZIPs that users download from their own account and upload (profile and list CSVs), and
- public diary RSS feeds, which include TMDB IDs.

No scraping and no unofficial API use.

## Consequences

- Curio stays within Letterboxd's terms.
- Full history arrives only when a user uploads a fresh export; RSS covers recent diary entries in between.
- TMDB IDs in the RSS make matching to TMDB metadata direct. CSV rows need title/year matching.
- Friends' taste is available only through their public diary RSS.
