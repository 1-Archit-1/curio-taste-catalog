"""Catalog, preset, slot, veto, and Stremio addon protocol business logic."""

import logging
from collections.abc import Sequence
from urllib.parse import unquote
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.db.models.catalogs import Preset, Veto
from curio.repositories import catalogs as catalogs_repo
from curio.repositories.catalogs import (
    FIXED_SLOTS,
    FIXED_SLOTS_BY_ID,
    UNSET,
    AntiTasteRow,
    CandidateRow,
    CatalogItemRow,
    ExcludedTitleRow,
    SlotDefinition,
    SlotRow,
    UnsetType,
)

VALID_MEDIA_TYPES = frozenset({"movie", "series"})
VALID_VETO_KINDS = frozenset({"seen", "not_for_me"})
log = logging.getLogger(__name__)

__all__ = [
    "FIXED_SLOTS",
    "FIXED_SLOTS_BY_ID",
    "Preset",
    "SlotDefinition",
    "SlotRow",
    "assign_slot",
    "create_preset",
    "delete_preset",
    "delete_veto",
    "get_preset",
    "get_slot",
    "list_anti_taste",
    "list_excluded_titles",
    "list_preset_candidates",
    "list_presets",
    "list_slots",
    "parse_skip_extra",
    "query_slot_catalog",
    "record_served_titles_best_effort",
    "resolve_poster_url",
    "update_preset",
    "upsert_veto",
]


def _require_slot(slot_id: str) -> SlotDefinition:
    slot_def = FIXED_SLOTS_BY_ID.get(slot_id)
    if slot_def is None:
        raise ValueError(f"unknown slot '{slot_id}'")
    return slot_def


