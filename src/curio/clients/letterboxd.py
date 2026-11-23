"""Letterboxd client: export ZIP parsing and public diary RSS fetching.

Rules (docs/architecture.md & ADR 0004): httpx and stdlib only; never touch the database,
never scrape HTML, and never use unofficial APIs.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_ZIP_ENTRY_BYTES = 20 * 1024 * 1024

_USERNAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_RESERVED_SEGMENTS = frozenset(
    {
        "film",
        "films",
        "list",
        "lists",
        "actor",
        "director",
        "writer",
        "studio",
        "genre",
        "search",
        "settings",
        "sign-in",
        "create-account",
        "about",
        "pro",
        "patron",
        "journal",
        "members",
        "activity",
    }
)

_NS = {
    "letterboxd": "https://letterboxd.com",
    "tmdb": "https://themoviedb.org",
    "dc": "http://purl.org/dc/elements/1.1/",
}

ExportItemKind = Literal["watched", "rating", "diary", "watchlist", "list"]


class LetterboxdError(Exception):
    """Raised when a Letterboxd URL, RSS feed, or export ZIP is invalid or unreachable."""


class LetterboxdNotFoundError(LetterboxdError):
    """Raised when a public Letterboxd RSS feed returns 404."""


@dataclass(frozen=True)
class LetterboxdRssEntry:
    """One film/series entry parsed from a public Letterboxd diary RSS feed."""

    media_type: Literal["movie", "series"]
    tmdb_id: int | None
    title: str
    year: int | None
    rating: float | None
    watched_at: datetime | None
    entry_url: str | None
    guid: str | None


@dataclass(frozen=True)
class LetterboxdRssFeed:
    """Parsed public Letterboxd RSS feed."""

    username: str
    feed_url: str
    title: str
    entries: list[LetterboxdRssEntry] = field(default_factory=list)


@dataclass(frozen=True)
class LetterboxdExportRow:
    """One normalized row from a Letterboxd export CSV."""

    item_kind: ExportItemKind
    title: str
    year: int | None
    letterboxd_url: str | None
    rating: float | None
    watched_at: datetime | None
    list_slug: str | None = None
    list_name: str | None = None
    list_url: str | None = None


@dataclass(frozen=True)
class LetterboxdExportList:
    """One list CSV extracted from a Letterboxd export ZIP."""

    slug: str
    name: str
    url: str | None
    rows: list[LetterboxdExportRow] = field(default_factory=list)


@dataclass(frozen=True)
class LetterboxdExportArchive:
    """Parsed contents of a Letterboxd export ZIP archive."""

    username: str | None
    profile_rows: list[LetterboxdExportRow] = field(default_factory=list)
    lists: list[LetterboxdExportList] = field(default_factory=list)


def normalize_letterboxd_username(value: str) -> str:
    """Extract and validate a Letterboxd username from a handle or profile/RSS URL."""
    raw = value.strip()
    if not raw:
        raise LetterboxdError("Enter a Letterboxd username or profile URL.")

    if raw.startswith("@"):
        raw = raw[1:].strip()

    if "://" in raw or raw.lower().startswith(("letterboxd.com/", "www.letterboxd.com/")):
        to_parse = raw if "://" in raw else f"https://{raw}"
        parsed = urlparse(to_parse)
        host = (parsed.hostname or "").lower()
        if host not in {"letterboxd.com", "www.letterboxd.com"}:
            raise LetterboxdError("URL must be a letterboxd.com profile or RSS link.")
        segments = [seg for seg in parsed.path.split("/") if seg]
        if not segments:
            raise LetterboxdError("Letterboxd URL does not include a username.")
        candidate = segments[0].lower()
    else:
        candidate = raw.strip("/").lower()

    if candidate in _RESERVED_SEGMENTS or not _USERNAME_RE.match(candidate):
        raise LetterboxdError(
            "Invalid Letterboxd username. Use a handle like 'alice' or https://letterboxd.com/alice/."
        )
    return candidate


def rss_url_for_username(username: str) -> str:
    """Canonical public diary RSS URL for a normalized Letterboxd username."""
    return f"https://letterboxd.com/{username}/rss/"


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_float(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        parsed = float(cleaned)
    except ValueError:
        return None
    if parsed < 0 or parsed > 5:
        return None
    return parsed


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        d = datetime.strptime(cleaned[:10], "%Y-%m-%d")
        return d.replace(tzinfo=UTC)
    except ValueError:
        return None


def _text(elem: ET.Element, path: str) -> str | None:
    node = elem.find(path, _NS)
    if node is None or node.text is None:
        return None
    stripped = node.text.strip()
    return stripped or None


def parse_rss(xml_text: str, *, username: str, feed_url: str) -> LetterboxdRssFeed:
    """Parse a Letterboxd public diary RSS XML document."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise LetterboxdError("Letterboxd returned invalid RSS XML.") from exc

    channel = root.find("channel")
    if channel is None:
        raise LetterboxdError("Letterboxd RSS feed is missing a <channel> element.")

    channel_title = (channel.findtext("title") or "").strip() or f"Letterboxd - {username}"
    entries: list[LetterboxdRssEntry] = []

    for item in channel.findall("item"):
        film_title = _text(item, "letterboxd:filmTitle")
        if not film_title:
            # Skip non-film list/news items if any appear without a filmTitle.
            continue

        movie_id = _parse_int(_text(item, "tmdb:movieId"))
        tv_id = _parse_int(_text(item, "tmdb:tvId"))
        if movie_id is not None:
            media_type: Literal["movie", "series"] = "movie"
            tmdb_id: int | None = movie_id
        elif tv_id is not None:
            media_type = "series"
            tmdb_id = tv_id
        else:
            media_type = "movie"
            tmdb_id = None

        entries.append(
            LetterboxdRssEntry(
                media_type=media_type,
                tmdb_id=tmdb_id,
                title=film_title,
                year=_parse_int(_text(item, "letterboxd:filmYear")),
                rating=_parse_float(_text(item, "letterboxd:memberRating")),
                watched_at=_parse_date(_text(item, "letterboxd:watchedDate")),
                entry_url=(item.findtext("link") or "").strip() or None,
                guid=(item.findtext("guid") or "").strip() or None,
            )
        )

    return LetterboxdRssFeed(
        username=username,
        feed_url=feed_url,
        title=channel_title,
        entries=entries,
    )


