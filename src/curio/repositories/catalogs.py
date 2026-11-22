"""Catalog, preset, slot, watch-history, and veto persistence using SQLAlchemy 2.0."""

from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    and_,
    column,
    delete,
    exists,
    func,
    insert,
    select,
    table,
    text,
    update,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from curio.db.models.catalogs import (
    CatalogCandidate,
    MediaItem,
    Preset,
    Slot,
    Veto,
    WatchHistory,
)

MediaType = Literal["movie", "series"]
VetoKind = Literal["seen", "not_for_me"]


class UnsetType:
    """Type of `UNSET`: marks a partial-update argument the caller did not supply."""


UNSET = UnsetType()


@dataclass(frozen=True)
class SlotDefinition:
    """Static definition of one of the 6 fixed Stremio catalog slots."""

    slot_id: str
    name: str
    media_type: MediaType


FIXED_SLOTS: tuple[SlotDefinition, ...] = (
    SlotDefinition(slot_id="curio_movie_1", name="Curio Movies 1", media_type="movie"),
    SlotDefinition(slot_id="curio_movie_2", name="Curio Movies 2", media_type="movie"),
    SlotDefinition(slot_id="curio_movie_3", name="Curio Movies 3", media_type="movie"),
    SlotDefinition(slot_id="curio_series_1", name="Curio Series 1", media_type="series"),
    SlotDefinition(slot_id="curio_series_2", name="Curio Series 2", media_type="series"),
    SlotDefinition(slot_id="curio_series_3", name="Curio Series 3", media_type="series"),
)

FIXED_SLOTS_BY_ID: dict[str, SlotDefinition] = {slot.slot_id: slot for slot in FIXED_SLOTS}


def _same_title(a: Any, b: Any) -> ColumnElement[bool]:
    """Join condition on the TMDB title key `(media_type, tmdb_id)`."""
    return and_(a.media_type == b.media_type, a.tmdb_id == b.tmdb_id)


@dataclass(frozen=True)
class SlotRow:
    slot_id: str
    name: str
    media_type: MediaType
    preset_id: UUID | None
    preset_name: str | None


@dataclass(frozen=True)
class CandidateRow:
    media_type: MediaType
    tmdb_id: int
    imdb_id: str | None
    name: str
    year: int | None
    poster_url: str | None
    description: str | None
    rating: float | None
    position: int
    blurb: str | None
    watch_status: str | None
    watch_rating: float | None
    veto_kind: VetoKind | None


@dataclass(frozen=True)
class CatalogItemRow:
    imdb_id: str
    media_type: str
    name: str
    year: int | None
    poster_url: str | None
    description: str | None
    blurb: str | None
    rating: float | None


@dataclass(frozen=True)
class ExcludedTitleRow:
    media_type: str
    tmdb_id: int
    reason: str


@dataclass(frozen=True)
class AntiTasteRow:
    media_type: str
    tmdb_id: int
    reason: str
    rating: float | None


