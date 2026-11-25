import io
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from curio.clients import letterboxd
from curio.clients.letterboxd import (
    LetterboxdClient,
    LetterboxdError,
    LetterboxdNotFoundError,
    normalize_letterboxd_username,
    parse_export_zip,
    parse_rss,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "letterboxd"


def _build_fixture_zip(*, prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel in (
            "profile.csv",
            "watched.csv",
            "ratings.csv",
            "diary.csv",
            "watchlist.csv",
            "lists/sci-fi-favorites.csv",
        ):
            zf.write(FIXTURES / rel, arcname=f"{prefix}{rel}")
    return buf.getvalue()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("alice", "alice"),
        ("@Alice", "alice"),
        ("https://letterboxd.com/alice/", "alice"),
        ("http://www.letterboxd.com/Alice/rss/", "alice"),
        ("letterboxd.com/alice/films/", "alice"),
    ],
)
def test_normalize_letterboxd_username_accepts_handles_and_urls(raw: str, expected: str):
    assert normalize_letterboxd_username(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "https://example.com/alice/",
        "https://letterboxd.com/",
        "https://letterboxd.com/films/",
        "bad user!",
    ],
)
def test_normalize_letterboxd_username_rejects_invalid_inputs(raw: str):
    with pytest.raises(LetterboxdError):
        normalize_letterboxd_username(raw)


def test_parse_rss_extracts_movie_and_tv_entries():
    xml_text = (FIXTURES / "diary_rss.xml").read_text(encoding="utf-8")
    feed = parse_rss(xml_text, username="alice", feed_url="https://letterboxd.com/alice/rss/")

    assert feed.username == "alice"
    assert feed.title == "Letterboxd - alice"
    assert len(feed.entries) == 2

    first, second = feed.entries
    assert first.media_type == "movie"
    assert first.tmdb_id == 603
    assert first.title == "The Matrix"
    assert first.year == 1999
    assert first.rating == 10.0  # 5 stars on Curio's 0-10 scale
    assert first.watched_at is not None
    assert first.watched_at.strftime("%Y-%m-%d") == "2026-11-14"
    assert first.entry_url == "https://letterboxd.com/alice/film/the-matrix/"
    assert first.guid == "letterboxd-diary-1001"

    assert second.media_type == "series"
    assert second.tmdb_id == 1396
    assert second.rating == 9.0


async def test_fetch_rss_handles_success_404_and_500(respx_mock: respx.MockRouter):
    xml_text = (FIXTURES / "diary_rss.xml").read_text(encoding="utf-8")
    respx_mock.get("https://letterboxd.com/alice/rss/").mock(
        return_value=httpx.Response(200, text=xml_text)
    )
    respx_mock.get("https://letterboxd.com/missing/rss/").mock(return_value=httpx.Response(404))
    respx_mock.get("https://letterboxd.com/broken/rss/").mock(return_value=httpx.Response(500))

    async with LetterboxdClient() as client:
        feed = await client.fetch_rss("https://letterboxd.com/alice/")
        assert len(feed.entries) == 2

        with pytest.raises(LetterboxdNotFoundError):
            await client.fetch_rss("missing")

        with pytest.raises(LetterboxdError):
            await client.fetch_rss("broken")


@pytest.mark.parametrize("prefix", ["", "letterboxd-alice-2026/"])
def test_parse_export_zip_extracts_profile_and_lists(prefix: str):
    archive = parse_export_zip(_build_fixture_zip(prefix=prefix))

    assert archive.username == "alice"
    kinds = [r.item_kind for r in archive.profile_rows]
    assert kinds == ["watched", "watched", "rating", "diary", "watchlist"]
    ratings = {r.item_kind: r.rating for r in archive.profile_rows if r.rating is not None}
    assert ratings == {"rating": 9.0, "diary": 10.0}
    assert archive.profile_rows[4].watched_at is None

    assert len(archive.lists) == 1
    lst = archive.lists[0]
    assert lst.slug == "sci-fi-favorites"
    assert lst.name == "Sci-Fi Favorites"
    assert lst.url == "https://letterboxd.com/alice/list/sci-fi-favorites/"
    assert len(lst.rows) == 1
    assert lst.rows[0].title == "The Matrix"


def test_parse_export_zip_rejects_invalid_and_empty_archives():
    with pytest.raises(LetterboxdError):
        parse_export_zip(b"not-a-zip")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "nothing here")
    with pytest.raises(LetterboxdError):
        parse_export_zip(buf.getvalue())


