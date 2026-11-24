# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The history of a tray's calibration: every save, reset, revert and import (#336, ADR 0029).

``record`` writes an entry in the caller's write transaction, the one that changes
``printer_calibrations``, so the row and its newest entry never disagree. ``seq`` counts per
printer and tray under the sheet write lock; the newest ``HISTORY_RETENTION`` entries are kept.

Jobs link (design 7.5, S7): a page job names the entry it was printed with in
``PagePayload.calibration_history_id``. Jobs from before 1.8.0 have no id; they belong to an entry
when their snapshot values equal the entry's and they were created inside its window (from the
entry to the next entry of the tray).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, and_, delete, false, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.calibration.model import (
    CalibrationIn,
    CalibrationRead,
    MeasuredOn,
    deviation_to_values,
)
from app.models.calibration_run import CalibrationHistory, CalibrationRun
from app.models.job import Job, json_at
from app.models.printer_calibration import PrinterCalibration
from app.models.sheet import SheetType, SheetTypeVersion
from app.page_render.transform import IDENTITY, Deviation
from app.repositories.write_lock import write_transaction
from app.sheets.errors import SheetApiError

HISTORY_RETENTION: Final = 500
"""Entries kept per printer and tray (design 7.5); the oldest go first."""
LIST_LIMIT_MAX: Final = 100

HistoryAction = Literal["imported", "save", "revert", "reset"]
HistorySource = Literal["manual", "fit"]

_VALUE_KEYS: Final = ("offset_x_hmm", "offset_y_hmm", "scale_x_ppm", "scale_y_ppm", "rotation_udeg")
_API_KEYS: Final = ("offset_x_mm", "offset_y_mm", "scale_x", "scale_y", "rotation_deg")

_HISTORY_ID_EXPR: Final = json_at("$.calibration_history_id")
_TRAY_EXPR: Final = json_at("$.tray")
_SNAPSHOT_EXPRS: Final = {
    "offset_x_hmm": json_at("$.calibration.dx_hmm"),
    "offset_y_hmm": json_at("$.calibration.dy_hmm"),
    "scale_x_ppm": json_at("$.calibration.sx_ppm"),
    "scale_y_ppm": json_at("$.calibration.sy_ppm"),
    "rotation_udeg": json_at("$.calibration.rotation_udeg"),
}


class CalibrationHistoryNotFoundError(SheetApiError):
    status = 404
    error_code = "calibration_history_not_found"
    message = "No such calibration history entry on this printer and tray."


# ---------------------------------------------------------------------------- write


def deviation_dict(dev: Deviation) -> dict[str, int]:
    """The stored integers of a deviation, as kept in ``previous``."""
    return {
        "offset_x_hmm": dev.offset_x_hmm,
        "offset_y_hmm": dev.offset_y_hmm,
        "scale_x_ppm": dev.scale_x_ppm,
        "scale_y_ppm": dev.scale_y_ppm,
        "rotation_udeg": dev.rotation_udeg,
    }


def entry_deviation(entry: CalibrationHistory) -> Deviation | None:
    """The values of an entry; ``None`` for a reset (no calibration after it)."""
    if entry.offset_x_hmm is None:
        return None
    return Deviation(
        entry.offset_x_hmm,
        entry.offset_y_hmm or 0,
        entry.scale_x_ppm or 1_000_000,
        entry.scale_y_ppm or 1_000_000,
        entry.rotation_udeg or 0,
    )


