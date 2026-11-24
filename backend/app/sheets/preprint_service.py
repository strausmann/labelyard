# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Preprint keep-out zones and presets, and the position rule of the margin sheet code (#246).

ADR 0023, D33. A sheet type keeps its margin code off preprinted areas: its own zones and those
of a linked preset count together (a union). Every write is checked here (patch, preset write,
import): at most ``MAX_ZONES`` zones, each at least 1 mm wide and tall, on the page, and (for a
type) over no label cell; the sheet code's minimal box (4 x 10 mm) at the requested position lies
on the page and off every cell. Preset writes take the sheet write lock, so a delete cannot race a
type that links the preset.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.models.preprint_preset import PreprintPreset
from app.models.sheet import SheetType
from app.repositories.write_lock import write_transaction
from app.sheets.errors import (
    IdPositionOffPageError,
    IdPositionOverlapsCellError,
    InvalidFieldError,
    PreprintPresetExistsError,
    PreprintPresetInUseError,
    PreprintPresetNotFoundError,
    PreprintZoneOffPageError,
    PreprintZoneOverlapsCellError,
)
from app.sheets.exchange import (
    MAX_ZONES,
    ZONE_NOTE_MAX,
    PreprintPresetDoc,
    PreprintZoneMm,
)
from app.sheets.free_rects import Rect
from app.sheets.geometry import PAGE_SIZES, Geometry, cell_origin, hmm_to_mm, mm_to_hmm
from app.sheets.naming import KEY_PATTERN
from app.sheets.planner import (
    DEFAULT_HW_HMM,
    MIN_MARGIN_TEXT_HMM,
    MIN_MARGIN_TEXT_RUN_HMM,
    MarginBandRule,
    MarginRule,
)
from app.sheets.text import log_value
from app.sheets.type_service import clean_text

__all__ = [
    "BANDS",
    "MIN_ZONE_HMM",
    "check_id_position",
    "check_zones",
    "create",
    "create_locked",
    "delete",
    "effective_zones",
    "get",
    "list_with_usage",
    "margin_rule_of",
    "margin_rules",
    "patch",
    "to_doc",
    "types_in_use",
    "zones_from_mm",
    "zones_to_mm",
]

logger = logging.getLogger(__name__)

BANDS: frozenset[str] = frozenset({"auto", "top", "bottom", "left", "right"})
MIN_ZONE_HMM = 100  # 1 mm
NAME_MAX = 200


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------- zones


def zones_from_mm(zones: Sequence[PreprintZoneMm], *, field: str) -> list[dict[str, Any]]:
    """Millimetres (two decimals at most) to stored hundredths; the note is cleaned text."""
    out: list[dict[str, Any]] = []
    for i, z in enumerate(zones):
        note = None if z.note is None else clean_text(f"{field}.{i}.note", z.note, ZONE_NOTE_MAX)
        out.append(
            {
                "x": mm_to_hmm(z.x_mm, field=f"{field}.{i}.x_mm"),
                "y": mm_to_hmm(z.y_mm, field=f"{field}.{i}.y_mm"),
                "w": mm_to_hmm(z.w_mm, field=f"{field}.{i}.w_mm"),
                "h": mm_to_hmm(z.h_mm, field=f"{field}.{i}.h_mm"),
                "note": note or None,
            }
        )
    return out


def zones_to_mm(zones: Iterable[dict[str, Any]]) -> list[PreprintZoneMm]:
    return [
        PreprintZoneMm(
            x_mm=hmm_to_mm(int(z["x"])),
            y_mm=hmm_to_mm(int(z["y"])),
            w_mm=hmm_to_mm(int(z["w"])),
            h_mm=hmm_to_mm(int(z["h"])),
            note=z.get("note"),
        )
        for z in zones
    ]


def _rect(z: dict[str, Any]) -> Rect:
    return Rect(int(z["x"]), int(z["y"]), int(z["w"]), int(z["h"]))


def _cells(g: Geometry) -> list[Rect]:
    return [Rect(*cell_origin(g, n), g.label_w, g.label_h) for n in range(1, g.cols * g.rows + 1)]


