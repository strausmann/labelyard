# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Queries for sheet types and their versions (spec 7.1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.models.sheet import Sheet, SheetCell, SheetType, SheetTypeVersion

UsageState = Literal["in_use", "used_before", "unused"]


async def get_by_key(session: AsyncSession, key: str) -> SheetType | None:
    result = await session.execute(select(SheetType).where(col(SheetType.key) == key))
    return result.scalar_one_or_none()


async def get_by_prefix(session: AsyncSession, prefix: str) -> SheetType | None:
    result = await session.execute(select(SheetType).where(col(SheetType.prefix) == prefix))
    return result.scalar_one_or_none()


async def current_version(session: AsyncSession, type_id: UUID) -> SheetTypeVersion | None:
    result = await session.execute(
        select(SheetTypeVersion)
        .where(col(SheetTypeVersion.type_id) == type_id)
        .order_by(col(SheetTypeVersion.version).desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_version(session: AsyncSession, type_id: UUID, n: int) -> SheetTypeVersion | None:
    result = await session.execute(
        select(SheetTypeVersion).where(
            col(SheetTypeVersion.type_id) == type_id, col(SheetTypeVersion.version) == n
        )
    )
    return result.scalar_one_or_none()


async def versions_with_sheet_counts_many(
    session: AsyncSession, type_ids: list[UUID]
) -> dict[UUID, list[tuple[SheetTypeVersion, int]]]:
    """Versions of all given types, oldest first, with the sheets (any state) pinned to each.

    One query for any number of types.
    """
    grouped: dict[UUID, list[tuple[SheetTypeVersion, int]]] = {tid: [] for tid in type_ids}
    if not type_ids:
        return grouped
    counts = (
        select(col(Sheet.type_version_id).label("vid"), func.count().label("n"))
        .group_by(col(Sheet.type_version_id))
        .subquery()
    )
    result = await session.execute(
        select(SheetTypeVersion, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.vid == col(SheetTypeVersion.id))
        .where(col(SheetTypeVersion.type_id).in_(type_ids))
        .order_by(col(SheetTypeVersion.type_id), col(SheetTypeVersion.version))
    )
    for v, n in result.all():
        grouped[v.type_id].append((v, int(n)))
    return grouped


async def versions_with_sheet_counts(
    session: AsyncSession, type_id: UUID
) -> list[tuple[SheetTypeVersion, int]]:
    """Every version, oldest first, with the number of sheets (any state) pinned to it."""
    return (await versions_with_sheet_counts_many(session, [type_id]))[type_id]


async def count_sheets(session: AsyncSession, type_id: UUID) -> int:
    result = await session.execute(
        select(func.count()).select_from(Sheet).where(col(Sheet.type_id) == type_id)
    )
    return int(result.scalar_one())


async def list_with_current_version(
    session: AsyncSession, *, vendor: str | None, include_archived: bool
) -> list[tuple[SheetType, SheetTypeVersion]]:
    """Types paired with their newest version; free-text and size filters live in the service."""
    newest = (
        select(
            col(SheetTypeVersion.type_id).label("tid"),
            func.max(col(SheetTypeVersion.version)).label("v"),
        )
        .group_by(col(SheetTypeVersion.type_id))
        .subquery()
    )
    stmt = (
        select(SheetType, SheetTypeVersion)
        .join(newest, newest.c.tid == col(SheetType.id))
        .join(
            SheetTypeVersion,
            (col(SheetTypeVersion.type_id) == col(SheetType.id))
            & (col(SheetTypeVersion.version) == newest.c.v),
        )
        .order_by(col(SheetType.key))
    )
    if vendor is not None:
        stmt = stmt.where(func.lower(col(SheetType.vendor)) == vendor.lower())
    if not include_archived:
        stmt = stmt.where(col(SheetType.archived_at).is_(None))
    result = await session.execute(stmt)
    return [(t, v) for t, v in result.all()]


@dataclass(frozen=True)
class TypeUsage:
    """What the sheets of one type amount to (spec 6.1.5)."""

    sheets_with_free_cells: int = 0  # active sheets with at least one free cell
    free_labels: int = 0  # the free cells on those sheets
    sheets_total: int = 0  # sheets in any state; purged sheets are gone and not counted
    last_used_at: datetime | None = None  # the newest ``sheets.last_used_at``

    @property
    def state(self) -> UsageState:
        if self.sheets_with_free_cells > 0:
            return "in_use"
        return "used_before" if self.sheets_total > 0 else "unused"


NO_USAGE = TypeUsage()


def _taken_cells() -> Any:
    """Non-free cells of the outer sheet: a lookup on the ``(sheet_id, cell_no)`` key per sheet."""
    return (
        select(func.count())
        .select_from(SheetCell)
        .where(
            col(SheetCell.sheet_id) == col(Sheet.id),
            col(SheetCell.state).in_(("used", "reserved", "unconfirmed")),
        )
        .correlate(Sheet)
        .scalar_subquery()
    )


async def usage_by_type(
    session: AsyncSession, type_ids: list[UUID] | None = None
) -> dict[UUID, TypeUsage]:
    """Usage of every type that has sheets (or of ``type_ids``), in **one** statement.

    The free cells of a sheet are the cells of its pinned version minus the used, reserved and
    unconfirmed ones, the rule of the sheet list. A type without sheets is not in the answer;
    ``NO_USAGE`` stands for it.
    """
    free = col(SheetTypeVersion.cols) * col(SheetTypeVersion.rows) - _taken_cells()
    per_sheet = select(
        col(Sheet.type_id).label("type_id"),
        col(Sheet.state).label("state"),
        col(Sheet.last_used_at).label("last_used_at"),
        free.label("free"),
    ).join(SheetTypeVersion, col(SheetTypeVersion.id) == col(Sheet.type_version_id))
    if type_ids is not None:
        if not type_ids:
            return {}
        per_sheet = per_sheet.where(col(Sheet.type_id).in_(type_ids))
    sub = per_sheet.subquery()
    open_sheet = and_(sub.c.state == "active", sub.c.free > 0)
    stmt = select(
        sub.c.type_id,
        func.count(),
        func.coalesce(func.sum(case((open_sheet, 1), else_=0)), 0),
        func.coalesce(func.sum(case((open_sheet, sub.c.free), else_=0)), 0),
        func.max(sub.c.last_used_at),
    ).group_by(sub.c.type_id)
    out: dict[UUID, TypeUsage] = {}
    for type_id, total, open_sheets, free_labels, last_used in (await session.execute(stmt)).all():
        out[type_id] = TypeUsage(
            sheets_with_free_cells=int(open_sheets),
            free_labels=int(free_labels),
            sheets_total=int(total),
            last_used_at=last_used,
        )
    return out


async def sheet_counts(session: AsyncSession, type_ids: list[UUID]) -> dict[UUID, int]:
    """Sheets of any state per type, for the types that have any (one statement)."""
    if not type_ids:
        return {}
    result = await session.execute(
        select(col(Sheet.type_id), func.count())
        .where(col(Sheet.type_id).in_(type_ids))
        .group_by(col(Sheet.type_id))
    )
    return {tid: int(n) for tid, n in result.all()}


async def get_many_by_key(session: AsyncSession, keys: list[str]) -> dict[str, SheetType]:
    """The types with these keys, fresh from the database; unknown keys are left out."""
    if not keys:
        return {}
    result = await session.execute(
        select(SheetType)
        .where(col(SheetType.key).in_(keys))
        .execution_options(populate_existing=True)
    )
    return {t.key: t for t in result.scalars()}