def _zip_of(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


_WATCHED_CSV = b"Date,Name,Year,Letterboxd URI\n2026-10-01,The Matrix,1999,https://boxd.it/1\n"


def test_parse_export_zip_rejects_too_many_members():
    data = _zip_of({f"lists/l{i}.csv": b"Position,Name,Year,URL\n" for i in range(6)})
    with (
        patch.object(letterboxd, "MAX_ZIP_MEMBERS", 5),
        pytest.raises(LetterboxdError, match="more than 5 files"),
    ):
        parse_export_zip(data)


def test_parse_export_zip_rejects_oversized_entry():
    data = _zip_of({"watched.csv": _WATCHED_CSV + b"x" * 5000})
    with (
        patch.object(letterboxd, "MAX_ZIP_ENTRY_BYTES", 1000),
        pytest.raises(LetterboxdError, match="exceeds size limit"),
    ):
        parse_export_zip(data)


def test_parse_export_zip_rejects_entry_whose_header_understates_its_size():
    """The cap applies to bytes read, and a header that lies about size fails the CRC check."""
    data = bytearray(_zip_of({"watched.csv": _WATCHED_CSV + b"x" * 5000}))
    real_size = (len(_WATCHED_CSV) + 5000).to_bytes(4, "little")
    fake_size = (10).to_bytes(4, "little")
    # Uncompressed size sits in both the local header and the central directory.
    assert data.count(real_size) == 2
    patched = bytes(data).replace(real_size, fake_size)
    with pytest.raises(LetterboxdError):
        parse_export_zip(patched)


def test_parse_export_zip_rejects_total_uncompressed_size_over_cap():
    data = _zip_of(
        {
            "watched.csv": _WATCHED_CSV + b"x" * 600,
            "ratings.csv": b"Date,Name,Year,Letterboxd URI,Rating\n" + b"y" * 600,
        }
    )
    with (
        patch.object(letterboxd, "MAX_ZIP_TOTAL_BYTES", 1000),
        pytest.raises(LetterboxdError, match="too large once uncompressed"),
    ):
        parse_export_zip(data)


def test_parse_export_zip_rejects_archives_over_the_zip_size_cap():
    with (
        patch.object(letterboxd, "MAX_ZIP_BYTES", 100),
        pytest.raises(LetterboxdError, match="50 MB"),
    ):
        parse_export_zip(_zip_of({"watched.csv": _WATCHED_CSV}))


async def test_fetch_rss_rejects_oversized_body(respx_mock: respx.MockRouter):
    xml_text = (FIXTURES / "diary_rss.xml").read_text(encoding="utf-8")
    respx_mock.get("https://letterboxd.com/alice/rss/").mock(
        return_value=httpx.Response(200, text=xml_text)
    )
    with patch.object(letterboxd, "MAX_RSS_BYTES", 200):
        async with LetterboxdClient() as client:
            with pytest.raises(LetterboxdError, match="too large"):
                await client.fetch_rss("alice")


async def test_fetch_rss_follows_redirects_on_letterboxd_only(respx_mock: respx.MockRouter):
    xml_text = (FIXTURES / "diary_rss.xml").read_text(encoding="utf-8")
    respx_mock.get("https://letterboxd.com/old/rss/").mock(
        return_value=httpx.Response(301, headers={"Location": "https://letterboxd.com/alice/rss/"})
    )
    respx_mock.get("https://letterboxd.com/alice/rss/").mock(
        return_value=httpx.Response(200, text=xml_text)
    )
    respx_mock.get("https://letterboxd.com/evil/rss/").mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.example/rss/"})
    )
    plain_http = respx_mock.get("https://letterboxd.com/downgrade/rss/").mock(
        return_value=httpx.Response(302, headers={"Location": "http://letterboxd.com/alice/rss/"})
    )
    offsite = respx_mock.get("https://evil.example/rss/").mock(
        return_value=httpx.Response(200, text=xml_text)
    )
    async with LetterboxdClient() as client:
        assert len((await client.fetch_rss("old")).entries) == 2
        with pytest.raises(LetterboxdError, match="unexpected address"):
            await client.fetch_rss("evil")
        with pytest.raises(LetterboxdError, match="unexpected address"):
            await client.fetch_rss("downgrade")
    assert not offsite.called
    assert plain_http.called


async def test_fetch_rss_gives_up_on_redirect_loops(respx_mock: respx.MockRouter):
    respx_mock.get("https://letterboxd.com/loop/rss/").mock(
        return_value=httpx.Response(302, headers={"Location": "https://letterboxd.com/loop/rss/"})
    )
    async with LetterboxdClient() as client:
        with pytest.raises(LetterboxdError, match="too many times"):
            await client.fetch_rss("loop")