async def list_presets(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> list[Preset]:
    stmt = select(Preset).where(Preset.user_id == user_id).order_by(Preset.created_at, Preset.name)
    return list((await session.execute(stmt)).scalars().all())


async def get_preset(
    session: AsyncSession,
    *,
    user_id: UUID,
    preset_id: UUID,
) -> Preset | None:
    stmt = select(Preset).where(Preset.user_id == user_id, Preset.id == preset_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_preset_by_name(
    session: AsyncSession,
    *,
    user_id: UUID,
    name: str,
    media_type: str,
) -> Preset | None:
    stmt = select(Preset).where(
        Preset.user_id == user_id,
        Preset.name == name,
        Preset.media_type == media_type,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_preset(
    session: AsyncSession,
    *,
    user_id: UUID,
    name: str,
    media_type: str,
    min_rating: float | None = None,
    criteria: str | None = None,
) -> Preset:
    stmt = (
        insert(Preset)
        .values(
            user_id=user_id,
            name=name,
            media_type=media_type,
            min_rating=min_rating,
            criteria=criteria,
        )
        .returning(Preset)
    )
    return (await session.execute(stmt)).scalar_one()


async def update_preset(
    session: AsyncSession,
    *,
    user_id: UUID,
    preset_id: UUID,
    name: str | UnsetType = UNSET,
    min_rating: float | None | UnsetType = UNSET,
    criteria: str | None | UnsetType = UNSET,
) -> Preset | None:
    values: dict[str, object] = {"updated_at": func.now()}
    if not isinstance(name, UnsetType):
        values["name"] = name
    if not isinstance(min_rating, UnsetType):
        values["min_rating"] = min_rating
    if not isinstance(criteria, UnsetType):
        values["criteria"] = criteria

    stmt = (
        update(Preset)
        .where(Preset.user_id == user_id, Preset.id == preset_id)
        .values(**values)
        .returning(Preset)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def delete_preset(
    session: AsyncSession,
    *,
    user_id: UUID,
    preset_id: UUID,
) -> bool:
    stmt = delete(Preset).where(Preset.user_id == user_id, Preset.id == preset_id)
    result = await session.execute(stmt)
    return (result.rowcount or 0) > 0


async def list_slots(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> list[SlotRow]:
    stmt = (
        select(Slot.slot_id, Slot.preset_id, Preset.name.label("preset_name"))
        .select_from(Slot)
        .outerjoin(
            Preset,
            (Slot.user_id == Preset.user_id) & (Slot.preset_id == Preset.id),
        )
        .where(Slot.user_id == user_id)
    )
    rows = {row.slot_id: row for row in (await session.execute(stmt)).all()}
    return [
        SlotRow(
            slot_id=slot_def.slot_id,
            name=slot_def.name,
            media_type=slot_def.media_type,
            preset_id=rows[slot_def.slot_id].preset_id if slot_def.slot_id in rows else None,
            preset_name=rows[slot_def.slot_id].preset_name if slot_def.slot_id in rows else None,
        )
        for slot_def in FIXED_SLOTS
    ]


async def get_slot(
    session: AsyncSession,
    *,
    user_id: UUID,
    slot_id: str,
) -> SlotRow | None:
    slot_def = FIXED_SLOTS_BY_ID.get(slot_id)
    if slot_def is None:
        return None

    stmt = (
        select(Slot.preset_id, Preset.name.label("preset_name"))
        .select_from(Slot)
        .outerjoin(
            Preset,
            (Slot.user_id == Preset.user_id) & (Slot.preset_id == Preset.id),
        )
        .where(Slot.user_id == user_id, Slot.slot_id == slot_id)
    )
    row = (await session.execute(stmt)).one_or_none()
    return SlotRow(
        slot_id=slot_def.slot_id,
        name=slot_def.name,
        media_type=slot_def.media_type,
        preset_id=row.preset_id if row is not None else None,
        preset_name=row.preset_name if row is not None else None,
    )


async def assign_slot(
    session: AsyncSession,
    *,
    user_id: UUID,
    slot_id: str,
    preset_id: UUID | None,
) -> SlotRow:
    slot_def = FIXED_SLOTS_BY_ID[slot_id]
    insert_stmt = postgresql.insert(Slot).values(
        user_id=user_id,
        slot_id=slot_id,
        media_type=slot_def.media_type,
        preset_id=preset_id,
    )
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=[Slot.user_id, Slot.slot_id],
        set_={
            "preset_id": insert_stmt.excluded.preset_id,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)
    slot_row = await get_slot(session, user_id=user_id, slot_id=slot_id)
    assert slot_row is not None
    return slot_row


async def list_preset_candidates(
    session: AsyncSession,
    *,
    user_id: UUID,
    preset_id: UUID,
) -> list[CandidateRow]:
    stmt = (
        select(
            MediaItem.media_type,
            MediaItem.tmdb_id,
            MediaItem.imdb_id,
            MediaItem.name,
            MediaItem.year,
            MediaItem.poster_url,
            MediaItem.description,
            MediaItem.rating,
            CatalogCandidate.position,
            CatalogCandidate.blurb,
            WatchHistory.status.label("watch_status"),
            WatchHistory.rating.label("watch_rating"),
            Veto.kind.label("veto_kind"),
        )
        .select_from(CatalogCandidate)
        .join(MediaItem, _same_title(CatalogCandidate, MediaItem))
        .outerjoin(
            WatchHistory,
            (WatchHistory.user_id == user_id) & _same_title(WatchHistory, CatalogCandidate),
        )
        .outerjoin(
            Veto,
            (Veto.user_id == user_id) & _same_title(Veto, CatalogCandidate),
        )
        .where(
            CatalogCandidate.user_id == user_id,
            CatalogCandidate.preset_id == preset_id,
        )
        .order_by(CatalogCandidate.position, CatalogCandidate.tmdb_id)
    )
    rows = (await session.execute(stmt)).all()
    return [
        CandidateRow(
            media_type=cast(MediaType, row.media_type),
            tmdb_id=row.tmdb_id,
            imdb_id=row.imdb_id,
            name=row.name,
            year=row.year,
            poster_url=row.poster_url,
            description=row.description,
            rating=row.rating,
            position=row.position,
            blurb=row.blurb,
            watch_status=row.watch_status,
            watch_rating=row.watch_rating,
            veto_kind=cast(VetoKind | None, row.veto_kind),
        )
        for row in rows
    ]


async def query_slot_catalog(
    session: AsyncSession,
    *,
    user_id: UUID,
    slot_id: str,
    skip: int = 0,
    limit: int = 20,
) -> list[CatalogItemRow]:
    preset_stmt = (
        select(Preset.id, Preset.min_rating)
        .select_from(Slot)
        .join(
            Preset,
            (Slot.user_id == Preset.user_id) & (Slot.preset_id == Preset.id),
        )
        .where(
            Slot.user_id == user_id,
            Slot.slot_id == slot_id,
            Slot.preset_id.is_not(None),
        )
    )
    preset_row = (await session.execute(preset_stmt)).one_or_none()
    if preset_row is None:
        return []

    excluded_titles = table(
        "excluded_titles", column("user_id"), column("media_type"), column("tmdb_id")
    )
    is_excluded = exists(
        select(1)
        .select_from(excluded_titles)
        .where(
            excluded_titles.c.user_id == user_id,
            _same_title(excluded_titles.c, CatalogCandidate),
        )
    )

    stmt = (
        select(
            MediaItem.imdb_id,
            MediaItem.media_type,
            MediaItem.name,
            MediaItem.year,
            MediaItem.poster_url,
            MediaItem.description,
            CatalogCandidate.blurb,
            MediaItem.rating,
        )
        .select_from(CatalogCandidate)
        .join(MediaItem, _same_title(CatalogCandidate, MediaItem))
        .where(
            CatalogCandidate.user_id == user_id,
            CatalogCandidate.preset_id == preset_row.id,
            # Stremio/Nuvio address items by IMDb ID, so titles without one can't be served.
            MediaItem.imdb_id.is_not(None),
            ~is_excluded,
        )
    )
    if preset_row.min_rating is not None:
        stmt = stmt.where(
            MediaItem.rating.is_not(None),
            MediaItem.rating >= preset_row.min_rating,
        )

    stmt = (
        stmt.order_by(CatalogCandidate.position, CatalogCandidate.tmdb_id).offset(skip).limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        CatalogItemRow(
            imdb_id=cast(str, row.imdb_id),
            media_type=row.media_type,
            name=row.name,
            year=row.year,
            poster_url=row.poster_url,
            description=row.description,
            blurb=row.blurb,
            rating=row.rating,
        )
        for row in rows
    ]


async def upsert_veto(
    session: AsyncSession,
    *,
    user_id: UUID,
    media_type: str,
    tmdb_id: int,
    kind: str,
) -> Veto:
    insert_stmt = postgresql.insert(Veto).values(
        user_id=user_id,
        media_type=media_type,
        tmdb_id=tmdb_id,
        kind=kind,
    )
    stmt = (
        insert_stmt.on_conflict_do_update(
            index_elements=[Veto.user_id, Veto.media_type, Veto.tmdb_id],
            set_={
                "kind": insert_stmt.excluded.kind,
                "updated_at": func.now(),
            },
        )
        .returning(Veto)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one()


async def delete_veto(
    session: AsyncSession,
    *,
    user_id: UUID,
    media_type: str,
    tmdb_id: int,
) -> bool:
    stmt = delete(Veto).where(
        Veto.user_id == user_id,
        Veto.media_type == media_type,
        Veto.tmdb_id == tmdb_id,
    )
    result = await session.execute(stmt)
    return (result.rowcount or 0) > 0


async def list_excluded_titles(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> list[ExcludedTitleRow]:
    stmt = text(
        "SELECT media_type, tmdb_id, reason FROM excluded_titles "
        "WHERE user_id = :user_id ORDER BY media_type, tmdb_id, reason"
    )
    rows = (await session.execute(stmt, {"user_id": user_id})).all()
    return [
        ExcludedTitleRow(media_type=row.media_type, tmdb_id=row.tmdb_id, reason=row.reason)
        for row in rows
    ]


async def list_anti_taste(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> list[AntiTasteRow]:
    stmt = text(
        "SELECT media_type, tmdb_id, reason, rating FROM anti_taste "
        "WHERE user_id = :user_id ORDER BY media_type, tmdb_id, reason"
    )
    rows = (await session.execute(stmt, {"user_id": user_id})).all()
    return [
        AntiTasteRow(
            media_type=row.media_type,
            tmdb_id=row.tmdb_id,
            reason=row.reason,
            rating=row.rating,
        )
        for row in rows
    ]


async def media_item_exists(
    session: AsyncSession,
    *,
    media_type: str,
    tmdb_id: int,
) -> bool:
    stmt = (
        select(1)
        .select_from(MediaItem)
        .where(MediaItem.media_type == media_type, MediaItem.tmdb_id == tmdb_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def insert_media_items_ignore_conflicts(
    session: AsyncSession,
    rows: list[dict[str, object]],
) -> None:
    if not rows:
        return
    stmt = (
        postgresql.insert(MediaItem)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["media_type", "tmdb_id"])
    )
    await session.execute(stmt)


async def insert_slots_ignore_conflicts(
    session: AsyncSession,
    rows: list[dict[str, object]],
) -> None:
    if not rows:
        return
    stmt = (
        postgresql.insert(Slot)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["user_id", "slot_id"])
    )
    await session.execute(stmt)


async def insert_candidates_ignore_conflicts(
    session: AsyncSession,
    rows: list[dict[str, object]],
) -> None:
    if not rows:
        return
    stmt = (
        postgresql.insert(CatalogCandidate)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["user_id", "preset_id", "media_type", "tmdb_id"])
    )
    await session.execute(stmt)


async def insert_watch_history_ignore_conflicts(
    session: AsyncSession,
    rows: list[dict[str, object]],
) -> None:
    if not rows:
        return
    stmt = (
        postgresql.insert(WatchHistory)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["user_id", "media_type", "tmdb_id"])
    )
    await session.execute(stmt)


async def insert_vetoes_ignore_conflicts(
    session: AsyncSession,
    rows: list[dict[str, object]],
) -> None:
    if not rows:
        return
    stmt = (
        postgresql.insert(Veto)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["user_id", "media_type", "tmdb_id"])
    )
    await session.execute(stmt)
