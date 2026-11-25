"""Integration tests for jobs, starter pipeline, pause/resume, SSE, and served_titles."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from curio.api.deps import get_job_event_hub
from curio.api.job_events import JobEventHub
from curio.clients.providers import KeyType
from curio.clients.ratelimit import RateLimiter
from curio.clients.tmdb import BASE_URL as TMDB_BASE_URL
from curio.clients.tmdb import TmdbAuthError, TmdbRateLimitError
from curio.crypto import KeyEncryptor, Keypair
from curio.db.engine import to_sqlalchemy_url
from curio.db.models.catalogs import (
    CatalogCandidate,
    MediaItem,
    ServedTitle,
    Veto,
    WatchHistory,
)
from curio.db.models.jobs import Job, Run
from curio.repositories import jobs as jobs_repo
from curio.services import catalogs as catalogs_service
from curio.services import jobs as jobs_service
from curio.services import keys as keys_service
from curio.services.keys import KeyNotFoundError
from curio.services.users import ProvisionedUser
from curio.settings import Settings


def _worker_settings(database_url: str, keypair: Keypair) -> Settings:
    return Settings(
        _env_file=None,
        database_url=database_url,
        key_public_key=keypair.public_key,
        key_private_key=keypair.private_key,
    )


async def _login_client(client: httpx.AsyncClient, login_token: str) -> None:
    resp = await client.post("/api/auth/login", json={"token": login_token})
    assert resp.status_code == 200


async def _save_committed_tmdb_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    keypair: Keypair,
    api_key: str = "tmdb-secret-key-1234",
) -> None:
    from curio.repositories import keys as keys_repo

    encrypted = KeyEncryptor(keypair.public_key).encrypt(api_key)
    async with session_factory() as s:
        await keys_repo.upsert_api_key(
            s,
            user_id=user_id,
            key_type=KeyType.TMDB.value,
            encrypted_key=encrypted,
            last4=api_key[-4:],
        )
        await s.commit()


async def test_concurrent_process_one_job_skip_locked_runs_once(
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    migrated_database_url: str,
    keypair: Keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned = await make_committed_user("skip_locked_user")
    user_id = provisioned.user.id
    await _save_committed_tmdb_key(audit_session_factory, user_id=user_id, keypair=keypair)

    async with audit_session_factory() as s:
        preset = await catalogs_service.create_preset(
            s, user_id=user_id, name="Concurrent Preset", media_type="movie"
        )
        job = await jobs_service.enqueue_regenerate_job(s, user_id=user_id, preset_id=preset.id)
        await s.commit()

    from curio.services import starter

    call_count = 0

    async def fake_starter(
        session: AsyncSession,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        rate_limiter: RateLimiter,
        job: Job,
        run: Run,
        emit_step: object,
    ) -> None:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)

    monkeypatch.setattr(starter, "run_starter_pipeline", fake_starter)
    settings = _worker_settings(migrated_database_url, keypair)
    limiter = RateLimiter(1000)

    r1, r2 = await asyncio.gather(
        jobs_service.process_one_job(
            audit_session_factory, settings=settings, rate_limiter=limiter, worker_id="w1"
        ),
        jobs_service.process_one_job(
            audit_session_factory, settings=settings, rate_limiter=limiter, worker_id="w2"
        ),
    )

    assert sorted([r1, r2]) == [False, True]
    assert call_count == 1

    async with audit_session_factory() as s:
        db_job = await jobs_repo.get_job(s, user_id=user_id, job_id=job.id)
        assert db_job is not None
        assert db_job.status == "succeeded"


async def test_regenerate_route_status_codes_and_duplicate_conflict(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    audit_session_factory: async_sessionmaker[AsyncSession],
    keypair: Keypair,
) -> None:
    provisioned = await make_committed_user("regen_route_user")
    await _login_client(client, provisioned.login_token)

    # Unknown preset -> 404
    missing_id = UUID("00000000-0000-0000-0000-000000000001")
    resp_404 = await client.post(f"/api/presets/{missing_id}/regenerate")
    assert resp_404.status_code == 404

    # Create preset without TMDB key -> 422
    async with audit_session_factory() as s:
        preset = await catalogs_service.create_preset(
            s, user_id=provisioned.user.id, name="Regen Preset", media_type="movie"
        )
        await s.commit()

    resp_422 = await client.post(f"/api/presets/{preset.id}/regenerate")
    assert resp_422.status_code == 422

    # Save TMDB key -> 202 Accepted, then duplicate -> 409
    await _save_committed_tmdb_key(
        audit_session_factory, user_id=provisioned.user.id, keypair=keypair
    )
    resp_202 = await client.post(f"/api/presets/{preset.id}/regenerate")
    assert resp_202.status_code == 202
    job_payload = resp_202.json()
    assert job_payload["status"] == "queued"
    assert job_payload["preset_id"] == str(preset.id)

    resp_409 = await client.post(f"/api/presets/{preset.id}/regenerate")
    assert resp_409.status_code == 409

    latest_resp = await client.get(f"/api/presets/{preset.id}/jobs/latest")
    assert latest_resp.status_code == 200
    assert latest_resp.json()["job"]["id"] == job_payload["id"]

    status_resp = await client.get(f"/api/jobs/{job_payload['id']}")
    assert status_resp.status_code == 200
    assert status_resp.json()["job"]["id"] == job_payload["id"]


async def test_starter_pipeline_populates_movie_and_series_presets(
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    migrated_database_url: str,
    keypair: Keypair,
    respx_mock: object,
) -> None:
    import respx

    assert isinstance(respx_mock, respx.MockRouter)
    provisioned = await make_committed_user("starter_user")
    user_id = provisioned.user.id
    secret_tmdb_key = "tmdb-plaintext-secret-9999"
    await _save_committed_tmdb_key(
        audit_session_factory, user_id=user_id, keypair=keypair, api_key=secret_tmdb_key
    )

    # Seed watch history & vetoed/excluded items
    async with audit_session_factory() as s:
        movie_preset = await catalogs_service.create_preset(
            s, user_id=user_id, name="Movie Picks", media_type="movie", min_rating=7.0
        )
        series_preset = await catalogs_service.create_preset(
            s, user_id=user_id, name="Series Picks", media_type="series"
        )
        # Seed watched movie (100) and vetoed movie (102)
        s.add_all(
            [
                MediaItem(
                    media_type="movie",
                    tmdb_id=100,
                    imdb_id="tt0000100",
                    name="Seed Movie",
                    rating=8.5,
                ),
                MediaItem(
                    media_type="movie",
                    tmdb_id=102,
                    imdb_id="tt0000102",
                    name="Vetoed Movie",
                    rating=9.0,
                ),
                MediaItem(
                    media_type="series",
                    tmdb_id=200,
                    imdb_id="tt0000200",
                    name="Seed Series",
                    rating=8.5,
                ),
            ]
        )
        await s.flush()
        s.add_all(
            [
                WatchHistory(
                    user_id=user_id,
                    media_type="movie",
                    tmdb_id=100,
                    status="watched",
                    rating=9,
                ),
                Veto(
                    user_id=user_id,
                    media_type="movie",
                    tmdb_id=102,
                    kind="not_for_me",
                ),
                WatchHistory(
                    user_id=user_id,
                    media_type="series",
                    tmdb_id=200,
                    status="watched",
                    rating=9,
                ),
            ]
        )
        movie_job = await jobs_service.enqueue_regenerate_job(
            s, user_id=user_id, preset_id=movie_preset.id
        )
        series_job = await jobs_service.enqueue_regenerate_job(
            s, user_id=user_id, preset_id=series_preset.id
        )
        await s.commit()

    def _movie_detail(tmdb_id: int, title: str, rating: float, votes: int) -> dict:
        return {
            "id": tmdb_id,
            "title": title,
            "overview": f"Overview for {title}",
            "release_date": "2022-01-01",
            "vote_average": rating,
            "vote_count": votes,
            "poster_path": f"/p{tmdb_id}.jpg",
            "genres": [{"id": 18, "name": "Drama"}],
            "external_ids": {"imdb_id": f"tt{tmdb_id:07d}"},
            "keywords": {"keywords": []},
            "credits": {"cast": [], "crew": []},
        }

    def _tv_detail(tmdb_id: int, name: str, rating: float, votes: int) -> dict:
        return {
            "id": tmdb_id,
            "name": name,
            "overview": f"Overview for {name}",
            "first_air_date": "2022-01-01",
            "vote_average": rating,
            "vote_count": votes,
            "poster_path": f"/p{tmdb_id}.jpg",
            "genres": [{"id": 18, "name": "Drama"}],
            "external_ids": {"imdb_id": f"tt{tmdb_id:07d}"},
            "keywords": {"results": []},
            "credits": {"cast": [], "crew": []},
        }

    respx_mock.get(f"{TMDB_BASE_URL}/movie/100/recommendations").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": 101, "title": "Lower Vote", "release_date": "2022-01-01"},
                    {"id": 102, "title": "Vetoed Movie", "release_date": "2022-01-01"},
                    {"id": 103, "title": "Top Pick", "release_date": "2022-01-01"},
                    {"id": 104, "title": "Too Low Rating", "release_date": "2022-01-01"},
                ]
            },
        )
    )
    respx_mock.get(f"{TMDB_BASE_URL}/movie/101").mock(
        return_value=httpx.Response(200, json=_movie_detail(101, "Lower Vote", 8.0, 500))
    )
    respx_mock.get(f"{TMDB_BASE_URL}/movie/102").mock(
        return_value=httpx.Response(200, json=_movie_detail(102, "Vetoed Movie", 9.0, 900))
    )
    respx_mock.get(f"{TMDB_BASE_URL}/movie/103").mock(
        return_value=httpx.Response(200, json=_movie_detail(103, "Top Pick", 8.0, 1200))
    )
    respx_mock.get(f"{TMDB_BASE_URL}/movie/104").mock(
        return_value=httpx.Response(200, json=_movie_detail(104, "Too Low Rating", 6.2, 800))
    )

    respx_mock.get(f"{TMDB_BASE_URL}/tv/200/recommendations").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": 201, "name": "Great Series", "first_air_date": "2023-01-01"}]},
        )
    )
    respx_mock.get(f"{TMDB_BASE_URL}/tv/201").mock(
        return_value=httpx.Response(200, json=_tv_detail(201, "Great Series", 8.7, 300))
    )

    settings = _worker_settings(migrated_database_url, keypair)
    limiter = RateLimiter(1000)

    assert await jobs_service.process_one_job(
        audit_session_factory, settings=settings, rate_limiter=limiter, worker_id="w1"
    )
    assert await jobs_service.process_one_job(
        audit_session_factory, settings=settings, rate_limiter=limiter, worker_id="w1"
    )

    async with audit_session_factory() as s:
        movie_candidates = (
            (
                await s.execute(
                    select(CatalogCandidate)
                    .where(CatalogCandidate.preset_id == movie_preset.id)
                    .order_by(CatalogCandidate.position.asc())
                )
            )
            .scalars()
            .all()
        )
        # 102 vetoed, 104 below min_rating 7.0; 103 has more votes than 101 at same 8.0 rating
        assert [c.tmdb_id for c in movie_candidates] == [103, 101]

        series_candidates = (
            (
                await s.execute(
                    select(CatalogCandidate)
                    .where(CatalogCandidate.preset_id == series_preset.id)
                    .order_by(CatalogCandidate.position.asc())
                )
            )
            .scalars()
            .all()
        )
        assert [c.tmdb_id for c in series_candidates] == [201]

        for j_id in (movie_job.id, series_job.id):
            run = await jobs_repo.get_run_for_job(s, user_id=user_id, job_id=j_id)
            assert run is not None
            assert run.pipeline == "starter"
            assert run.status == "succeeded"
            assert run.latency_ms is not None and run.latency_ms >= 0
            assert secret_tmdb_key not in (run.error or "")

            events = await jobs_repo.list_run_events_for_job(s, user_id=user_id, job_id=j_id)
            assert len(events) >= 2
            for ev in events:
                assert secret_tmdb_key not in ev.message
                assert secret_tmdb_key not in json.dumps(ev.payload or {})


@pytest.mark.parametrize("status_code", [401, 429])
async def test_tmdb_auth_and_rate_limit_pause_only_user_jobs_and_resumes_on_key_update(
    status_code: int,
    client: httpx.AsyncClient,
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    migrated_database_url: str,
    keypair: Keypair,
    respx_mock: object,
) -> None:
    import respx

    assert isinstance(respx_mock, respx.MockRouter)
    user_a = await make_committed_user(f"pause_user_a_{status_code}")
    user_b = await make_committed_user(f"pause_user_b_{status_code}")

    await _save_committed_tmdb_key(audit_session_factory, user_id=user_a.user.id, keypair=keypair)
    await _save_committed_tmdb_key(audit_session_factory, user_id=user_b.user.id, keypair=keypair)

    async with audit_session_factory() as s:
        preset_a = await catalogs_service.create_preset(
            s, user_id=user_a.user.id, name="A Preset", media_type="movie"
        )
        preset_b = await catalogs_service.create_preset(
            s, user_id=user_b.user.id, name="B Preset", media_type="movie"
        )
        s.add(
            MediaItem(
                media_type="movie",
                tmdb_id=300 + status_code,
                imdb_id=f"tt0003{status_code}",
                name="Seed",
                rating=8.0,
            )
        )
        await s.flush()
        s.add(
            WatchHistory(
                user_id=user_a.user.id,
                media_type="movie",
                tmdb_id=300 + status_code,
                status="watched",
                rating=8,
            )
        )
        job_a = await jobs_service.enqueue_regenerate_job(
            s, user_id=user_a.user.id, preset_id=preset_a.id
        )
        job_b = await jobs_service.enqueue_regenerate_job(
            s, user_id=user_b.user.id, preset_id=preset_b.id
        )
        await s.commit()

    respx_mock.get(f"{TMDB_BASE_URL}/movie/{300 + status_code}/recommendations").mock(
        return_value=httpx.Response(status_code, json={"status_message": "error"})
    )

    settings = _worker_settings(migrated_database_url, keypair)
    limiter = RateLimiter(1000)

    assert await jobs_service.process_one_job(
        audit_session_factory, settings=settings, rate_limiter=limiter, worker_id="w1"
    )

    async with audit_session_factory() as s:
        db_job_a = await jobs_repo.get_job(s, user_id=user_a.user.id, job_id=job_a.id)
        db_job_b = await jobs_repo.get_job(s, user_id=user_b.user.id, job_id=job_b.id)
        assert db_job_a is not None and db_job_a.status == "paused"
        assert db_job_a.paused_key_type == "tmdb"
        assert db_job_a.paused_reason
        assert db_job_b is not None and db_job_b.status == "queued"

    # Settings API shows paused_reason on tmdb key
    await _login_client(client, user_a.login_token)
    settings_resp = await client.get("/api/settings")
    assert settings_resp.status_code == 200
    assert settings_resp.json()["keys"]["tmdb"]["paused_reason"] is not None

    # Updating TMDB key via API (mocking validation) resumes paused jobs
    respx_mock.get(f"{TMDB_BASE_URL}/configuration").mock(
        return_value=httpx.Response(200, json={"images": {}})
    )
    put_resp = await client.put(
        "/api/settings/keys/tmdb",
        json={"api_key": "new-valid-tmdb-key-5678"},
    )
    assert put_resp.status_code == 200

    # Check via GET /api/jobs/{job_id} on the same request session
    status_resp = await client.get(f"/api/jobs/{job_a.id}")
    assert status_resp.status_code == 200
    assert status_resp.json()["job"]["status"] == "queued"
    assert status_resp.json()["job"]["paused_reason"] is None


async def test_sse_streams_step_and_terminal_status_via_notify(
    committed_client: httpx.AsyncClient,
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    keypair: Keypair,
) -> None:
    client = committed_client
    provisioned = await make_committed_user("sse_user")
    user_id = provisioned.user.id
    await _save_committed_tmdb_key(audit_session_factory, user_id=user_id, keypair=keypair)

    async with audit_session_factory() as s:
        preset = await catalogs_service.create_preset(
            s, user_id=user_id, name="SSE Preset", media_type="movie"
        )
        job = await jobs_service.enqueue_regenerate_job(s, user_id=user_id, preset_id=preset.id)
        run = await jobs_repo.create_run(
            s,
            user_id=user_id,
            job_id=job.id,
            preset_id=preset.id,
            attempt=1,
            pipeline="starter",
            trigger="manual",
        )
        await s.commit()

    await _login_client(client, provisioned.login_token)

    async def publish_events() -> None:
        await asyncio.sleep(0.15)
        async with audit_session_factory() as s:
            ev = await jobs_repo.append_run_event(
                s,
                run_id=run.id,
                user_id=user_id,
                step="seeds",
                message="Loading seeds",
                payload={"count": 3},
            )
            await jobs_repo.notify_job_event(s, job_id=job.id, event_id=ev.id, status="running")
            await s.commit()

        await asyncio.sleep(0.1)
        await _finish_job_with_event(audit_session_factory, job=job, run=run)

    publisher = asyncio.create_task(publish_events())
    async with client.stream("GET", f"/api/jobs/{job.id}/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        frames = _parse_sse(await resp.aread())
    await publisher

    step_events = [f for f in frames if f["event"] == "step"]
    status_events = [f for f in frames if f["event"] == "status"]
    assert [f["data"]["step"] for f in step_events] == ["seeds", "completed"]
    assert all(f["id"] == str(f["data"]["id"]) for f in step_events)
    assert len(status_events) == 1
    assert status_events[0]["data"]["status"] == "succeeded"


async def test_addon_catalog_records_served_titles_best_effort(
    client: httpx.AsyncClient,
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned = await make_committed_user("served_user")
    user_id = provisioned.user.id
    token = provisioned.user.addon_token

    async with audit_session_factory() as s:
        preset = await catalogs_service.create_preset(
            s, user_id=user_id, name="Served Preset", media_type="movie"
        )
        await catalogs_service.assign_slot(
            s, user_id=user_id, slot_id="curio_movie_1", preset_id=preset.id
        )
        s.add(
            MediaItem(
                media_type="movie",
                tmdb_id=999,
                imdb_id="tt0000999",
                name="Served Movie",
                rating=8.2,
                poster_url="https://image.tmdb.org/t/p/w500/999.jpg",
            )
        )
        await s.flush()
        await catalogs_repo_replace_one(s, user_id=user_id, preset_id=preset.id, tmdb_id=999)
        await s.commit()

    # First and second serve increment serve_count
    r1 = await client.get(f"/{token}/catalog/movie/curio_movie_1.json")
    assert r1.status_code == 200
    assert len(r1.json()["metas"]) == 1

    r2 = await client.get(f"/{token}/catalog/movie/curio_movie_1.json")
    assert r2.status_code == 200

    async with audit_session_factory() as s:
        row = (
            await s.execute(
                select(ServedTitle).where(
                    ServedTitle.user_id == user_id,
                    ServedTitle.preset_id == preset.id,
                    ServedTitle.tmdb_id == 999,
                )
            )
        ).scalar_one()
        assert row.serve_count == 2

    # Failing record_served_titles must not fail the addon response
    from curio.repositories import catalogs as catalogs_repo

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("db exploded")

    monkeypatch.setattr(catalogs_repo, "record_served_titles", boom)
    r3 = await client.get(f"/{token}/catalog/movie/curio_movie_1.json")
    assert r3.status_code == 200
    assert len(r3.json()["metas"]) == 1


async def catalogs_repo_replace_one(
    session: AsyncSession,
    *,
    user_id: UUID,
    preset_id: UUID,
    tmdb_id: int,
) -> None:
    from curio.repositories import catalogs as catalogs_repo

    await catalogs_repo.replace_preset_candidates(
        session,
        user_id=user_id,
        preset_id=preset_id,
        items=[("movie", tmdb_id)],
    )


# --- Helpers for the queue, lease, and SSE tests below ---------------------------------------


def _parse_sse(raw: bytes) -> list[dict]:
    """Parse an SSE body into frames: {"id", "event", "data"} (comments are skipped)."""
    frames: list[dict] = []
    for block in raw.decode().split("\n\n"):
        frame: dict = {"id": None, "event": None, "data": None}
        for line in block.splitlines():
            if line.startswith("id: "):
                frame["id"] = line.removeprefix("id: ")
            elif line.startswith("event: "):
                frame["event"] = line.removeprefix("event: ")
            elif line.startswith("data: "):
                frame["data"] = json.loads(line.removeprefix("data: "))
        if frame["event"] is not None:
            frames.append(frame)
    return frames


async def _finish_job_with_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job: Job,
    run: Run,
    worker_id: str = "sse-worker",
) -> None:
    """Commit a terminal job state and its `completed` event, then NOTIFY."""
    async with session_factory() as s:
        await s.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(status="running", claimed_by=worker_id, lease_expires_at=func.now())
        )
        await jobs_repo.finish_run(s, run_id=run.id, status="succeeded", latency_ms=42)
        await jobs_repo.finish_job(s, job_id=job.id, worker_id=worker_id, status="succeeded")
        ev = await jobs_repo.append_run_event(
            s, run_id=run.id, user_id=job.user_id, step="completed", message="Done"
        )
        await jobs_repo.notify_job_event(s, job_id=job.id, event_id=ev.id, status="succeeded")
        await s.commit()


async def _seed_job(
    session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    keypair: Keypair,
    username: str,
    *,
    api_key: str = "tmdb-secret-key-1234",
    enqueue: bool = True,
) -> tuple[ProvisionedUser, UUID, Job | None]:
    """A committed user with a TMDB key and a preset, plus a queued `regenerate` job."""
    provisioned = await make_committed_user(username)
    user_id = provisioned.user.id
    await _save_committed_tmdb_key(
        session_factory, user_id=user_id, keypair=keypair, api_key=api_key
    )
    async with session_factory() as s:
        preset = await catalogs_service.create_preset(
            s, user_id=user_id, name=f"{username} preset", media_type="movie"
        )
        job = (
            await jobs_service.enqueue_regenerate_job(s, user_id=user_id, preset_id=preset.id)
            if enqueue
            else None
        )
        await s.commit()
    return provisioned, preset.id, job


def _stub_starter(
    monkeypatch: pytest.MonkeyPatch,
    behaviour: Callable[[int, Callable[..., Awaitable[None]]], Awaitable[None]],
) -> list[str | None]:
    """Replace the starter pipeline. `behaviour(call_number, emit_step)` runs per call.

    Returns the list of `claimed_by` values of the jobs the stub was called with.
    """
    from curio.services import starter

    callers: list[str | None] = []

    async def stub(
        session: AsyncSession,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        rate_limiter: RateLimiter,
        job: Job,
        run: Run,
        emit_step: Callable[..., Awaitable[None]],
    ) -> None:
        callers.append(job.claimed_by)
        await behaviour(len(callers), emit_step)

    monkeypatch.setattr(starter, "run_starter_pipeline", stub)
    return callers


async def _make_due(session_factory: async_sessionmaker[AsyncSession], job_id: UUID) -> None:
    async with session_factory() as s:
        await s.execute(
            update(Job).where(Job.id == job_id).values(run_at=func.now() - timedelta(seconds=1))
        )
        await s.commit()


async def _expire_lease(session_factory: async_sessionmaker[AsyncSession], job_id: UUID) -> None:
    async with session_factory() as s:
        await s.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(lease_expires_at=func.now() - timedelta(seconds=1))
        )
        await s.commit()


async def _runs_of(
    session_factory: async_sessionmaker[AsyncSession], job_id: UUID
) -> list[tuple[int, str, str | None]]:
    async with session_factory() as s:
        rows = (
            (await s.execute(select(Run).where(Run.job_id == job_id).order_by(Run.attempt)))
            .scalars()
            .all()
        )
    return [(r.attempt, r.status, r.error) for r in rows]


async def _stored_text_for_user(
    session_factory: async_sessionmaker[AsyncSession], user_id: UUID
) -> str:
    """Every jobs, runs, run_events, and key-pause row of the user, as one string."""
    queries = [
        "SELECT row_to_json(t)::text FROM jobs t WHERE user_id = :u",
        "SELECT row_to_json(t)::text FROM runs t WHERE user_id = :u",
        "SELECT row_to_json(t)::text FROM run_events t WHERE user_id = :u",
        "SELECT paused_reason FROM user_api_keys WHERE user_id = :u",
        "SELECT jobs_paused_reason FROM users WHERE id = :u",
    ]
    parts: list[str] = []
    async with session_factory() as s:
        for query in queries:
            result = await s.execute(text(query), {"u": user_id})
            parts.extend(str(row[0]) for row in result if row[0] is not None)
    return "\n".join(parts)


# --- Attempts, retries, pause/resume ----------------------------------------------------------


async def test_failed_attempt_is_retried_as_a_new_run(
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    migrated_database_url: str,
    keypair: Keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned, _, job = await _seed_job(
        audit_session_factory, make_committed_user, keypair, "retry_user"
    )
    assert job is not None
    user_id = provisioned.user.id

    async def behaviour(call: int, emit_step: Callable[..., Awaitable[None]]) -> None:
        await emit_step("work", f"attempt {call}", None)
        if call == 1:
            raise RuntimeError("flaky")

    _stub_starter(monkeypatch, behaviour)
    settings = _worker_settings(migrated_database_url, keypair)
    limiter = RateLimiter(1000)

    assert await jobs_service.process_one_job(
        audit_session_factory, settings=settings, rate_limiter=limiter, worker_id="w1"
    )
    async with audit_session_factory() as s:
        after_failure = await jobs_repo.get_job(s, user_id=user_id, job_id=job.id)
    assert after_failure is not None
    assert after_failure.status == "queued"
    assert after_failure.attempts == 1
    assert after_failure.run_at > datetime.now(UTC)  # retry backoff

    # Not claimable until the backoff has passed.
    assert not await jobs_service.process_one_job(
        audit_session_factory, settings=settings, rate_limiter=limiter, worker_id="w1"
    )
    await _make_due(audit_session_factory, job.id)
    assert await jobs_service.process_one_job(
        audit_session_factory, settings=settings, rate_limiter=limiter, worker_id="w1"
    )

    async with audit_session_factory() as s:
        final = await jobs_repo.get_job(s, user_id=user_id, job_id=job.id)
        assert final is not None
        assert (final.status, final.attempts) == ("succeeded", 2)
        latest = await jobs_repo.get_run_for_job(s, user_id=user_id, job_id=job.id)
        status = await jobs_service.get_job_status(s, user_id=user_id, job_id=job.id)
    runs = await _runs_of(audit_session_factory, job.id)
    assert [(attempt, run_status) for attempt, run_status, _ in runs] == [
        (1, "failed"),
        (2, "succeeded"),
    ]
    assert latest is not None and latest.attempt == 2
    assert status is not None
    _, status_run, status_events = status
    assert status_run is not None and status_run.id == latest.id
    assert [e.step for e in status_events] == ["work", "completed"]
    assert {e.run_id for e in status_events} == {latest.id}


@pytest.mark.parametrize("error_type", [TmdbAuthError, TmdbRateLimitError])
async def test_key_pause_does_not_consume_an_attempt_and_rerun_succeeds(
    error_type: type[Exception],
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    migrated_database_url: str,
    keypair: Keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned, _, job = await _seed_job(
        audit_session_factory, make_committed_user, keypair, f"pause_rerun_{error_type.__name__}"
    )
    assert job is not None
    user_id = provisioned.user.id

    async def behaviour(call: int, emit_step: Callable[..., Awaitable[None]]) -> None:
        if call == 1:
            raise error_type("TMDB said no", 401)

    _stub_starter(monkeypatch, behaviour)
    settings = _worker_settings(migrated_database_url, keypair)
    limiter = RateLimiter(1000)

    assert await jobs_service.process_one_job(
        audit_session_factory, settings=settings, rate_limiter=limiter, worker_id="w1"
    )
    async with audit_session_factory() as s:
        paused = await jobs_repo.get_job(s, user_id=user_id, job_id=job.id)
    assert paused is not None
    assert (paused.status, paused.paused_key_type, paused.attempts) == ("paused", "tmdb", 0)
    assert paused.claimed_by is None and paused.lease_expires_at is None
    assert [(a, st) for a, st, _ in await _runs_of(audit_session_factory, job.id)] == [
        (1, "paused")
    ]

    async with audit_session_factory() as s:
        await keys_service.resume_key_and_jobs(s, user_id=user_id, key_type=KeyType.TMDB)
        await s.commit()
    assert await jobs_service.process_one_job(
        audit_session_factory, settings=settings, rate_limiter=limiter, worker_id="w1"
    )

    async with audit_session_factory() as s:
        final = await jobs_repo.get_job(s, user_id=user_id, job_id=job.id)
    assert final is not None
    assert (final.status, final.attempts) == ("succeeded", 1)
    assert [(a, st) for a, st, _ in await _runs_of(audit_session_factory, job.id)] == [
        (1, "paused"),
        (1, "succeeded"),
    ]


async def test_key_error_pauses_the_failed_key_not_a_fixed_one(
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    migrated_database_url: str,
    keypair: Keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned, _, job = await _seed_job(
        audit_session_factory, make_committed_user, keypair, "missing_key_user"
    )
    assert job is not None

    async def behaviour(call: int, emit_step: Callable[..., Awaitable[None]]) -> None:
        raise KeyNotFoundError("no gemini key configured", key_type=KeyType.GEMINI)

    _stub_starter(monkeypatch, behaviour)
    assert await jobs_service.process_one_job(
        audit_session_factory,
        settings=_worker_settings(migrated_database_url, keypair),
        rate_limiter=RateLimiter(1000),
        worker_id="w1",
    )
    async with audit_session_factory() as s:
        paused = await jobs_repo.get_job(s, user_id=provisioned.user.id, job_id=job.id)
    assert paused is not None
    assert (paused.status, paused.paused_key_type) == ("paused", "gemini")


async def test_unknown_job_kind_fails_permanently_with_a_clear_message(
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    migrated_database_url: str,
    keypair: Keypair,
) -> None:
    provisioned, preset_id, _ = await _seed_job(
        audit_session_factory, make_committed_user, keypair, "unknown_kind_user", enqueue=False
    )
    user_id = provisioned.user.id
    async with audit_session_factory() as s:
        # `compile` passes the CHECK constraint but has no handler until a later PR registers it.
        job = await jobs_repo.enqueue_job(
            s,
            user_id=user_id,
            preset_id=preset_id,
            job_kind="compile",
            trigger="manual",
            required_keys=[],
        )
        await s.commit()
        with pytest.raises(jobs_service.UnknownJobKindError):
            await jobs_service.enqueue_job(s, user_id=user_id, job_kind="nope")

    assert await jobs_service.process_one_job(
        audit_session_factory,
        settings=_worker_settings(migrated_database_url, keypair),
        rate_limiter=RateLimiter(1000),
        worker_id="w1",
    )
    async with audit_session_factory() as s:
        failed = await jobs_repo.get_job(s, user_id=user_id, job_id=job.id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.last_error == "Unknown job kind 'compile'."


# --- Leases, reclaim, fencing -----------------------------------------------------------------


async def _simulate_dead_worker(
    session_factory: async_sessionmaker[AsyncSession],
    job: Job,
    *,
    max_attempts: int | None = None,
) -> Run:
    """Claim `job` as worker `dead` and start a run, then let the lease expire."""
    async with session_factory() as s:
        if max_attempts is not None:
            await s.execute(update(Job).where(Job.id == job.id).values(max_attempts=max_attempts))
        claimed = await jobs_repo.claim_next_job(s, worker_id="dead", lease_seconds=30)
        assert claimed is not None and not claimed.reclaimed
        run = await jobs_repo.create_run(
            s,
            user_id=job.user_id,
            job_id=job.id,
            preset_id=job.preset_id,
            attempt=claimed.job.attempts,
            pipeline="starter",
            trigger="manual",
        )
        await s.commit()
    await _expire_lease(session_factory, job.id)
    return run


async def test_expired_lease_is_reclaimed_and_old_run_failed(
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    migrated_database_url: str,
    keypair: Keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned, _, job = await _seed_job(
        audit_session_factory, make_committed_user, keypair, "reclaim_user"
    )
    assert job is not None
    user_id = provisioned.user.id
    callers = _stub_starter(monkeypatch, lambda call, emit_step: emit_step("work", "ok", None))
    stale_run = await _simulate_dead_worker(audit_session_factory, job)

    # A live lease is not reclaimable.
    async with audit_session_factory() as s:
        await s.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(lease_expires_at=func.now() + timedelta(seconds=60))
        )
        await s.commit()
    settings = _worker_settings(migrated_database_url, keypair)
    limiter = RateLimiter(1000)
    assert not await jobs_service.process_one_job(
        audit_session_factory, settings=settings, rate_limiter=limiter, worker_id="w2"
    )
    await _expire_lease(audit_session_factory, job.id)

    assert await jobs_service.process_one_job(
        audit_session_factory, settings=settings, rate_limiter=limiter, worker_id="w2"
    )
    assert callers == ["w2"]
    runs = await _runs_of(audit_session_factory, job.id)
    assert runs == [
        (1, "failed", jobs_service.LEASE_LOST_MESSAGE),
        (2, "succeeded", None),
    ]
    async with audit_session_factory() as s:
        final = await jobs_repo.get_job(s, user_id=user_id, job_id=job.id)
        stale_events = await jobs_repo.list_run_events_for_run(
            s, user_id=user_id, run_id=stale_run.id
        )
    assert final is not None
    assert (final.status, final.attempts, final.claimed_by) == ("succeeded", 2, "w2")
    assert [e.step for e in stale_events] == ["failed"]


async def test_reclaim_past_max_attempts_fails_the_job_without_running_it(
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    migrated_database_url: str,
    keypair: Keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned, _, job = await _seed_job(
        audit_session_factory, make_committed_user, keypair, "reclaim_max_user"
    )
    assert job is not None
    callers = _stub_starter(monkeypatch, lambda call, emit_step: emit_step("work", "ok", None))
    await _simulate_dead_worker(audit_session_factory, job, max_attempts=1)

    assert await jobs_service.process_one_job(
        audit_session_factory,
        settings=_worker_settings(migrated_database_url, keypair),
        rate_limiter=RateLimiter(1000),
        worker_id="w2",
    )
    assert callers == []
    async with audit_session_factory() as s:
        failed = await jobs_repo.get_job(s, user_id=provisioned.user.id, job_id=job.id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.last_error == jobs_service.LEASE_LOST_MESSAGE
    assert failed.finished_at is not None
    assert await _runs_of(audit_session_factory, job.id) == [
        (1, "failed", jobs_service.LEASE_LOST_MESSAGE)
    ]


async def test_heartbeat_extends_the_lease_while_a_job_runs(
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    migrated_database_url: str,
    keypair: Keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned, _, job = await _seed_job(
        audit_session_factory, make_committed_user, keypair, "heartbeat_user"
    )
    assert job is not None
    seen: dict[str, datetime] = {}

    async def read_lease() -> datetime | None:
        async with audit_session_factory() as s:
            row = await jobs_repo.get_job(s, user_id=provisioned.user.id, job_id=job.id)
        return row.lease_expires_at if row else None

    async def behaviour(call: int, emit_step: Callable[..., Awaitable[None]]) -> None:
        first = await read_lease()
        assert first is not None
        seen["first"] = first
        await asyncio.sleep(0.5)
        later = await read_lease()
        assert later is not None
        seen["later"] = later

    _stub_starter(monkeypatch, behaviour)
    # The setting validates `ge=10`; `model_copy` skips validation to get a sub-second interval
    # (lease/3) for the test.
    settings = _worker_settings(migrated_database_url, keypair).model_copy(
        update={"job_lease_seconds": 0.3}
    )
    assert await jobs_service.process_one_job(
        audit_session_factory,
        settings=settings,
        rate_limiter=RateLimiter(1000),
        worker_id="w1",
    )
    assert seen["later"] > seen["first"]


async def test_fenced_writes_are_noops_after_the_job_is_reclaimed(
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    migrated_database_url: str,
    keypair: Keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker A is slow, loses its lease to B, then finishes: A must not touch the job."""
    provisioned, _, job = await _seed_job(
        audit_session_factory, make_committed_user, keypair, "fencing_user"
    )
    assert job is not None
    user_id = provisioned.user.id
    a_started = asyncio.Event()
    release_a = asyncio.Event()

    async def behaviour(call: int, emit_step: Callable[..., Awaitable[None]]) -> None:
        if call == 1:
            a_started.set()
            await release_a.wait()
        else:
            await emit_step("work", "B did the work", None)

    callers = _stub_starter(monkeypatch, behaviour)
    settings = _worker_settings(migrated_database_url, keypair)
    limiter = RateLimiter(1000)

    task_a = asyncio.create_task(
        jobs_service.process_one_job(
            audit_session_factory, settings=settings, rate_limiter=limiter, worker_id="A"
        )
    )
    async with asyncio.timeout(10):
        await a_started.wait()
    await _expire_lease(audit_session_factory, job.id)
    assert await jobs_service.process_one_job(
        audit_session_factory, settings=settings, rate_limiter=limiter, worker_id="B"
    )
    release_a.set()
    async with asyncio.timeout(10):
        assert await task_a
    assert callers == ["A", "B"]

    async with audit_session_factory() as s:
        final = await jobs_repo.get_job(s, user_id=user_id, job_id=job.id)
        # Repository level: every worker-side write by A is rejected.
        assert not await jobs_repo.finish_job(
            s, job_id=job.id, worker_id="A", status="failed", last_error="late"
        )
        assert not await jobs_repo.extend_job_lease(
            s, job_id=job.id, worker_id="A", lease_seconds=60
        )
        assert not await jobs_repo.pause_job(
            s, job_id=job.id, worker_id="A", key_type="tmdb", reason="late"
        )
    assert final is not None
    assert (final.status, final.claimed_by, final.attempts) == ("succeeded", "B", 2)
    assert final.last_error is None
    assert [(a, st) for a, st, _ in await _runs_of(audit_session_factory, job.id)] == [
        (1, "failed"),
        (2, "succeeded"),
    ]