def check_zones(
    zones: Sequence[dict[str, Any]],
    *,
    page_w: int,
    page_h: int,
    geometry: Geometry | None,
    field: str,
) -> None:
    """Refuse a zone list the page cannot hold, or (with ``geometry``) that covers a cell.

    422 ``invalid_geometry`` for more than 16 zones or a zone below 1 mm, 422
    ``preprint_zone_off_page`` for a zone outside the page, 422 ``preprint_zone_overlaps_cell``
    for one over a label cell (vendor preprint is in the margins: such a zone is a typo, spec 5.3).
    """
    if len(zones) > MAX_ZONES:
        raise InvalidFieldError(field, "too many")
    cells = [] if geometry is None else _cells(geometry)
    for i, z in enumerate(zones):
        r = _rect(z)
        if r.w < MIN_ZONE_HMM or r.h < MIN_ZONE_HMM:
            raise InvalidFieldError(f"{field}.{i}", "smaller than 1 mm")
        if r.x < 0 or r.y < 0 or r.x2 > page_w or r.y2 > page_h:
            raise PreprintZoneOffPageError(f"{field}.{i}")
        if any(r.overlaps(c) for c in cells):
            raise PreprintZoneOverlapsCellError(f"{field}.{i}")


def _minimal_box(
    g: Geometry, band: str, align: Literal["start", "end"], offset_hmm: int
) -> Rect | None:
    """The sheet code's smallest box (4 x 10 mm) in ``band`` at the requested position.

    Across the band it starts at the assumed 5 mm hardware margin (the edge the printer reaches);
    along it at the grid's start plus ``offset_hmm`` (``align`` ``end``: ending at the grid's end
    minus it). ``None`` for ``auto`` (no band is named).
    """
    deep, long = MIN_MARGIN_TEXT_HMM, MIN_MARGIN_TEXT_RUN_HMM
    grid_right = g.margin_left + (g.cols - 1) * g.pitch_x + g.label_w
    grid_bottom = g.margin_top + (g.rows - 1) * g.pitch_y + g.label_h
    if band in ("top", "bottom"):
        x = g.margin_left + offset_hmm if align == "start" else grid_right - offset_hmm - long
        y = DEFAULT_HW_HMM if band == "top" else g.page_h - DEFAULT_HW_HMM - deep
        return Rect(x, y, long, deep)
    if band in ("left", "right"):
        y = g.margin_top + offset_hmm if align == "start" else grid_bottom - offset_hmm - long
        x = DEFAULT_HW_HMM if band == "left" else g.page_w - DEFAULT_HW_HMM - deep
        return Rect(x, y, deep, long)
    return None


def check_id_position(
    g: Geometry, *, band: str, align: Literal["start", "end"], offset_hmm: int
) -> None:
    """Refuse a position whose minimal box leaves the page or covers a cell (spec 5.3).

    Only a configured position is checked (a band, ``end`` or an offset): the default (``auto``
    from the start, no offset) never refuses a type, so every geometry 1.7.x accepted stays
    accepted, on writes and on import alike; the planner then finds a place or none, as before
    (Codex review on #439). With ``auto`` and an offset the box is checked in the top band for
    the page only: ``auto`` falls back to another band, so a cell there is not a reason to refuse.
    """
    if band == "auto" and align == "start" and offset_hmm == 0:
        return
    box = _minimal_box(g, "top" if band == "auto" else band, align, offset_hmm)
    if box is None:  # unreachable: every band name has a box
        return
    if box.x < 0 or box.y < 0 or box.x2 > g.page_w or box.y2 > g.page_h:
        raise IdPositionOffPageError()
    if band != "auto" and any(box.overlaps(c) for c in _cells(g)):
        raise IdPositionOverlapsCellError()


def effective_zones(t: SheetType, preset: PreprintPreset | None) -> list[Rect]:
    """The zones the placement keeps clear: the preset's and the type's own, a union (spec 5.1)."""
    own = [_rect(z) for z in (t.preprint_zones or [])]
    shared = [] if preset is None else [_rect(z) for z in (preset.zones or [])]
    return shared + own


def margin_rule_of(t: SheetType, preset: PreprintPreset | None) -> MarginRule:
    """The placement rule of a sheet type for the planner."""
    return MarginRule(
        zones=tuple(effective_zones(t, preset)),
        band=cast(MarginBandRule, t.id_margin_band or "auto"),
        align=cast(Literal["start", "end"], t.id_margin_align or "start"),
        offset_hmm=int(t.id_margin_offset_hmm or 0),
    )


async def margin_rules(session: AsyncSession, types: Iterable[SheetType]) -> dict[UUID, MarginRule]:
    """``margin_rule_of`` for several types, their presets read in one statement."""
    types = list(types)
    ids = {t.preprint_preset_id for t in types if t.preprint_preset_id is not None}
    presets: dict[UUID, PreprintPreset] = {}
    if ids:
        rows = await session.execute(select(PreprintPreset).where(col(PreprintPreset.id).in_(ids)))
        presets = {p.id: p for p in rows.scalars()}
    return {
        t.id: margin_rule_of(
            t, None if t.preprint_preset_id is None else presets.get(t.preprint_preset_id)
        )
        for t in types
    }


# ---------------------------------------------------------------------------- presets


