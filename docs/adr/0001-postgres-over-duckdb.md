# 1. Postgres over DuckDB

Status: Accepted

## Context

The prototype stored everything in DuckDB. DuckDB allows only one process to open a database for writing. Curio needs several API worker processes plus background workers, all writing.

## Decision

Use Postgres 16 with the pgvector extension as the only datastore. Access it asynchronously with psycopg 3 and `psycopg_pool.AsyncConnectionPool`, using raw SQL in repositories and no ORM.

## Consequences

- Any number of API and worker processes can read and write concurrently.
- Postgres also covers needs that would otherwise require extra services: pgvector for taste embeddings, the LangGraph Postgres checkpointer for agent runs, `LISTEN/NOTIFY` for cross-process progress events, and a job queue with `SELECT ... FOR UPDATE SKIP LOCKED` (no Redis).
- Development and tests need a running Postgres (Docker Compose locally, testcontainers or a CI service in tests).
- The DuckDB prototype is discarded; nothing carries over.
