"""Integration tests for Issue #3: manifest, catalog serving, exclusions, vetoes, and seed_demo."""

from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from curio.db.models.catalogs import (
    CatalogCandidate,
    MediaItem,
    Preset,
    Slot,
    WatchHistory,
)
from curio.seed import seed_demo
from curio.services import catalogs as catalogs_service
from curio.services import users as users_service


async def _add_media_item(
    session: AsyncSession,
    *,
    imdb_id: str,
    media_type: str = "movie",
    name: str = "Test Title",
    rating: float | None = 8.0,
    poster_url: str | None = "https://image.tmdb.org/t/p/w500/test.jpg",
    description: str | None = "Test description.",
) -> MediaItem:
    item = MediaItem(
        imdb_id=imdb_id,
        media_type=media_type,
        name=name,
        rating=rating,
        poster_url=poster_url,
        description=description,
    )
    session.add(item)
    await session.flush()
    return item


async def test_manifest_shape_and_cors(
    client: httpx.AsyncClient,
    session: AsyncSession,
) -> None:
    provisioned = await users_service.create_user_with_login_token(
        session, username="manifest_user"
    )
    token = provisioned.user.addon_token

    resp = await client.get(f"/{token}/manifest.json")
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"
    assert resp.headers["cache-control"] == "no-cache, no-store, must-revalidate"

    body = resp.json()
    assert body["types"] == ["movie", "series"]
    assert body["idPrefixes"] == ["tt"]
    assert body["resources"] == ["catalog"]
    assert len(body["catalogs"]) == 6
    assert [c["id"] for c in body["catalogs"]] == [
        "curio_movie_1",
        "curio_movie_2",
        "curio_movie_3",
        "curio_series_1",
        "curio_series_2",
        "curio_series_3",
    ]
    for cat in body["catalogs"]:
        assert cat["extra"] == [{"name": "skip"}]
        assert cat["type"] in ("movie", "series")
        assert cat["name"]

    missing = await client.get("/nonexistent-token/manifest.json")
    assert missing.status_code == 404
    assert missing.headers["access-control-allow-origin"] == "*"


