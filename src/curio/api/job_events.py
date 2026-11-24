"""One shared Postgres listener per API process, fanned out to SSE subscribers.

Workers `NOTIFY job_events` with `{"job_id", "event_id", "status"}` after committing a run
event (see `repositories.jobs.notify_job_event`). The hub holds a single connection that
`LISTEN`s on that channel and wakes the subscribers watching the job. A wake-up carries no data
the subscriber relies on: it re-reads `run_events` from its last seen id. So if the connection
drops, the hub reconnects with backoff and wakes everyone, and nothing is lost.
"""

import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import psycopg
from sqlalchemy.ext.asyncio import AsyncEngine

from curio.repositories.jobs import JOB_EVENTS_CHANNEL

log = logging.getLogger(__name__)

# Wake-ups are coalesced: a subscriber holds at most one pending wake-up.
Wakeups = asyncio.Queue[dict[str, Any]]


def listen_dsn(engine: AsyncEngine) -> str:
    """Build a libpq DSN for the engine's database (for connections outside the pool)."""
    return engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


class TooManyStreamsError(Exception):
    """Raised when a user already has the maximum number of open job streams."""


class JobEventHub:
    def __init__(
        self,
        dsn: str,
        *,
        reconnect_min_seconds: float = 0.5,
        reconnect_max_seconds: float = 30.0,
    ) -> None:
        self._dsn = dsn
        self._reconnect_min = reconnect_min_seconds
        self._reconnect_max = reconnect_max_seconds
        self._subscribers: dict[UUID, set[Wakeups]] = defaultdict(set)
        self._streams_by_user: dict[UUID, int] = defaultdict(int)
        self._task: asyncio.Task[None] | None = None
        self._listening = asyncio.Event()

    async def start(self) -> None:
        """Begin listening in the background. Does not wait for the first connection."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="job-event-hub")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._listening.clear()

    async def wait_listening(self) -> None:
        """Wait until the connection is LISTENing. Callers bound it with `asyncio.timeout`."""
        await self._listening.wait()

    @asynccontextmanager
    async def subscribe(self, job_id: UUID) -> AsyncIterator[Wakeups]:
        """Register for wake-ups on `job_id`. Subscribe before the first read of the job."""
        queue: Wakeups = asyncio.Queue(maxsize=1)
        self._subscribers[job_id].add(queue)
        try:
            yield queue
        finally:
            subscribers = self._subscribers[job_id]
            subscribers.discard(queue)
            if not subscribers:
                del self._subscribers[job_id]

    def acquire_stream(self, user_id: UUID, limit: int) -> None:
        """Count an open stream for `user_id`, or raise `TooManyStreamsError` at `limit`."""
        if self._streams_by_user[user_id] >= limit:
            raise TooManyStreamsError(f"user {user_id} has {limit} open job streams")
        self._streams_by_user[user_id] += 1

    def release_stream(self, user_id: UUID) -> None:
        remaining = self._streams_by_user[user_id] - 1
        if remaining > 0:
            self._streams_by_user[user_id] = remaining
        else:
            self._streams_by_user.pop(user_id, None)

    def _wake(self, queue: Wakeups, message: dict[str, Any]) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(message)

    def _wake_all(self) -> None:
        for queues in self._subscribers.values():
            for queue in queues:
                self._wake(queue, {})

    def _dispatch(self, raw_payload: str) -> None:
        try:
            message = json.loads(raw_payload)
            job_id = UUID(message["job_id"])
        except (ValueError, KeyError, TypeError):
            log.warning("ignoring malformed job_events payload: %.200s", raw_payload)
            return
        for queue in self._subscribers.get(job_id, ()):
            self._wake(queue, message)

    async def _run(self) -> None:
        backoff = self._reconnect_min
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._dsn,
                    autocommit=True,
                    # Notice a silently dead connection instead of waiting on it forever.
                    keepalives=1,
                    keepalives_idle=30,
                    keepalives_interval=10,
                    keepalives_count=3,
                ) as conn:
                    await conn.execute(f"LISTEN {JOB_EVENTS_CHANNEL}")
                    backoff = self._reconnect_min
                    self._listening.set()
                    # Events committed while the connection was down produced no wake-up.
                    self._wake_all()
                    async for notification in conn.notifies():
                        self._dispatch(notification.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("job event listener lost its connection: %s; retrying", exc)
            self._listening.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._reconnect_max)
