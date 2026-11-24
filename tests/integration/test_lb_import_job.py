"""The `lb_import` job: upload route, worker handler, failure handling, and upload limits."""

from collections.abc import Awaitable, Callable
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.api.routers import control
from curio.clients.providers import KeyType
from curio.clients.ratelimit import RateLimiter
from curio.crypto import Keypair
from curio.db.models.catalogs import MediaItem, WatchHistory
from curio.db.models.jobs import Job
from curio.db.models.sources import Source, SourceUpload
from curio.repositories import jobs as jobs_repo
from curio.services import jobs as jobs_service
from curio.services import keys as keys_service
from curio.services import sources as sources_service
from curio.services.users import ProvisionedUser
from tests.integration.lb_support import (
    TMDB_BASE,
    build_fixture_zip,
    build_zip,
    login,
    mock_tmdb_for_zip,
    save_committed_tmdb_key,
    worker_settings,
)

pytestmark = pytest.mark.usefixtures("clean_lb_data")

MakeUser = Callable[[str], Awaitable[ProvisionedUser]]
SessionFactory = async_sessionmaker[AsyncSession]


async def _upload(
    client: httpx.AsyncClient,
    zip_bytes: bytes,
    role: str = "self",
) -> httpx.Response:
    return await client.post(
        "/api/sources/import-zip",
        files={"file": ("export.zip", zip_bytes, "application/zip")},
        data={"role": role},
    )


async def _upload_count(factory: SessionFactory, user_id: UUID) -> int:
    async with factory() as s:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(SourceUpload)
                .where(SourceUpload.user_id == user_id)
            )
            or 0
        )


async def _job(factory: SessionFactory, user_id: UUID, job_id: str) -> Job:
    async with factory() as s:
        job = await jobs_repo.get_job(s, user_id=user_id, job_id=UUID(job_id))
    assert job is not None
    return job


async def _work(
    factory: SessionFactory, database_url: str, keypair: Keypair, worker_id: str = "w1"
) -> bool:
    return await jobs_service.process_one_job(
        factory,
        settings=worker_settings(database_url, keypair),
        rate_limiter=RateLimiter(1000),
        worker_id=worker_id,
    )


async def _make_due(factory: SessionFactory, job_id: str) -> None:
    async with factory() as s:
        await s.execute(text("UPDATE jobs SET run_at = now() WHERE id = :id"), {"id": job_id})
        await s.commit()


async def _set_max_attempts(factory: SessionFactory, job_id: str, value: int) -> None:
    async with factory() as s:
        await s.execute(
            text("UPDATE jobs SET max_attempts = :v WHERE id = :id"), {"v": value, "id": job_id}
        )
        await s.commit()


