"""Catalog, preset, slot, watch-history, and veto persistence using SQLAlchemy 2.0."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, exists, func, insert, select, text, update
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


@dataclass(frozen=True)
class SlotDefinition:
    """Static definition of one of the 6 fixed Stremio catalog slots."""

    slot_id: str
    name: str
    media_type: str


FIXED_SLOTS: tuple[SlotDefinition, ...] = (
    SlotDefinition(slot_id="curio_movie_1", name="Curio Movies 1", media_type="movie"),
    SlotDefinition(slot_id="curio_movie_2", name="Curio Movies 2", media_type="movie"),
    SlotDefinition(slot_id="curio_movie_3", name="Curio Movies 3", media_type="movie"),
    SlotDefinition(slot_id="curio_series_1", name="Curio Series 1", media_type="series"),
    SlotDefinition(slot_id="curio_series_2", name="Curio Series 2", media_type="series"),
    SlotDefinition(slot_id="curio_series_3", name="Curio Series 3", media_type="series"),
)

FIXED_SLOTS_BY_ID: dict[str, SlotDefinition] = {slot.slot_id: slot for slot in FIXED_SLOTS}


@dataclass(frozen=True)
class SlotRow:
    slot_id: str
    name: str
    media_type: str
    preset_id: UUID | None
    preset_name: str | None


@dataclass(frozen=True)
class CandidateRow:
    imdb_id: str
    media_type: str
    name: str
    year: int | None
    poster_url: str | None
    description: str | None
    rating: float | None
    tmdb_id: int | None
    position: int
    blurb: str | None
    watch_status: str | None
    watch_rating: float | None
    veto_kind: str | None


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
    imdb_id: str
    reason: str


@dataclass(frozen=True)
class AntiTasteRow:
    imdb_id: str
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
    name: str,
    media_type: str | None = None,
    min_rating: float | None = None,
    criteria: str | None = None,
) -> Preset | None:
    values: dict[str, object] = {
        "name": name,
        "min_rating": min_rating,
        "criteria": criteria,
        "updated_at": func.now(),
    }
    if media_type is not None:
        values["media_type"] = media_type

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
            CatalogCandidate.imdb_id,
            MediaItem.media_type,
            MediaItem.name,
            MediaItem.year,
            MediaItem.poster_url,
            MediaItem.description,
            MediaItem.rating,
            MediaItem.tmdb_id,
            CatalogCandidate.position,
            CatalogCandidate.blurb,
            WatchHistory.status.label("watch_status"),
            WatchHistory.rating.label("watch_rating"),
            Veto.kind.label("veto_kind"),
        )
        .select_from(CatalogCandidate)
        .join(MediaItem, CatalogCandidate.imdb_id == MediaItem.imdb_id)
        .outerjoin(
            WatchHistory,
            (WatchHistory.user_id == user_id) & (WatchHistory.imdb_id == CatalogCandidate.imdb_id),
        )
        .outerjoin(
            Veto,
            (Veto.user_id == user_id) & (Veto.imdb_id == CatalogCandidate.imdb_id),
        )
        .where(
            CatalogCandidate.user_id == user_id,
            CatalogCandidate.preset_id == preset_id,
        )
        .order_by(CatalogCandidate.position, CatalogCandidate.imdb_id)
    )
    rows = (await session.execute(stmt)).all()
    return [
        CandidateRow(
            imdb_id=row.imdb_id,
            media_type=row.media_type,
            name=row.name,
            year=row.year,
            poster_url=row.poster_url,
            description=row.description,
            rating=row.rating,
            tmdb_id=row.tmdb_id,
            position=row.position,
            blurb=row.blurb,
            watch_status=row.watch_status,
            watch_rating=row.watch_rating,
            veto_kind=row.veto_kind,
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

    excluded_watch = exists(
        select(1)
        .select_from(WatchHistory)
        .where(
            WatchHistory.user_id == user_id,
            WatchHistory.imdb_id == CatalogCandidate.imdb_id,
            WatchHistory.status.in_(("watched", "dropped")),
        )
    )
    excluded_veto = exists(
        select(1)
        .select_from(Veto)
        .where(
            Veto.user_id == user_id,
            Veto.imdb_id == CatalogCandidate.imdb_id,
            Veto.kind.in_(("seen", "not_for_me")),
        )
    )

    stmt = (
        select(
            CatalogCandidate.imdb_id,
            MediaItem.media_type,
            MediaItem.name,
            MediaItem.year,
            MediaItem.poster_url,
            MediaItem.description,
            CatalogCandidate.blurb,
            MediaItem.rating,
        )
        .select_from(CatalogCandidate)
        .join(MediaItem, CatalogCandidate.imdb_id == MediaItem.imdb_id)
        .where(
            CatalogCandidate.user_id == user_id,
            CatalogCandidate.preset_id == preset_row.id,
            ~excluded_watch,
            ~excluded_veto,
        )
    )
    if preset_row.min_rating is not None:
        stmt = stmt.where(
            MediaItem.rating.is_not(None),
            MediaItem.rating >= preset_row.min_rating,
        )

    stmt = (
        stmt.order_by(CatalogCandidate.position, CatalogCandidate.imdb_id).offset(skip).limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        CatalogItemRow(
            imdb_id=row.imdb_id,
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
    imdb_id: str,
    kind: str,
) -> Veto:
    insert_stmt = postgresql.insert(Veto).values(
        user_id=user_id,
        imdb_id=imdb_id,
        kind=kind,
    )
    stmt = (
        insert_stmt.on_conflict_do_update(
            index_elements=[Veto.user_id, Veto.imdb_id],
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
    imdb_id: str,
) -> bool:
    stmt = delete(Veto).where(Veto.user_id == user_id, Veto.imdb_id == imdb_id)
    result = await session.execute(stmt)
    return (result.rowcount or 0) > 0


async def list_excluded_titles(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> list[ExcludedTitleRow]:
    stmt = text(
        "SELECT imdb_id, reason FROM excluded_titles "
        "WHERE user_id = :user_id ORDER BY imdb_id, reason"
    )
    rows = (await session.execute(stmt, {"user_id": user_id})).all()
    return [ExcludedTitleRow(imdb_id=row.imdb_id, reason=row.reason) for row in rows]


async def list_anti_taste(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> list[AntiTasteRow]:
    stmt = text(
        "SELECT imdb_id, reason, rating FROM anti_taste "
        "WHERE user_id = :user_id ORDER BY imdb_id, reason"
    )
    rows = (await session.execute(stmt, {"user_id": user_id})).all()
    return [AntiTasteRow(imdb_id=row.imdb_id, reason=row.reason, rating=row.rating) for row in rows]


async def media_item_exists(
    session: AsyncSession,
    *,
    imdb_id: str,
) -> bool:
    stmt = select(1).select_from(MediaItem).where(MediaItem.imdb_id == imdb_id)
    return (await session.execute(stmt)).scalar_one_or_none() is not None