def to_doc(preset: PreprintPreset) -> PreprintPresetDoc:
    return PreprintPresetDoc(
        key=preset.key,
        name=preset.name,
        page_size=preset.page_size,  # type: ignore[arg-type]
        zones=zones_to_mm(preset.zones),
    )


def _clean_name(name: str) -> str:
    return clean_text("name", name, NAME_MAX, required=True)


def _check_preset_zones(zones: Sequence[dict[str, Any]], page_size: str) -> None:
    page_w, page_h = PAGE_SIZES[page_size]
    check_zones(zones, page_w=page_w, page_h=page_h, geometry=None, field="zones")


async def get(session: AsyncSession, key: str) -> PreprintPreset:
    found = (
        await session.execute(select(PreprintPreset).where(col(PreprintPreset.key) == key))
    ).scalar_one_or_none()
    if found is None:
        raise PreprintPresetNotFoundError()
    return found


async def types_in_use(session: AsyncSession, preset_id: Any) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(SheetType)
        .where(col(SheetType.preprint_preset_id) == preset_id)
    )
    return int(result.scalar_one())


async def list_with_usage(session: AsyncSession) -> list[tuple[PreprintPreset, int]]:
    """Every preset by key, with the number of sheet types that link it."""
    presets = list(
        (await session.execute(select(PreprintPreset).order_by(col(PreprintPreset.key)))).scalars()
    )
    counts = dict(
        (
            await session.execute(
                select(col(SheetType.preprint_preset_id), func.count())
                .where(col(SheetType.preprint_preset_id).is_not(None))
                .group_by(col(SheetType.preprint_preset_id))
            )
        ).all()
    )
    return [(p, int(counts.get(p.id, 0))) for p in presets]


async def create_locked(
    session: AsyncSession,
    *,
    key: str,
    name: str,
    page_size: str,
    zones: list[dict[str, Any]],
    origin: Literal["seed", "custom", "imported"],
) -> PreprintPreset:
    """Insert a preset inside a transaction that already holds the write lock."""
    if not KEY_PATTERN.fullmatch(key):
        raise InvalidFieldError("key", "invalid format")
    if page_size not in PAGE_SIZES:
        raise InvalidFieldError("page_size", "unknown page size")
    _check_preset_zones(zones, page_size)
    clean = _clean_name(name)
    exists = (
        await session.execute(select(col(PreprintPreset.id)).where(col(PreprintPreset.key) == key))
    ).first()
    if exists is not None:
        raise PreprintPresetExistsError()
    preset = PreprintPreset(key=key, name=clean, page_size=page_size, zones=zones, origin=origin)
    session.add(preset)
    await session.flush()
    return preset


async def create(
    session: AsyncSession, *, key: str, name: str, page_size: str, zones: list[dict[str, Any]]
) -> PreprintPreset:
    if page_size in PAGE_SIZES:
        _check_preset_zones(zones, page_size)  # cheap refusals before the lock
    async with write_transaction(session):
        preset = await create_locked(
            session, key=key, name=name, page_size=page_size, zones=zones, origin="custom"
        )
        await session.commit()
    logger.info("preprint preset %s created", log_value(key))
    return preset


async def patch(session: AsyncSession, key: str, changes: dict[str, Any]) -> PreprintPreset:
    """Change the name or the zones; the page size of a preset is fixed.

    New zones are checked against the cells of every type that links the preset, so a change
    cannot put a linked type's labels on the preprint.
    """
    from app.sheets.type_service import current_version, version_geometry  # deferred cycle

    async with write_transaction(session):
        preset = await get(session, key)
        if "name" in changes:
            preset.name = _clean_name(changes["name"])
        if "zones" in changes:
            zones = list(changes["zones"])
            _check_preset_zones(zones, preset.page_size)
            linked = (
                await session.execute(
                    select(SheetType).where(col(SheetType.preprint_preset_id) == preset.id)
                )
            ).scalars()
            for t in linked:
                g = version_geometry(await current_version(session, t.id))
                check_zones(zones, page_w=g.page_w, page_h=g.page_h, geometry=g, field="zones")
            preset.zones = zones
        preset.updated_at = _now()
        await session.commit()
    logger.info("preprint preset %s edited", log_value(key))
    return preset


async def delete(session: AsyncSession, key: str) -> None:
    """Delete a preset no sheet type links (409 ``preset_in_use`` otherwise)."""
    async with write_transaction(session):
        preset = await get(session, key)
        used = await types_in_use(session, preset.id)
        if used:
            raise PreprintPresetInUseError(used)
        await session.delete(preset)
        await session.commit()
    logger.info("preprint preset %s deleted", log_value(key))