def _decode_csv_text(raw: bytes) -> str:
    return raw.decode("utf-8-sig", errors="replace")


def _parse_standard_csv(text: str, item_kind: ExportItemKind) -> list[LetterboxdExportRow]:
    reader = csv.DictReader(io.StringIO(text))
    rows: list[LetterboxdExportRow] = []
    for row in reader:
        title = (row.get("Name") or "").strip()
        if not title:
            continue
        year = _parse_int(row.get("Year"))
        url = (row.get("Letterboxd URI") or row.get("URL") or "").strip() or None
        rating = _parse_float(row.get("Rating"))
        watched_raw = row.get("Watched Date") or row.get("Date")
        watched_at = None if item_kind == "watchlist" else _parse_date(watched_raw)
        rows.append(
            LetterboxdExportRow(
                item_kind=item_kind,
                title=title,
                year=year,
                letterboxd_url=url,
                rating=rating,
                watched_at=watched_at,
            )
        )
    return rows


def _parse_list_csv(text: str, slug: str) -> LetterboxdExportList:
    lines = text.splitlines()
    list_name = slug
    list_url: str | None = None
    table_start = 0

    # Letterboxd v7 list exports start with metadata lines before the Position,Name,... table.
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("Position,Name") or stripped.startswith("Date,Name,Year"):
            table_start = idx
            break

    if table_start > 0:
        meta_lines = [
            ln for ln in lines[:table_start] if ln.strip() and not ln.startswith("Letterboxd list")
        ]
        if len(meta_lines) >= 2:
            meta_reader = csv.DictReader(io.StringIO("\n".join(meta_lines)))
            for meta in meta_reader:
                if meta.get("Name"):
                    list_name = meta["Name"].strip()
                if meta.get("URL"):
                    list_url = meta["URL"].strip() or None
                break

    table_text = "\n".join(lines[table_start:])
    reader = csv.DictReader(io.StringIO(table_text))
    rows: list[LetterboxdExportRow] = []
    for row in reader:
        title = (row.get("Name") or "").strip()
        if not title:
            continue
        rows.append(
            LetterboxdExportRow(
                item_kind="list",
                title=title,
                year=_parse_int(row.get("Year")),
                letterboxd_url=(row.get("URL") or row.get("Letterboxd URI") or "").strip() or None,
                rating=None,
                watched_at=None,
                list_slug=slug,
                list_name=list_name,
                list_url=list_url,
            )
        )
    return LetterboxdExportList(slug=slug, name=list_name, url=list_url, rows=rows)