async def record(
    session: AsyncSession,
    *,
    printer_id: UUID,
    tray: str,
    action: HistoryAction,
    new: Deviation | None,
    previous: Deviation | None,
    method: str | None,
    measured_on_version_id: UUID | None,
    run_id: UUID | None,
    readings: list[dict[str, Any]] | None,
    residuals: list[float] | None,
    reverted_from_id: UUID | None,
    actor: str | None,
    note: str | None,
    source: HistorySource | None,
) -> CalibrationHistory:
    """Add the next entry of ``(printer_id, tray)`` to the caller's transaction (no commit).

    The caller holds the sheet write lock, so ``seq`` is the tray's highest plus one without a
    race; entries beyond ``HISTORY_RETENTION`` are dropped in the same transaction.
    """
    top = (
        await session.execute(
            select(func.max(col(CalibrationHistory.seq))).where(
                col(CalibrationHistory.printer_id) == printer_id,
                col(CalibrationHistory.tray) == tray,
            )
        )
    ).scalar_one_or_none()
    values = deviation_dict(new) if new is not None else dict.fromkeys(_VALUE_KEYS)
    entry = CalibrationHistory(
        printer_id=printer_id,
        tray=tray,
        seq=(top or 0) + 1,
        action=action,
        method=method,
        previous=deviation_dict(previous) if previous is not None else None,
        measured_on_type_version_id=measured_on_version_id,
        run_id=run_id,
        source=source,
        readings=readings,
        residuals=residuals,
        reverted_from_id=reverted_from_id,
        actor=actor,
        note=note,
        **values,
    )
    session.add(entry)
    await session.flush()
    await _drop_old(session, printer_id, tray)
    return entry


async def record_resets(
    session: AsyncSession, rows: Sequence[PrinterCalibration], *, actor: str | None
) -> None:
    """A ``reset`` entry for each calibration row the caller deletes in its transaction (a removed
    tray, a replaced device; review on #442). The caller already holds SQLite's write lock."""
    for row in rows:
        await record(
            session,
            printer_id=row.printer_id,
            tray=row.tray_key,
            action="reset",
            new=None,
            previous=Deviation(
                row.offset_x_hmm,
                row.offset_y_hmm,
                row.scale_x_ppm,
                row.scale_y_ppm,
                row.rotation_udeg,
            ),
            method=None,
            measured_on_version_id=None,
            run_id=None,
            readings=None,
            residuals=None,
            reverted_from_id=None,
            actor=actor,
            note=None,
            source=None,
        )


async def _drop_old(session: AsyncSession, printer_id: UUID, tray: str) -> None:
    old = (
        (
            await session.execute(
                select(col(CalibrationHistory.id))
                .where(
                    col(CalibrationHistory.printer_id) == printer_id,
                    col(CalibrationHistory.tray) == tray,
                )
                .order_by(col(CalibrationHistory.seq).desc())
                .offset(HISTORY_RETENTION)
            )
        )
        .scalars()
        .all()
    )
    if old:
        await session.execute(delete(CalibrationHistory).where(col(CalibrationHistory.id).in_(old)))


