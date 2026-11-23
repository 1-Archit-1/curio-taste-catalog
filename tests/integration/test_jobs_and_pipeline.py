"""Integration tests for jobs, starter pipeline, pause/resume, SSE, and served_titles."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.clients.providers import KeyType
from curio.clients.ratelimit import RateLimiter
from curio.clients.tmdb import BASE_URL as TMDB_BASE_URL
from curio.crypto import KeyEncryptor, Keypair
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

    # Save TMDB key -> 201, then duplicate -> 409
    await _save_committed_tmdb_key(
        audit_session_factory, user_id=provisioned.user.id, keypair=keypair
    )
    resp_201 = await client.post(f"/api/presets/{preset.id}/regenerate")
    assert resp_201.status_code == 201
    job_payload = resp_201.json()
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
    client: httpx.AsyncClient,
    audit_session_factory: async_sessionmaker[AsyncSession],
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
    keypair: Keypair,
) -> None:
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
        async with audit_session_factory() as s:
            await jobs_repo.finish_run(s, run_id=run.id, status="succeeded", latency_ms=42)
            await jobs_repo.finish_job(s, job_id=job.id, status="succeeded")
            ev2 = await jobs_repo.append_run_event(
                s,
                run_id=run.id,
                user_id=user_id,
                step="completed",
                message="Done",
            )
            await jobs_repo.notify_job_event(s, job_id=job.id, event_id=ev2.id, status="succeeded")
            await s.commit()

    publisher = asyncio.create_task(publish_events())
    events_seen: list[tuple[str, dict]] = []

    async with client.stream("GET", f"/api/jobs/{job.id}/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        current_event: str | None = None
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                current_event = line.removeprefix("event: ")
            elif line.startswith("data: ") and current_event is not None:
                events_seen.append((current_event, json.loads(line.removeprefix("data: "))))
                current_event = None

    await publisher

    step_events = [data for name, data in events_seen if name == "step"]
    status_events = [data for name, data in events_seen if name == "status"]
    assert [e["step"] for e in step_events] == ["seeds", "completed"]
    assert len(status_events) == 1
    assert status_events[0]["status"] == "succeeded"


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