def _strip_root_prefix(names: list[str]) -> dict[str, str]:
    """Map normalized relative path (e.g. 'ratings.csv') to archive member name."""
    mapping: dict[str, str] = {}
    for member in names:
        if member.endswith("/"):
            continue
        parts = [p for p in member.replace("\\", "/").split("/") if p]
        if not parts:
            continue
        # Handle both flat zips ('ratings.csv') and folder exports ('export-dir/ratings.csv').
        rel = f"lists/{parts[-1]}" if len(parts) >= 2 and parts[-2] == "lists" else parts[-1]
        mapping[rel] = member
    return mapping


def parse_export_zip(data: bytes) -> LetterboxdExportArchive:
    """Parse a Letterboxd account export ZIP into profile rows and lists."""
    if len(data) > MAX_ZIP_BYTES:
        raise LetterboxdError("Export ZIP exceeds the 50 MB size limit.")

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise LetterboxdError("Uploaded file is not a valid ZIP archive.") from exc

    with zf:
        rel_map = _strip_root_prefix(zf.namelist())
        username: str | None = None

        def read_member(member_name: str) -> str:
            info = zf.getinfo(member_name)
            if info.file_size > MAX_ZIP_ENTRY_BYTES:
                raise LetterboxdError(f"Archive entry {member_name} exceeds size limit.")
            return _decode_csv_text(zf.read(member_name))

        if "profile.csv" in rel_map:
            reader = csv.DictReader(io.StringIO(read_member(rel_map["profile.csv"])))
            for row in reader:
                raw_user = (row.get("Username") or "").strip().lower()
                if raw_user and _USERNAME_RE.match(raw_user):
                    username = raw_user
                break

        profile_rows: list[LetterboxdExportRow] = []
        for filename, kind in (
            ("watched.csv", "watched"),
            ("ratings.csv", "rating"),
            ("diary.csv", "diary"),
            ("watchlist.csv", "watchlist"),
        ):
            if filename in rel_map:
                profile_rows.extend(_parse_standard_csv(read_member(rel_map[filename]), kind))

        lists: list[LetterboxdExportList] = []
        for rel, member in sorted(rel_map.items()):
            if rel.startswith("lists/") and rel.lower().endswith(".csv"):
                slug = rel.removeprefix("lists/").removesuffix(".csv")
                parsed_list = _parse_list_csv(read_member(member), slug)
                if parsed_list.rows:
                    lists.append(parsed_list)

        if not profile_rows and not lists:
            raise LetterboxdError(
                "No recognized Letterboxd CSV files (watched, ratings, diary, watchlist, or lists) "
                "were found in the ZIP archive."
            )

        return LetterboxdExportArchive(
            username=username,
            profile_rows=profile_rows,
            lists=lists,
        )


class LetterboxdClient:
    """Async HTTP client for public Letterboxd diary RSS feeds."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._http = http or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "CurioTasteCatalog/0.1 (+https://github.com/1-Archit-1/curio-taste-catalog)"
            },
        )
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> LetterboxdClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def fetch_rss(self, username_or_url: str) -> LetterboxdRssFeed:
        """Fetch and parse a user's public Letterboxd diary RSS feed."""
        username = normalize_letterboxd_username(username_or_url)
        url = rss_url_for_username(username)
        try:
            resp = await self._http.get(url)
        except httpx.TimeoutException as exc:
            raise LetterboxdError("Timed out contacting Letterboxd.") from exc
        except httpx.HTTPError as exc:
            raise LetterboxdError("Network error contacting Letterboxd.") from exc

        if resp.status_code == 404:
            raise LetterboxdNotFoundError(f"Letterboxd user '{username}' was not found.")
        if resp.status_code >= 400:
            raise LetterboxdError(f"Letterboxd returned HTTP {resp.status_code}.")

        return parse_rss(resp.text, username=username, feed_url=url)