async def test_lb_import_runs_in_the_worker_and_is_idempotent(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    keypair: Keypair,
    respx_mock: respx.MockRouter,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("lbjob_user")
    uid = user.user.id
    await save_committed_tmdb_key(audit_session_factory, user_id=uid, keypair=keypair)
    await login(committed_client, user.login_token)
    routes = mock_tmdb_for_zip(respx_mock)

    resp = await _upload(committed_client, build_fixture_zip())

    assert resp.status_code == 202
    queued = resp.json()
    assert (queued["job_kind"], queued["status"], queued["preset_id"]) == (
        "lb_import",
        "queued",
        None,
    )
    assert await _upload_count(audit_session_factory, uid) == 1
    assert not routes["search_movie"].called  # the API never calls TMDB

    assert await _work(audit_session_factory, migrated_database_url, keypair)

    status = (await committed_client.get(f"/api/jobs/{queued['id']}")).json()
    assert status["job"]["status"] == "succeeded"
    done = [e for e in status["events"] if e["step"] == "import"]
    assert done[0]["payload"] == {
        "matched_count": 5,
        "issue_count": 1,
        "skipped_count": 0,
        "source_count": 2,
    }
    assert [e["step"] for e in status["events"]][-1] == "completed"
    assert await _upload_count(audit_session_factory, uid) == 0

    listed = (await committed_client.get("/api/sources")).json()["sources"]
    assert sorted(s["kind"] for s in listed) == ["lb_list_csv", "lb_profile_csv"]
    assert all(s["role"] == "self" and s["last_refreshed_at"] for s in listed)
    async with audit_session_factory() as s:
        history = list(await s.scalars(select(WatchHistory).where(WatchHistory.user_id == uid)))
        matrix = await s.scalar(select(MediaItem).where(MediaItem.tmdb_id == 603))
        owned = list(await s.scalars(select(Source).where(Source.owner_user_id == uid)))
    assert [(h.tmdb_id, h.rating) for h in history] == [(603, 9.0)]
    assert history[0].source_id in {src.id for src in owned}
    assert matrix is not None
    assert matrix.hydrated_at is not None

    # Importing the same export again changes nothing.
    again = await _upload(committed_client, build_fixture_zip())
    assert again.status_code == 202
    assert await _work(audit_session_factory, migrated_database_url, keypair)
    relisted = (await committed_client.get("/api/sources")).json()["sources"]
    assert {s["id"]: s["item_count"] for s in relisted} == {
        s["id"]: s["item_count"] for s in listed
    }
    issues = (await committed_client.get("/api/import-issues")).json()
    assert issues["pending_count"] == 1
    async with audit_session_factory() as s:
        count = await s.scalar(
            select(func.count()).select_from(WatchHistory).where(WatchHistory.user_id == uid)
        )
    assert count == 1


async def test_import_is_rejected_without_a_tmdb_key_or_while_it_is_paused(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    keypair: Keypair,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("lbjob_keys_user")
    uid = user.user.id
    await login(committed_client, user.login_token)

    missing = await _upload(committed_client, build_fixture_zip())
    assert missing.status_code == 422
    assert "tmdb" in missing.json()["detail"]
    assert await _upload_count(audit_session_factory, uid) == 0

    await save_committed_tmdb_key(audit_session_factory, user_id=uid, keypair=keypair)
    async with audit_session_factory() as s:
        await keys_service.pause_key_and_jobs(
            s, user_id=uid, key_type=KeyType.TMDB, reason="TMDB rejected the API key"
        )
        await s.commit()
    paused = await _upload(committed_client, build_fixture_zip())
    assert paused.status_code == 409
    assert paused.json()["detail"] == (
        "TMDB key paused: TMDB rejected the API key. Replace your TMDB key in Settings to resume."
    )
    assert await _upload_count(audit_session_factory, uid) == 0
    async with audit_session_factory() as s:
        assert (await s.execute(select(Job.id).where(Job.user_id == uid))).first() is None


async def test_only_one_import_runs_at_a_time_and_roles_are_validated(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    keypair: Keypair,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("lbjob_dup_user")
    uid = user.user.id
    await save_committed_tmdb_key(audit_session_factory, user_id=uid, keypair=keypair)
    await login(committed_client, user.login_token)

    assert (await _upload(committed_client, build_fixture_zip(), role="owner")).status_code == 422
    assert (await _upload(committed_client, build_fixture_zip())).status_code == 202
    second = await _upload(committed_client, build_fixture_zip())
    assert second.status_code == 409
    assert second.json()["detail"] == "an import is already in progress"
    assert await _upload_count(audit_session_factory, uid) == 1


async def test_upload_over_50_mb_is_rejected_with_413(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    keypair: Keypair,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("lbjob_big_user")
    await save_committed_tmdb_key(audit_session_factory, user_id=user.user.id, keypair=keypair)
    await login(committed_client, user.login_token)

    resp = await _upload(committed_client, b"\0" * (50 * 1024 * 1024 + 1))

    assert resp.status_code == 413
    assert resp.json()["detail"] == "Export ZIP exceeds the 50 MB size limit."
    assert await _upload_count(audit_session_factory, user.user.id) == 0


async def test_upload_is_cut_off_while_reading_when_content_length_is_not_enough(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    keypair: Keypair,
    make_committed_user: MakeUser,
):
    """The chunked read enforces the limit on its own, e.g. when the header is missing or low."""
    user = await make_committed_user("lbjob_chunk_user")
    await save_committed_tmdb_key(audit_session_factory, user_id=user.user.id, keypair=keypair)
    await login(committed_client, user.login_token)

    with (
        patch.object(control, "MAX_ZIP_BYTES", 1000),
        patch.object(control, "_MULTIPART_OVERHEAD_BYTES", 10**9),
        patch.object(control, "_UPLOAD_READ_CHUNK", 300),
    ):
        too_big = await _upload(committed_client, b"x" * 1001)
        fits = await _upload(committed_client, build_zip({"watched.csv": "x"}))

    assert too_big.status_code == 413
    assert fits.status_code == 202
    assert await _upload_count(audit_session_factory, user.user.id) == 1


async def test_an_invalid_zip_fails_the_job_at_once_and_discards_the_upload(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    keypair: Keypair,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("lbjob_badzip_user")
    uid = user.user.id
    await save_committed_tmdb_key(audit_session_factory, user_id=uid, keypair=keypair)
    await login(committed_client, user.login_token)
    queued = (await _upload(committed_client, b"definitely not a zip")).json()

    assert await _work(audit_session_factory, migrated_database_url, keypair)

    job = await _job(audit_session_factory, uid, queued["id"])
    assert (job.status, job.attempts) == ("failed", 1)
    assert job.last_error == "Uploaded file is not a valid ZIP archive."
    assert await _upload_count(audit_session_factory, uid) == 0


async def test_a_zip_with_too_many_files_fails_the_job(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    keypair: Keypair,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("lbjob_many_user")
    uid = user.user.id
    await save_committed_tmdb_key(audit_session_factory, user_id=uid, keypair=keypair)
    await login(committed_client, user.login_token)
    many = build_zip({f"lists/l{i}.csv": "Position,Name,Year,URL\n" for i in range(1001)})
    queued = (await _upload(committed_client, many)).json()

    assert await _work(audit_session_factory, migrated_database_url, keypair)

    job = await _job(audit_session_factory, uid, queued["id"])
    assert job.status == "failed"
    assert job.last_error == "Export ZIP has more than 1000 files."
    assert await _upload_count(audit_session_factory, uid) == 0


async def test_unexpected_failures_keep_the_upload_until_the_last_attempt(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    keypair: Keypair,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("lbjob_retry_user")
    uid = user.user.id
    await save_committed_tmdb_key(audit_session_factory, user_id=uid, keypair=keypair)
    await login(committed_client, user.login_token)
    queued = (await _upload(committed_client, build_fixture_zip())).json()

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database exploded")

    with patch.object(sources_service, "import_export_archive", boom):
        assert await _work(audit_session_factory, migrated_database_url, keypair)
        job = await _job(audit_session_factory, uid, queued["id"])
        assert (job.status, job.attempts) == ("queued", 1)
        assert job.last_error == "Unexpected error (RuntimeError). See worker logs."
        assert await _upload_count(audit_session_factory, uid) == 1

        await _make_due(audit_session_factory, queued["id"])
        await _set_max_attempts(audit_session_factory, queued["id"], 2)
        assert await _work(audit_session_factory, migrated_database_url, keypair)

    job = await _job(audit_session_factory, uid, queued["id"])
    assert (job.status, job.attempts) == ("failed", 2)
    assert await _upload_count(audit_session_factory, uid) == 0


async def test_a_rejected_tmdb_key_pauses_the_import_and_keeps_the_upload(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    keypair: Keypair,
    respx_mock: respx.MockRouter,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("lbjob_paused_user")
    uid = user.user.id
    await save_committed_tmdb_key(audit_session_factory, user_id=uid, keypair=keypair)
    await login(committed_client, user.login_token)
    respx_mock.get(f"{TMDB_BASE}/search/movie").mock(
        return_value=httpx.Response(401, json={"status_message": "Invalid API key"})
    )
    queued = (await _upload(committed_client, build_fixture_zip())).json()

    assert await _work(audit_session_factory, migrated_database_url, keypair)

    job = await _job(audit_session_factory, uid, queued["id"])
    assert job.status == "paused"
    assert job.paused_key_type == "tmdb"
    assert await _upload_count(audit_session_factory, uid) == 1
    async with audit_session_factory() as s:
        owned = list(await s.scalars(select(Source).where(Source.owner_user_id == uid)))
    assert owned == []


async def test_a_missing_upload_fails_the_job_with_a_clear_message(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    keypair: Keypair,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("lbjob_gone_user")
    uid = user.user.id
    await save_committed_tmdb_key(audit_session_factory, user_id=uid, keypair=keypair)
    await login(committed_client, user.login_token)
    queued = (await _upload(committed_client, build_fixture_zip())).json()
    async with audit_session_factory() as s:
        await s.execute(text("DELETE FROM source_uploads WHERE user_id = :u"), {"u": uid})
        await s.commit()

    assert await _work(audit_session_factory, migrated_database_url, keypair)

    job = await _job(audit_session_factory, uid, queued["id"])
    assert (job.status, job.attempts) == ("failed", 1)
    assert job.last_error == "The uploaded file is no longer available. Upload it again."


async def test_uploads_are_deleted_with_their_user(
    audit_session_factory: SessionFactory,
    make_committed_user: MakeUser,
    keypair: Keypair,
):
    user = await make_committed_user("lbjob_cascade_user")
    await save_committed_tmdb_key(audit_session_factory, user_id=user.user.id, keypair=keypair)
    async with audit_session_factory() as s:
        await jobs_service.enqueue_lb_import_job(
            s, user_id=user.user.id, filename="e.zip", data=b"PK", role="self"
        )
        await s.commit()
    assert await _upload_count(audit_session_factory, user.user.id) == 1
    async with audit_session_factory() as s:
        await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": user.user.id})
        await s.commit()
    assert await _upload_count(audit_session_factory, user.user.id) == 0


@pytest.mark.parametrize("payload", [None, {}, {"upload_id": "nope", "role": "self"}])
async def test_a_job_without_a_usable_upload_id_is_a_permanent_failure(
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    keypair: Keypair,
    make_committed_user: MakeUser,
    payload: dict[str, str] | None,
):
    user = await make_committed_user("lbjob_payload_user")
    uid = user.user.id
    await save_committed_tmdb_key(audit_session_factory, user_id=uid, keypair=keypair)
    async with audit_session_factory() as s:
        job = await jobs_service.enqueue_job(s, user_id=uid, job_kind="lb_import", payload=payload)
        await s.commit()

    assert await _work(audit_session_factory, migrated_database_url, keypair)

    stored = await _job(audit_session_factory, uid, str(job.id))
    assert (stored.status, stored.attempts) == ("failed", 1)
    assert stored.last_error == "The import job has no upload. Upload the file again."