async def test_fenced_pause_after_reclaim_leaves_the_new_owner_running(
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    migrated_database_url: str,
    keypair: Keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A's 401 arrives after B owns the job: the key is paused, B's job is not."""
    provisioned, _, job = await _seed_job(
        audit_session_factory, make_committed_user, keypair, "fenced_pause_user"
    )
    assert job is not None
    user_id = provisioned.user.id
    a_started = asyncio.Event()
    release_a = asyncio.Event()

    async def behaviour(call: int, emit_step: Callable[..., Awaitable[None]]) -> None:
        if call == 1:
            a_started.set()
            await release_a.wait()
            raise TmdbAuthError("TMDB rejected the API key (HTTP 401)", 401)

    _stub_starter(monkeypatch, behaviour)
    settings = _worker_settings(migrated_database_url, keypair)
    task_a = asyncio.create_task(
        jobs_service.process_one_job(
            audit_session_factory,
            settings=settings,
            rate_limiter=RateLimiter(1000),
            worker_id="A",
        )
    )
    async with asyncio.timeout(10):
        await a_started.wait()
    await _expire_lease(audit_session_factory, job.id)
    # B reclaims (claim only; it is "still running" when A's failure lands).
    async with audit_session_factory() as s:
        claimed = await jobs_repo.claim_next_job(s, worker_id="B", lease_seconds=60)
        assert claimed is not None and claimed.reclaimed
        await s.commit()
    release_a.set()
    async with asyncio.timeout(10):
        await task_a

    async with audit_session_factory() as s:
        current = await jobs_repo.get_job(s, user_id=user_id, job_id=job.id)
    assert current is not None
    assert (current.status, current.claimed_by, current.attempts) == ("running", "B", 2)


async def test_pausing_a_key_pauses_only_queued_jobs_not_other_running_jobs(
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    keypair: Keypair,
) -> None:
    provisioned, _, running_job = await _seed_job(
        audit_session_factory, make_committed_user, keypair, "pause_scope_user"
    )
    assert running_job is not None
    user_id = provisioned.user.id
    async with audit_session_factory() as s:
        other_preset = await catalogs_service.create_preset(
            s, user_id=user_id, name="Other", media_type="series"
        )
        queued_job = await jobs_service.enqueue_regenerate_job(
            s, user_id=user_id, preset_id=other_preset.id
        )
        await s.commit()
    async with audit_session_factory() as s:
        # Claim the older job so it is `running` on a worker.
        claimed = await jobs_repo.claim_next_job(s, worker_id="w1", lease_seconds=60)
        assert claimed is not None and claimed.job.id == running_job.id
        await s.commit()

    async with audit_session_factory() as s:
        paused_ids = await keys_service.pause_key_and_jobs(
            s, user_id=user_id, key_type=KeyType.TMDB, reason="rate limited"
        )
        await s.commit()
    assert paused_ids == [queued_job.id]
    async with audit_session_factory() as s:
        still_running = await jobs_repo.get_job(s, user_id=user_id, job_id=running_job.id)
    assert still_running is not None and still_running.status == "running"


# --- Worker loop ------------------------------------------------------------------------------


async def test_worker_loop_survives_an_exception_escaping_process_one_job(
    migrated_database_url: str,
    keypair: Keypair,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from curio import worker

    calls = 0
    third_call = asyncio.Event()

    async def flaky_process_one_job(*args: object, **kwargs: object) -> bool:
        nonlocal calls
        calls += 1
        if calls >= 3:
            third_call.set()
        if calls == 1:
            raise RuntimeError("boom")
        return False

    monkeypatch.setattr(worker.jobs_service, "process_one_job", flaky_process_one_job)
    settings = Settings(
        _env_file=None,
        database_url=migrated_database_url,
        key_public_key=keypair.public_key,
        key_private_key=keypair.private_key,
        worker_heartbeat_seconds=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(settings, stop))
    try:
        async with asyncio.timeout(10):
            await third_call.wait()
        assert not task.done()
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
    assert any(
        r.levelno == logging.ERROR and r.exc_info and "boom" in str(r.exc_info[1])
        for r in caplog.records
    )


# --- Enqueue: paused keys, integrity errors, commit before respond -------------------------------


async def test_enqueue_is_rejected_while_the_tmdb_key_is_paused(
    committed_client: httpx.AsyncClient,
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    keypair: Keypair,
) -> None:
    provisioned, preset_id, _ = await _seed_job(
        audit_session_factory, make_committed_user, keypair, "paused_enqueue_user", enqueue=False
    )
    user_id = provisioned.user.id
    async with audit_session_factory() as s:
        await keys_service.pause_key_and_jobs(
            s, user_id=user_id, key_type=KeyType.TMDB, reason="TMDB rejected the API key"
        )
        await s.commit()

    async with audit_session_factory() as s:
        with pytest.raises(jobs_service.KeyPausedError) as excinfo:
            await jobs_service.enqueue_regenerate_job(s, user_id=user_id, preset_id=preset_id)
    assert excinfo.value.key_type is KeyType.TMDB

    await _login_client(committed_client, provisioned.login_token)
    resp = await committed_client.post(f"/api/presets/{preset_id}/regenerate")
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "TMDB key paused: TMDB rejected the API key. Replace your TMDB key in Settings to resume."
    )
    async with audit_session_factory() as s:
        assert (await s.execute(select(Job.id).where(Job.user_id == user_id))).first() is None


async def test_only_the_active_job_constraint_is_reported_as_a_duplicate(
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    keypair: Keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned, preset_id, _ = await _seed_job(
        audit_session_factory, make_committed_user, keypair, "integrity_user", enqueue=False
    )
    user_id = provisioned.user.id

    def integrity_error(constraint: str | None) -> IntegrityError:
        diag = SimpleNamespace(constraint_name=constraint)
        orig = type("FakePgError", (Exception,), {"diag": diag})("violates constraint")
        return IntegrityError("INSERT INTO jobs", {}, orig)

    async def raising(constraint: str | None, *args: object, **kwargs: object) -> Job:
        raise integrity_error(constraint)

    async with audit_session_factory() as s:
        monkeypatch.setattr(
            jobs_repo, "enqueue_job", lambda *a, **k: raising("jobs_user_id_fkey", *a, **k)
        )
        with pytest.raises(IntegrityError):
            await jobs_service.enqueue_regenerate_job(s, user_id=user_id, preset_id=preset_id)
        monkeypatch.setattr(
            jobs_repo,
            "enqueue_job",
            lambda *a, **k: raising(jobs_service.ACTIVE_JOB_CONSTRAINT, *a, **k),
        )
        with pytest.raises(jobs_service.DuplicateJobError):
            await jobs_service.enqueue_regenerate_job(s, user_id=user_id, preset_id=preset_id)


async def test_regenerate_returns_202_with_the_job_already_committed(
    committed_client: httpx.AsyncClient,
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    keypair: Keypair,
) -> None:
    provisioned, preset_id, _ = await _seed_job(
        audit_session_factory, make_committed_user, keypair, "commit_first_user", enqueue=False
    )
    await _login_client(committed_client, provisioned.login_token)

    resp = await committed_client.post(f"/api/presets/{preset_id}/regenerate")

    assert resp.status_code == 202
    job_id = UUID(resp.json()["id"])
    async with audit_session_factory() as other_session:
        job = await jobs_repo.get_job(other_session, user_id=provisioned.user.id, job_id=job_id)
    assert job is not None and job.status == "queued"


# --- Error sanitisation -------------------------------------------------------------------------


async def test_unexpected_errors_are_stored_as_a_generic_message(
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    migrated_database_url: str,
    keypair: Keypair,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "tmdb-leaky-secret-7777"
    provisioned, _, job = await _seed_job(
        audit_session_factory, make_committed_user, keypair, "sanitise_user", api_key=secret
    )
    assert job is not None
    user_id = provisioned.user.id

    async def behaviour(call: int, emit_step: Callable[..., Awaitable[None]]) -> None:
        raise ValueError(f"connection string had {secret} in it")

    _stub_starter(monkeypatch, behaviour)
    assert await jobs_service.process_one_job(
        audit_session_factory,
        settings=_worker_settings(migrated_database_url, keypair),
        rate_limiter=RateLimiter(1000),
        worker_id="w1",
    )

    generic = "Unexpected error (ValueError). See worker logs."
    async with audit_session_factory() as s:
        db_job = await jobs_repo.get_job(s, user_id=user_id, job_id=job.id)
        events = await jobs_repo.list_run_events_for_job(s, user_id=user_id, job_id=job.id)
    assert db_job is not None and db_job.last_error == generic
    assert [(a, st, err) for a, st, err in await _runs_of(audit_session_factory, job.id)] == [
        (1, "failed", generic)
    ]
    assert [e.message for e in events] == [f"Failed: {generic}"]
    assert secret not in await _stored_text_for_user(audit_session_factory, user_id)
    # The full exception is still available to operators.
    assert any(r.exc_info and secret in str(r.exc_info[1]) for r in caplog.records)


def test_safe_error_message_exposes_only_allow_listed_types() -> None:
    assert jobs_service.safe_error_message(TmdbAuthError("bad key", 401)) == "bad key"
    assert jobs_service.safe_error_message(KeyNotFoundError("no key")) == "no key"
    assert (
        jobs_service.safe_error_message(jobs_service.KeyPausedError(KeyType.TMDB, "why"))
        == "TMDB key paused: why. Replace your TMDB key in Settings to resume."
    )
    assert (
        jobs_service.safe_error_message(OSError("/secret/path"))
        == "Unexpected error (OSError). See worker logs."
    )


@pytest.mark.parametrize("status_code", [401, 429])
async def test_api_key_never_reaches_job_run_or_event_rows_on_tmdb_errors(
    status_code: int,
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    migrated_database_url: str,
    keypair: Keypair,
    respx_mock: object,
) -> None:
    import respx

    assert isinstance(respx_mock, respx.MockRouter)
    secret = f"tmdb-never-store-me-{status_code}"
    provisioned, preset_id, job = await _seed_job(
        audit_session_factory,
        make_committed_user,
        keypair,
        f"secret_user_{status_code}",
        api_key=secret,
    )
    assert job is not None
    user_id = provisioned.user.id
    async with audit_session_factory() as s:
        s.add(
            MediaItem(
                media_type="movie",
                tmdb_id=500 + status_code,
                imdb_id=f"tt0005{status_code}",
                name="Seed",
            )
        )
        await s.flush()
        s.add(
            WatchHistory(
                user_id=user_id,
                media_type="movie",
                tmdb_id=500 + status_code,
                status="watched",
                rating=9,
            )
        )
        await s.commit()
    # TMDB echoing the key back in its error body (as some proxies do) must not be stored.
    respx_mock.get(f"{TMDB_BASE_URL}/movie/{500 + status_code}/recommendations").mock(
        return_value=httpx.Response(
            status_code, json={"status_message": f"Invalid API key: {secret}"}
        )
    )

    assert await jobs_service.process_one_job(
        audit_session_factory,
        settings=_worker_settings(migrated_database_url, keypair),
        rate_limiter=RateLimiter(1000),
        worker_id="w1",
    )

    async with audit_session_factory() as s:
        db_job = await jobs_repo.get_job(s, user_id=user_id, job_id=job.id)
    assert db_job is not None and db_job.status == "paused"
    stored = await _stored_text_for_user(audit_session_factory, user_id)
    assert "TMDB" in stored
    assert secret not in stored


# --- SSE ----------------------------------------------------------------------------------------


async def _seed_running_job_for_stream(
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    keypair: Keypair,
    username: str,
) -> tuple[ProvisionedUser, Job, Run]:
    provisioned, _, job = await _seed_job(
        audit_session_factory, make_committed_user, keypair, username
    )
    assert job is not None
    async with audit_session_factory() as s:
        claimed = await jobs_repo.claim_next_job(s, worker_id="sse-worker", lease_seconds=600)
        assert claimed is not None
        run = await jobs_repo.create_run(
            s,
            user_id=job.user_id,
            job_id=job.id,
            preset_id=job.preset_id,
            attempt=1,
            pipeline="starter",
            trigger="manual",
        )
        await s.commit()
    return provisioned, job, run


async def test_sse_streams_do_not_hold_pooled_connections(
    committed_app: FastAPI,
    committed_client: httpx.AsyncClient,
    migrated_database_url: str,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    audit_session_factory: async_sessionmaker[AsyncSession],
    keypair: Keypair,
) -> None:
    """More open streams than pooled connections, yet ordinary requests still work."""
    provisioned, job, run = await _seed_running_job_for_stream(
        audit_session_factory, make_committed_user, keypair, "sse_pool_user"
    )
    engine = create_async_engine(
        to_sqlalchemy_url(migrated_database_url),
        pool_size=2,
        max_overflow=0,
        pool_timeout=3,
    )
    committed_app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    committed_app.state.settings.sse_max_streams_per_user = 10
    try:
        await _login_client(committed_client, provisioned.login_token)
        streams = [
            asyncio.create_task(committed_client.get(f"/api/jobs/{job.id}/events"))
            for _ in range(6)
        ]
        await asyncio.sleep(0.5)
        assert not any(t.done() for t in streams)
        assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]

        # With a pool of 2, this would time out if the six streams each held a connection.
        settings_resp = await committed_client.get("/api/settings")
        assert settings_resp.status_code == 200

        await _finish_job_with_event(audit_session_factory, job=job, run=run)
        async with asyncio.timeout(10):
            responses = await asyncio.gather(*streams)
    finally:
        await engine.dispose()
    for resp in responses:
        assert resp.status_code == 200
        frames = _parse_sse(resp.content)
        assert [f["event"] for f in frames] == ["step", "status"]


async def test_sse_rejects_streams_over_the_per_user_cap(
    committed_app: FastAPI,
    committed_client: httpx.AsyncClient,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    audit_session_factory: async_sessionmaker[AsyncSession],
    keypair: Keypair,
) -> None:
    provisioned, job, run = await _seed_running_job_for_stream(
        audit_session_factory, make_committed_user, keypair, "sse_cap_user"
    )
    committed_app.state.settings.sse_max_streams_per_user = 2
    await _login_client(committed_client, provisioned.login_token)
    streams = [
        asyncio.create_task(committed_client.get(f"/api/jobs/{job.id}/events")) for _ in range(2)
    ]
    await asyncio.sleep(0.5)

    rejected = await committed_client.get(f"/api/jobs/{job.id}/events")
    assert rejected.status_code == 429

    await _finish_job_with_event(audit_session_factory, job=job, run=run)
    async with asyncio.timeout(10):
        responses = await asyncio.gather(*streams)
    assert [r.status_code for r in responses] == [200, 200]
    # The slots are released when the streams end.
    again = await committed_client.get(f"/api/jobs/{job.id}/events")
    assert again.status_code == 200


async def test_sse_stream_ends_after_the_maximum_lifetime(
    committed_app: FastAPI,
    committed_client: httpx.AsyncClient,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    audit_session_factory: async_sessionmaker[AsyncSession],
    keypair: Keypair,
) -> None:
    provisioned, job, _ = await _seed_running_job_for_stream(
        audit_session_factory, make_committed_user, keypair, "sse_lifetime_user"
    )
    committed_app.state.settings.sse_max_stream_seconds = 0.5
    await _login_client(committed_client, provisioned.login_token)
    async with asyncio.timeout(10):
        resp = await committed_client.get(f"/api/jobs/{job.id}/events")
    assert resp.status_code == 200
    assert _parse_sse(resp.content) == []


async def test_sse_requires_authentication_and_user_scoped_job(
    committed_client: httpx.AsyncClient,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    audit_session_factory: async_sessionmaker[AsyncSession],
    keypair: Keypair,
) -> None:
    owner, job, _ = await _seed_running_job_for_stream(
        audit_session_factory, make_committed_user, keypair, "sse_owner"
    )
    other = await make_committed_user("sse_other")

    assert (await committed_client.get(f"/api/jobs/{job.id}/events")).status_code == 401
    await _login_client(committed_client, other.login_token)
    assert (await committed_client.get(f"/api/jobs/{job.id}/events")).status_code == 404
    assert owner.user.id != other.user.id


async def test_sse_replays_events_after_last_event_id(
    committed_client: httpx.AsyncClient,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    audit_session_factory: async_sessionmaker[AsyncSession],
    keypair: Keypair,
) -> None:
    provisioned, job, run = await _seed_running_job_for_stream(
        audit_session_factory, make_committed_user, keypair, "sse_replay_user"
    )
    async with audit_session_factory() as s:
        for step in ("one", "two", "three"):
            await jobs_repo.append_run_event(
                s, run_id=run.id, user_id=job.user_id, step=step, message=step
            )
        await s.commit()
    await _finish_job_with_event(audit_session_factory, job=job, run=run)
    await _login_client(committed_client, provisioned.login_token)

    full = _parse_sse((await committed_client.get(f"/api/jobs/{job.id}/events")).content)
    step_frames = [f for f in full if f["event"] == "step"]
    assert [f["data"]["step"] for f in step_frames] == ["one", "two", "three", "completed"]
    assert [f["id"] for f in step_frames] == [str(f["data"]["id"]) for f in step_frames]

    resumed = _parse_sse(
        (
            await committed_client.get(
                f"/api/jobs/{job.id}/events", headers={"Last-Event-ID": step_frames[1]["id"]}
            )
        ).content
    )
    assert [f["data"]["step"] for f in resumed if f["event"] == "step"] == ["three", "completed"]
    assert resumed[-1]["event"] == "status"


async def test_job_event_hub_wakes_only_subscribers_of_the_notified_job(
    migrated_database_url: str,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    hub = JobEventHub(migrated_database_url)
    job_a, job_b = uuid4(), uuid4()
    await hub.start()
    try:
        async with asyncio.timeout(10):
            await hub.wait_listening()
        async with hub.subscribe(job_a) as wake_a, hub.subscribe(job_b) as wake_b:
            # The hub wakes everyone when it (re)connects; drain that first.
            for queue in (wake_a, wake_b):
                while not queue.empty():
                    queue.get_nowait()
            async with audit_session_factory() as s:
                await jobs_repo.notify_job_event(s, job_id=job_a, event_id=7, status="running")
                await s.commit()
            async with asyncio.timeout(5):
                message = await wake_a.get()
            assert message == {"job_id": str(job_a), "event_id": 7, "status": "running"}
            await asyncio.sleep(0.2)
            assert wake_b.empty()
    finally:
        async with asyncio.timeout(5):
            await hub.stop()


async def test_job_event_hub_stop_is_prompt_even_while_the_database_is_unreachable() -> None:
    hub = JobEventHub("postgresql://nobody@127.0.0.1:1/none", reconnect_min_seconds=30)
    await hub.start()
    await asyncio.sleep(0.3)  # first connect fails; the hub is now sleeping in its backoff
    async with asyncio.timeout(5):
        await hub.stop()


def test_sse_route_fails_clearly_when_the_hub_is_not_running() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    with pytest.raises(RuntimeError, match="JobEventHub is not running"):
        get_job_event_hub(request)  # type: ignore[arg-type]


# --- Served titles ------------------------------------------------------------------------------


async def test_slow_served_titles_write_is_dropped_after_the_timeout(
    committed_app: FastAPI,
    committed_client: httpx.AsyncClient,
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from curio.repositories import catalogs as catalogs_repo

    provisioned = await make_committed_user("served_slow_user")
    user_id = provisioned.user.id
    async with audit_session_factory() as s:
        preset = await catalogs_service.create_preset(
            s, user_id=user_id, name="Slow Preset", media_type="movie"
        )
        await catalogs_service.assign_slot(
            s, user_id=user_id, slot_id="curio_movie_1", preset_id=preset.id
        )
        s.add(MediaItem(media_type="movie", tmdb_id=998, imdb_id="tt0000998", name="Slow Movie"))
        await s.flush()
        await catalogs_repo_replace_one(s, user_id=user_id, preset_id=preset.id, tmdb_id=998)
        await s.commit()

    async def slow_write(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(catalogs_repo, "record_served_titles", slow_write)
    committed_app.state.settings.served_titles_timeout_seconds = 0.2

    started = time.monotonic()
    async with asyncio.timeout(10):
        resp = await committed_client.get(
            f"/{provisioned.user.addon_token}/catalog/movie/curio_movie_1.json"
        )
    elapsed = time.monotonic() - started

    assert resp.status_code == 200 and len(resp.json()["metas"]) == 1
    assert elapsed < 5  # the stub sleeps 30 s; the write was abandoned after 0.2 s
    assert any("timed out" in r.getMessage() for r in caplog.records)
    async with audit_session_factory() as s:
        rows = (await s.execute(select(ServedTitle).where(ServedTitle.user_id == user_id))).all()
    assert rows == []