def parse_skip_extra(extra: str | None, query_skip: int | None = None) -> int:
    """Parse Stremio `extra` path segment (e.g. `skip=20` or `genre=Drama&skip=20`).

    Falls back to `query_skip` when `extra` has no `skip` key, and clamps to `>= 0`.
    Raises `ValueError` if `skip` is present in `extra` with a non-integer value.
    """
    if extra:
        decoded = unquote(extra)
        for part in decoded.split("&"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key == "skip":
                try:
                    parsed = int(value)
                except ValueError as exc:
                    raise ValueError(f"invalid skip value: {value!r}") from exc
                return max(0, parsed)

    if query_skip is not None:
        return max(0, query_skip)
    return 0


def resolve_poster_url(poster_url: str | None, *, base_url: str) -> str:
    """Return `poster_url` if non-empty, otherwise the local SVG fallback URL."""
    if poster_url and poster_url.strip():
        return poster_url
    return f"{base_url.rstrip('/')}/addon-assets/poster-fallback.svg"


async def list_presets(session: AsyncSession, *, user_id: UUID) -> list[Preset]:
    return await catalogs_repo.list_presets(session, user_id=user_id)


async def get_preset(
    session: AsyncSession,
    *,
    user_id: UUID,
    preset_id: UUID,
) -> Preset | None:
    return await catalogs_repo.get_preset(session, user_id=user_id, preset_id=preset_id)


async def create_preset(
    session: AsyncSession,
    *,
    user_id: UUID,
    name: str,
    media_type: str,
    min_rating: float | None = None,
    criteria: str | None = None,
) -> Preset:
    if media_type not in VALID_MEDIA_TYPES:
        raise ValueError(f"invalid media_type '{media_type}'")
    cleaned_name = name.strip()
    if not cleaned_name:
        raise ValueError("preset name cannot be empty")
    return await catalogs_repo.create_preset(
        session,
        user_id=user_id,
        name=cleaned_name,
        media_type=media_type,
        min_rating=min_rating,
        criteria=criteria,
    )


async def update_preset(
    session: AsyncSession,
    *,
    user_id: UUID,
    preset_id: UUID,
    name: str | UnsetType = UNSET,
    min_rating: float | None | UnsetType = UNSET,
    criteria: str | None | UnsetType = UNSET,
) -> Preset | None:
    if not isinstance(name, UnsetType):
        name = name.strip()
        if not name:
            raise ValueError("preset name cannot be empty")
    return await catalogs_repo.update_preset(
        session,
        user_id=user_id,
        preset_id=preset_id,
        name=name,
        min_rating=min_rating,
        criteria=criteria,
    )


async def delete_preset(
    session: AsyncSession,
    *,
    user_id: UUID,
    preset_id: UUID,
) -> bool:
    return await catalogs_repo.delete_preset(session, user_id=user_id, preset_id=preset_id)


async def list_slots(session: AsyncSession, *, user_id: UUID) -> list[SlotRow]:
    return await catalogs_repo.list_slots(session, user_id=user_id)


async def get_slot(
    session: AsyncSession,
    *,
    user_id: UUID,
    slot_id: str,
) -> SlotRow:
    _require_slot(slot_id)
    slot = await catalogs_repo.get_slot(session, user_id=user_id, slot_id=slot_id)
    assert slot is not None
    return slot


async def assign_slot(
    session: AsyncSession,
    *,
    user_id: UUID,
    slot_id: str,
    preset_id: UUID | None,
) -> SlotRow:
    slot_def = _require_slot(slot_id)
    if preset_id is not None:
        preset = await catalogs_repo.get_preset(session, user_id=user_id, preset_id=preset_id)
        if preset is None:
            raise ValueError(f"preset '{preset_id}' not found")
        if preset.media_type != slot_def.media_type:
            raise ValueError(
                f"preset media_type '{preset.media_type}' does not match "
                f"slot '{slot_id}' media_type '{slot_def.media_type}'"
            )
    return await catalogs_repo.assign_slot(
        session,
        user_id=user_id,
        slot_id=slot_id,
        preset_id=preset_id,
    )


async def list_preset_candidates(
    session: AsyncSession,
    *,
    user_id: UUID,
    preset_id: UUID,
) -> list[CandidateRow]:
    return await catalogs_repo.list_preset_candidates(
        session,
        user_id=user_id,
        preset_id=preset_id,
    )


async def query_slot_catalog(
    session: AsyncSession,
    *,
    user_id: UUID,
    slot_id: str,
    skip: int = 0,
    limit: int = 20,
) -> list[CatalogItemRow]:
    _require_slot(slot_id)
    return await catalogs_repo.query_slot_catalog(
        session,
        user_id=user_id,
        slot_id=slot_id,
        skip=max(0, skip),
        limit=limit,
    )


async def upsert_veto(
    session: AsyncSession,
    *,
    user_id: UUID,
    media_type: str,
    tmdb_id: int,
    kind: str,
) -> Veto:
    if kind not in VALID_VETO_KINDS:
        raise ValueError(f"invalid veto kind '{kind}'")
    if not await catalogs_repo.media_item_exists(session, media_type=media_type, tmdb_id=tmdb_id):
        raise LookupError(f"media item '{media_type}/{tmdb_id}' not found")
    return await catalogs_repo.upsert_veto(
        session,
        user_id=user_id,
        media_type=media_type,
        tmdb_id=tmdb_id,
        kind=kind,
    )


async def delete_veto(
    session: AsyncSession,
    *,
    user_id: UUID,
    media_type: str,
    tmdb_id: int,
) -> bool:
    return await catalogs_repo.delete_veto(
        session, user_id=user_id, media_type=media_type, tmdb_id=tmdb_id
    )


async def list_excluded_titles(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> list[ExcludedTitleRow]:
    return await catalogs_repo.list_excluded_titles(session, user_id=user_id)


async def list_anti_taste(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> list[AntiTasteRow]:
    return await catalogs_repo.list_anti_taste(session, user_id=user_id)


async def record_served_titles_best_effort(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    items: Sequence[tuple[UUID, str, int]],
) -> None:
    """Persist served-title counts on a fresh session, logging and swallowing errors."""
    if not items:
        return
    try:
        async with session_factory() as session:
            await catalogs_repo.record_served_titles(session, user_id=user_id, items=items)
            await session.commit()
    except Exception:
        log.warning("failed to record served titles for user %s", user_id, exc_info=True)