async def latest_id(session: AsyncSession, printer_id: UUID, tray: str) -> UUID | None:
    """The newest entry of the tray: the one a page printed now is printed with."""
    found = (
        await session.execute(
            select(col(CalibrationHistory.id))
            .where(
                col(CalibrationHistory.printer_id) == printer_id,
                col(CalibrationHistory.tray) == tray,
            )
            .order_by(col(CalibrationHistory.seq).desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return found


# ---------------------------------------------------------------------------- restore


async def restore(
    session: AsyncSession, printer_id: UUID, tray: str, entry_id: UUID, *, actor: str
) -> CalibrationRead:
    """Make an entry's values the tray's calibration again, as a new ``revert`` entry.

    Nothing is deleted. A reset entry restores "no calibration". An entry measured on a sheet type
    version that was deleted since is restored without it (``measured_on_deleted``).
    """
    from app.calibration import service  # the service writes entries through this module

    await service.page_printer(session, printer_id)
    await service.check_tray(session, printer_id, tray)
    notes: list[Literal["client_values_ignored", "measured_on_deleted"]] = []
    async with write_transaction(session):
        await service.check_tray(session, printer_id, tray)
        entry = await session.get(CalibrationHistory, entry_id, populate_existing=True)
        if entry is None or entry.printer_id != printer_id or entry.tray != tray:
            raise CalibrationHistoryNotFoundError()
        version_id = entry.measured_on_type_version_id
        if version_id is not None and await session.get(SheetTypeVersion, version_id) is None:
            version_id = None
            notes.append("measured_on_deleted")
        values = entry_deviation(entry)
        row = await session.get(PrinterCalibration, (printer_id, tray))
        previous = service.row_deviation(row)
        if values is None:
            if row is not None:
                await session.delete(row)
            row = None
        else:
            row = service.write_row(
                session, row, printer_id, tray, values, version_id=version_id, actor=actor
            )
        await record(
            session,
            printer_id=printer_id,
            tray=tray,
            action="revert",
            new=values,
            previous=previous,
            method=entry.method,
            measured_on_version_id=version_id,
            run_id=None,
            readings=None,
            residuals=None,
            reverted_from_id=entry.id,
            actor=actor,
            note=None,
            source=None,
        )
        await session.commit()
    read = await service.read_row(session, tray, row)
    return read.model_copy(update={"notes": notes})


# ---------------------------------------------------------------------------- jobs


def _window_end(next_created: datetime | None) -> ColumnElement[bool] | None:
    return None if next_created is None else col(Job.created_at) < next_created


def jobs_clause(entry: CalibrationHistory, next_created: datetime | None) -> ColumnElement[bool]:
    """The jobs printed with ``entry``: named by id, or (before 1.8.0) by equal values inside the
    entry's window. A reset entry stands for the identity, as a 1.7.x snapshot of no calibration."""
    dev = entry_deviation(entry) or IDENTITY
    by_values = [literal_column(_SNAPSHOT_EXPRS[k]) == getattr(dev, k) for k in _VALUE_KEYS]
    window = [col(Job.created_at) >= entry.created_at]
    end = _window_end(next_created)
    if end is not None:
        window.append(end)
    return and_(
        col(Job.printer_id) == entry.printer_id,
        literal_column(_TRAY_EXPR) == entry.tray,
        or_(
            literal_column(_HISTORY_ID_EXPR) == str(entry.id),
            and_(literal_column(_HISTORY_ID_EXPR).is_(None), *by_values, *window),
        ),
    )


async def _next_created(session: AsyncSession, entry: CalibrationHistory) -> datetime | None:
    found: datetime | None = (
        await session.execute(
            select(col(CalibrationHistory.created_at))
            .where(
                col(CalibrationHistory.printer_id) == entry.printer_id,
                col(CalibrationHistory.tray) == entry.tray,
                col(CalibrationHistory.seq) > entry.seq,
            )
            .order_by(col(CalibrationHistory.seq))
            .limit(1)
        )
    ).scalar_one_or_none()
    return found


async def jobs_filter(session: AsyncSession, history_id: UUID) -> ColumnElement[bool]:
    """``GET /api/v1/jobs?calibration=<id>``: an unknown id matches no job."""
    entry = await session.get(CalibrationHistory, history_id)
    if entry is None:
        return false()
    return jobs_clause(entry, await _next_created(session, entry))


# ---------------------------------------------------------------------------- read


class HistoryValues(BaseModel):
    """Values in API units; all ``None`` for a reset."""

    offset_x_mm: float | None
    offset_y_mm: float | None
    scale_x: float | None
    scale_y: float | None
    rotation_deg: float | None


class ValueChange(BaseModel):
    old: float | None
    new: float | None


class CalibrationHistoryRead(BaseModel):
    id: UUID
    seq: int = Field(description="Number per printer and tray, from 1.")
    action: HistoryAction
    method: str | None
    values: HistoryValues
    previous: HistoryValues | None = Field(
        description="The values before this entry; null for the first entry of the tray."
    )
    diff: dict[str, ValueChange] = Field(
        description="The values that differ from `previous`, by name (old → new)."
    )
    measured_on: MeasuredOn | None = Field(
        description="Null when no version was named, or when it was deleted since."
    )
    measured_on_deleted: bool = Field(
        description="The entry named a sheet type version that no longer exists."
    )
    run_id: UUID | None
    run_no: int | None = Field(description="Null without a run, or when the run was dropped.")
    source: HistorySource | None = Field(
        description="`fit`: the hub's fit of `readings`; `manual`: typed values; null otherwise."
    )
    readings: list[dict[str, Any]] | None
    residuals_mm: list[float] | None
    reverted_from_id: UUID | None
    actor: str | None
    created_at: datetime
    note: str | None
    jobs_count: int = Field(
        description="Jobs printed with this entry (`GET /api/v1/jobs?calibration=<id>`)."
    )


def _api_values(dev: Deviation | None) -> HistoryValues:
    if dev is None:
        return HistoryValues(
            offset_x_mm=None, offset_y_mm=None, scale_x=None, scale_y=None, rotation_deg=None
        )
    v: CalibrationIn = deviation_to_values(dev)
    return HistoryValues(**v.model_dump())


def _from_dict(raw: dict[str, Any] | None) -> Deviation | None:
    if raw is None:
        return None
    return Deviation(*(int(raw[k]) for k in _VALUE_KEYS))


def _diff(old: HistoryValues | None, new: HistoryValues) -> dict[str, ValueChange]:
    before = old.model_dump() if old is not None else dict.fromkeys(_API_KEYS)
    after = new.model_dump()
    return {k: ValueChange(old=before[k], new=after[k]) for k in _API_KEYS if before[k] != after[k]}


def _aware(value: datetime) -> datetime:
    """SQLite gives naive datetimes back; they are stored in UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def list_entries(
    session: AsyncSession, printer_id: UUID, tray: str, *, limit: int, offset: int
) -> tuple[list[CalibrationHistoryRead], bool]:
    """A page of the tray's entries, newest first, and whether more follow."""
    rows: Sequence[Any] = (
        await session.execute(
            select(
                CalibrationHistory,
                col(SheetType.key),
                col(SheetTypeVersion.version),
                col(CalibrationRun.run_no),
            )
            .outerjoin(
                SheetTypeVersion,
                col(SheetTypeVersion.id) == col(CalibrationHistory.measured_on_type_version_id),
            )
            .outerjoin(SheetType, col(SheetType.id) == col(SheetTypeVersion.type_id))
            .outerjoin(CalibrationRun, col(CalibrationRun.id) == col(CalibrationHistory.run_id))
            .where(
                col(CalibrationHistory.printer_id) == printer_id,
                col(CalibrationHistory.tray) == tray,
            )
            .order_by(col(CalibrationHistory.seq).desc())
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    out: list[CalibrationHistoryRead] = []
    for entry, key, version, run_no in rows:
        next_created = await _next_created(session, entry)
        count = (
            await session.execute(
                select(func.count()).select_from(Job).where(jobs_clause(entry, next_created))
            )
        ).scalar_one()
        values = _api_values(entry_deviation(entry))
        previous = None if entry.previous is None else _api_values(_from_dict(entry.previous))
        out.append(
            CalibrationHistoryRead(
                id=entry.id,
                seq=entry.seq,
                action=entry.action,
                method=entry.method,
                values=values,
                previous=previous,
                diff=_diff(previous, values),
                measured_on=None if key is None else MeasuredOn(type_key=key, version=version),
                measured_on_deleted=entry.measured_on_type_version_id is not None and key is None,
                run_id=entry.run_id,
                run_no=run_no,
                source=entry.source,  # written by this module only: manual, fit or null
                readings=entry.readings,
                residuals_mm=entry.residuals,
                reverted_from_id=entry.reverted_from_id,
                actor=entry.actor,
                created_at=_aware(entry.created_at),
                note=entry.note,
                jobs_count=int(count),
            )
        )
    return out, has_more