async def test_poster_fallback_asset(client: httpx.AsyncClient) -> None:
    resp = await client.get("/addon-assets/poster-fallback.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert resp.headers["cache-control"] == "public, max-age=86400"
    assert resp.headers["access-control-allow-origin"] == "*"
    assert "<svg" in resp.text


async def test_catalog_serving_pagination_min_rating_posters_and_errors(
    client: httpx.AsyncClient,
    session: AsyncSession,
) -> None:
    provisioned = await users_service.create_user_with_login_token(session, username="cat_user")
    user = provisioned.user

    # Unassigned slot returns empty metas
    empty_resp = await client.get(f"/{user.addon_token}/catalog/movie/curio_movie_2.json")
    assert empty_resp.status_code == 200
    assert empty_resp.json() == {"metas": []}
    assert empty_resp.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    assert empty_resp.headers["access-control-allow-origin"] == "*"

    preset = await catalogs_service.create_preset(
        session,
        user_id=user.id,
        name="High Rated",
        media_type="movie",
        min_rating=7.0,
    )
    await catalogs_service.assign_slot(
        session,
        user_id=user.id,
        slot_id="curio_movie_1",
        preset_id=preset.id,
    )

    # 25 qualifying items (1st has None poster, others have TMDB poster), plus low-rated & unrated
    for idx in range(25):
        imdb_id = f"tt9000{idx:03d}"
        poster = None if idx == 0 else f"https://image.tmdb.org/t/p/w500/p{idx}.jpg"
        await _add_media_item(
            session,
            imdb_id=imdb_id,
            media_type="movie",
            name=f"Movie {idx:02d}",
            rating=8.0,
            poster_url=poster,
            description=f"Desc {idx}",
        )
        session.add(
            CatalogCandidate(
                user_id=user.id,
                preset_id=preset.id,
                imdb_id=imdb_id,
                position=idx,
                blurb=f"Blurb {idx}" if idx == 0 else None,
            )
        )

    for idx, bad_rating in enumerate((6.5, None)):
        imdb_id = f"tt9990{idx:03d}"
        await _add_media_item(
            session,
            imdb_id=imdb_id,
            media_type="movie",
            name=f"Filtered {idx}",
            rating=bad_rating,
        )
        session.add(
            CatalogCandidate(
                user_id=user.id,
                preset_id=preset.id,
                imdb_id=imdb_id,
                position=100 + idx,
            )
        )
    await session.flush()

    page1 = await client.get(f"/{user.addon_token}/catalog/movie/curio_movie_1.json")
    assert page1.status_code == 200
    assert page1.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    assert page1.headers["access-control-allow-origin"] == "*"
    metas1 = page1.json()["metas"]
    assert len(metas1) == 20
    assert metas1[0] == {
        "id": "tt9000000",
        "type": "movie",
        "name": "Movie 00",
        "poster": "http://test/addon-assets/poster-fallback.svg",
        "description": "Blurb 0",
    }
    assert metas1[1]["poster"] == "https://image.tmdb.org/t/p/w500/p1.jpg"
    assert metas1[1]["description"] == "Desc 1"

    page2 = await client.get(f"/{user.addon_token}/catalog/movie/curio_movie_1/skip=20.json")
    assert page2.status_code == 200
    metas2 = page2.json()["metas"]
    assert len(metas2) == 5
    assert [m["id"] for m in metas2] == [f"tt9000{i:03d}" for i in range(20, 25)]

    # Invalid token, invalid slot, type mismatch -> 404 with CORS *
    for bad_path in (
        "/bad-token/catalog/movie/curio_movie_1.json",
        f"/{user.addon_token}/catalog/movie/unknown_slot.json",
        f"/{user.addon_token}/catalog/series/curio_movie_1.json",
    ):
        err = await client.get(bad_path)
        assert err.status_code == 404
        assert err.headers["access-control-allow-origin"] == "*"

    # Invalid skip -> 400 with CORS *
    bad_skip = await client.get(f"/{user.addon_token}/catalog/movie/curio_movie_1/skip=abc.json")
    assert bad_skip.status_code == 400
    assert bad_skip.headers["access-control-allow-origin"] == "*"


async def test_zero_leakage_of_excluded_titles(
    client: httpx.AsyncClient,
    session: AsyncSession,
) -> None:
    provisioned = await users_service.create_user_with_login_token(session, username="excl_user")
    user = provisioned.user

    preset = await catalogs_service.create_preset(
        session,
        user_id=user.id,
        name="Exclusion Test",
        media_type="movie",
    )
    await catalogs_service.assign_slot(
        session,
        user_id=user.id,
        slot_id="curio_movie_1",
        preset_id=preset.id,
    )

    titles = [
        ("tt8000001", "Watched Movie"),
        ("tt8000002", "Dropped Movie"),
        ("tt8000003", "Seen Veto Movie"),
        ("tt8000004", "Not For Me Veto Movie"),
        ("tt8000005", "Watching Movie"),
        ("tt8000006", "Eligible Movie"),
    ]
    for idx, (imdb_id, name) in enumerate(titles):
        await _add_media_item(session, imdb_id=imdb_id, media_type="movie", name=name)
        session.add(
            CatalogCandidate(
                user_id=user.id,
                preset_id=preset.id,
                imdb_id=imdb_id,
                position=idx,
            )
        )
    await session.flush()

    session.add_all(
        [
            WatchHistory(user_id=user.id, imdb_id="tt8000001", status="watched"),
            WatchHistory(user_id=user.id, imdb_id="tt8000002", status="dropped"),
            WatchHistory(user_id=user.id, imdb_id="tt8000005", status="watching"),
        ]
    )
    await session.flush()

    await catalogs_service.upsert_veto(session, user_id=user.id, imdb_id="tt8000003", kind="seen")
    await catalogs_service.upsert_veto(
        session, user_id=user.id, imdb_id="tt8000004", kind="not_for_me"
    )

    resp = await client.get(f"/{user.addon_token}/catalog/movie/curio_movie_1.json")
    assert resp.status_code == 200
    returned_ids = [m["id"] for m in resp.json()["metas"]]
    assert returned_ids == ["tt8000005", "tt8000006"]


async def test_seen_vs_not_for_me_and_anti_taste_views(session: AsyncSession) -> None:
    provisioned = await users_service.create_user_with_login_token(session, username="taste_user")
    user = provisioned.user

    await _add_media_item(session, imdb_id="tt7000001", media_type="movie", name="Seen Movie")
    await _add_media_item(session, imdb_id="tt7000002", media_type="movie", name="Disliked Movie")
    await _add_media_item(session, imdb_id="tt7000003", media_type="movie", name="Low Rated Movie")
    await _add_media_item(session, imdb_id="tt7000004", media_type="series", name="Dropped Series")

    await catalogs_service.upsert_veto(session, user_id=user.id, imdb_id="tt7000001", kind="seen")
    await catalogs_service.upsert_veto(
        session, user_id=user.id, imdb_id="tt7000002", kind="not_for_me"
    )
    session.add_all(
        [
            WatchHistory(user_id=user.id, imdb_id="tt7000003", status="watched", rating=4.0),
            WatchHistory(user_id=user.id, imdb_id="tt7000004", status="dropped", rating=None),
        ]
    )
    await session.flush()

    excluded = await catalogs_service.list_excluded_titles(session, user_id=user.id)
    assert {(e.imdb_id, e.reason) for e in excluded} == {
        ("tt7000001", "seen"),
        ("tt7000002", "not_for_me"),
        ("tt7000003", "watch_history"),
        ("tt7000004", "watch_history"),
    }

    anti_taste = await catalogs_service.list_anti_taste(session, user_id=user.id)
    assert {(a.imdb_id, a.reason, a.rating) for a in anti_taste} == {
        ("tt7000002", "not_for_me", None),
        ("tt7000003", "low_rating", 4.0),
        ("tt7000004", "dropped_series", None),
    }


async def test_unveto_preserves_watch_history(
    client: httpx.AsyncClient,
    session: AsyncSession,
) -> None:
    provisioned = await users_service.create_user_with_login_token(session, username="unveto_user")
    user = provisioned.user

    preset = await catalogs_service.create_preset(
        session, user_id=user.id, name="Movies", media_type="movie"
    )
    await catalogs_service.assign_slot(
        session, user_id=user.id, slot_id="curio_movie_1", preset_id=preset.id
    )
    await _add_media_item(session, imdb_id="tt6000001", media_type="movie", name="Already Watched")
    session.add(
        CatalogCandidate(user_id=user.id, preset_id=preset.id, imdb_id="tt6000001", position=0)
    )
    session.add(WatchHistory(user_id=user.id, imdb_id="tt6000001", status="watched", rating=9.0))
    await session.flush()

    # Veto and then un-veto the watched title
    await catalogs_service.upsert_veto(session, user_id=user.id, imdb_id="tt6000001", kind="seen")
    deleted = await catalogs_service.delete_veto(session, user_id=user.id, imdb_id="tt6000001")
    assert deleted is True

    # WatchHistory row must remain untouched
    wh = (
        await session.execute(
            select(WatchHistory).where(
                WatchHistory.user_id == user.id,
                WatchHistory.imdb_id == "tt6000001",
            )
        )
    ).scalar_one_or_none()
    assert wh is not None
    assert wh.status == "watched"
    assert wh.rating == 9.0

    # Title must still be excluded from the catalog
    resp = await client.get(f"/{user.addon_token}/catalog/movie/curio_movie_1.json")
    assert resp.status_code == 200
    assert resp.json()["metas"] == []


async def test_service_validation_errors(session: AsyncSession) -> None:
    provisioned = await users_service.create_user_with_login_token(session, username="val_user")
    user = provisioned.user

    movie_preset = await catalogs_service.create_preset(
        session, user_id=user.id, name="Movies", media_type="movie"
    )
    with pytest.raises(ValueError, match="does not match"):
        await catalogs_service.assign_slot(
            session,
            user_id=user.id,
            slot_id="curio_series_1",
            preset_id=movie_preset.id,
        )

    with pytest.raises(LookupError, match="not found"):
        await catalogs_service.upsert_veto(
            session,
            user_id=user.id,
            imdb_id="tt0000000",
            kind="seen",
        )


async def test_seed_demo_is_idempotent(session: AsyncSession) -> None:
    first = await seed_demo(session, username="demo")
    assert first.created_user is True
    assert first.login_token is not None

    second = await seed_demo(session, username="demo")
    assert second.created_user is False
    assert second.login_token is None
    assert second.user.id == first.user.id
    assert second.movie_preset_id == first.movie_preset_id
    assert second.series_preset_id == first.series_preset_id
    assert second.movies_count == first.movies_count
    assert second.series_count == first.series_count

    preset_count = (
        await session.execute(
            select(func.count()).select_from(Preset).where(Preset.user_id == first.user.id)
        )
    ).scalar_one()
    slot_count = (
        await session.execute(
            select(func.count()).select_from(Slot).where(Slot.user_id == first.user.id)
        )
    ).scalar_one()
    assert preset_count == 2
    assert slot_count == 6


async def _login(client: httpx.AsyncClient, login_token: str) -> None:
    resp = await client.post("/api/auth/login", json={"token": login_token})
    assert resp.status_code == 200


async def test_control_plane_requires_auth(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/slots")).status_code == 401
    assert (await client.get("/api/presets")).status_code == 401
    assert (
        await client.post("/api/presets", json={"name": "Test", "media_type": "movie"})
    ).status_code == 401
    assert (await client.put("/api/vetoes/tt0133093", json={"kind": "seen"})).status_code == 401


async def test_control_plane_presets_slots_candidates_vetoes_and_isolation(
    client: httpx.AsyncClient,
    session: AsyncSession,
) -> None:
    alice = await users_service.create_user_with_login_token(session, username="cp_alice")
    bob = await users_service.create_user_with_login_token(session, username="cp_bob")

    await _add_media_item(
        session,
        imdb_id="tt8100001",
        media_type="movie",
        name="Fallback Poster Movie",
        poster_url=None,
        rating=8.2,
    )
    await _add_media_item(
        session,
        imdb_id="tt8100002",
        media_type="movie",
        name="Real Poster Movie",
        poster_url="https://image.tmdb.org/t/p/w500/real.jpg",
        rating=7.9,
    )

    await _login(client, alice.login_token)

    # Initial slots: 6 fixed slots, all unassigned
    slots_resp = await client.get("/api/slots")
    assert slots_resp.status_code == 200
    slots = slots_resp.json()["slots"]
    assert len(slots) == 6
    assert all(s["preset_id"] is None for s in slots)

    # Create movie and series presets
    create_resp = await client.post(
        "/api/presets",
        json={"name": "  Alice Movies  ", "media_type": "movie", "min_rating": 7.5},
    )
    assert create_resp.status_code == 201
    movie_preset = create_resp.json()
    assert movie_preset["name"] == "Alice Movies"
    assert movie_preset["media_type"] == "movie"
    assert movie_preset["min_rating"] == 7.5

    series_resp = await client.post(
        "/api/presets",
        json={"name": "Alice Series", "media_type": "series"},
    )
    assert series_resp.status_code == 201
    series_preset = series_resp.json()

    # List presets
    list_resp = await client.get("/api/presets")
    assert list_resp.status_code == 200
    assert len(list_resp.json()["presets"]) == 2

    # Update preset
    patch_resp = await client.patch(
        f"/api/presets/{movie_preset['id']}",
        json={"name": "Alice Top Movies", "min_rating": 8.0},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["name"] == "Alice Top Movies"
    assert patch_resp.json()["min_rating"] == 8.0

    # Unknown preset -> 404
    missing_id = "00000000-0000-0000-0000-000000000999"
    assert (
        await client.patch(f"/api/presets/{missing_id}", json={"name": "Nope"})
    ).status_code == 404
    assert (await client.delete(f"/api/presets/{missing_id}")).status_code == 404
    assert (await client.get(f"/api/presets/{missing_id}/candidates")).status_code == 404

    # Slot assignment: success, unknown slot 404, media_type mismatch 400, missing preset 400
    assign_resp = await client.put(
        "/api/slots/curio_movie_1",
        json={"preset_id": movie_preset["id"]},
    )
    assert assign_resp.status_code == 200
    assert assign_resp.json()["preset_id"] == movie_preset["id"]
    assert assign_resp.json()["preset_name"] == "Alice Top Movies"

    assert (
        await client.put("/api/slots/unknown_slot", json={"preset_id": movie_preset["id"]})
    ).status_code == 404
    assert (
        await client.put("/api/slots/curio_series_1", json={"preset_id": movie_preset["id"]})
    ).status_code == 400
    assert (
        await client.put("/api/slots/curio_movie_2", json={"preset_id": missing_id})
    ).status_code == 400

    # Add candidates directly to Alice's movie preset
    movie_preset_uuid = UUID(movie_preset["id"])
    session.add_all(
        [
            CatalogCandidate(
                user_id=alice.user.id,
                preset_id=movie_preset_uuid,
                imdb_id="tt8100001",
                position=1,
                blurb="First pick",
            ),
            CatalogCandidate(
                user_id=alice.user.id,
                preset_id=movie_preset_uuid,
                imdb_id="tt8100002",
                position=2,
                blurb="Second pick",
            ),
        ]
    )
    await session.flush()

    cand_resp = await client.get(f"/api/presets/{movie_preset['id']}/candidates")
    assert cand_resp.status_code == 200
    cands = cand_resp.json()["candidates"]
    assert len(cands) == 2
    assert cands[0]["imdb_id"] == "tt8100001"
    assert cands[0]["poster"].endswith("/addon-assets/poster-fallback.svg")
    assert cands[0]["veto_kind"] is None
    assert cands[1]["poster"] == "https://image.tmdb.org/t/p/w500/real.jpg"

    # Veto & Undo
    assert (await client.put("/api/vetoes/tt0000000", json={"kind": "seen"})).status_code == 404

    veto_resp = await client.put("/api/vetoes/tt8100001", json={"kind": "seen"})
    assert veto_resp.status_code == 200
    assert veto_resp.json() == {"imdb_id": "tt8100001", "kind": "seen"}

    cands_after_veto = (await client.get(f"/api/presets/{movie_preset['id']}/candidates")).json()[
        "candidates"
    ]
    assert cands_after_veto[0]["veto_kind"] == "seen"

    del_veto_resp = await client.delete("/api/vetoes/tt8100001")
    assert del_veto_resp.status_code == 200
    assert del_veto_resp.json() == {"message": "Veto removed."}
    # Idempotent delete
    assert (await client.delete("/api/vetoes/tt8100001")).status_code == 200

    cands_after_undo = (await client.get(f"/api/presets/{movie_preset['id']}/candidates")).json()[
        "candidates"
    ]
    assert cands_after_undo[0]["veto_kind"] is None

    # User isolation: log in as Bob and verify he cannot access Alice's presets
    await _login(client, bob.login_token)
    assert (await client.get("/api/presets")).json()["presets"] == []
    assert (
        await client.patch(f"/api/presets/{movie_preset['id']}", json={"name": "Hijack"})
    ).status_code == 404
    assert (await client.delete(f"/api/presets/{movie_preset['id']}")).status_code == 404
    assert (await client.get(f"/api/presets/{movie_preset['id']}/candidates")).status_code == 404
    assert (
        await client.put("/api/slots/curio_movie_1", json={"preset_id": movie_preset["id"]})
    ).status_code == 400

    # Log back in as Alice, unassign slot and delete preset
    await _login(client, alice.login_token)
    unassign_resp = await client.put("/api/slots/curio_movie_1", json={"preset_id": None})
    assert unassign_resp.status_code == 200
    assert unassign_resp.json()["preset_id"] is None

    del_preset_resp = await client.delete(f"/api/presets/{series_preset['id']}")
    assert del_preset_resp.status_code == 200
    assert del_preset_resp.json() == {"message": "Preset deleted."}
