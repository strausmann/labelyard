# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The sheet-type log (#351, design 7.6): what was checked, decided and changed per sheet type.

Written automatically by the writers that change or check a type, in their own transaction:
the readings of a check print (``check_print``), "Mark as verified" (``verified``), every new
version (``new_version``) and every catalog update (``catalog_update``). An admin adds a
``note`` by hand or through the API and sets the verdict and note of any entry afterwards.

The log names printers, trays and local history. It is internal: never part of an export, the
catalog or a pre-filled feedback issue. A key restricted to some printers reads only the entries
of those printers and the entries without a printer.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.models.calibration_run import CalibrationHistory, CalibrationRun
from app.models.printer import Printer
from app.models.printer_tray import PrinterTray
from app.models.sheet import SheetTypeVersion
from app.models.sheet_type_log import SheetTypeLog
from app.sheets.errors import (
    InvalidFieldError,
    PrinterNotFoundError,
    SheetApiError,
    TextTooLongError,
)
from app.sheets.text import sanitise_text

LOG_RETENTION: Final = 500
"""Entries kept per sheet type (design 7.6); the oldest go first."""
LIST_LIMIT_MAX: Final = 100
NOTE_MAX: Final = 2000
"""Longest note of an entry, after cleaning."""

LogKind = Literal["check_print", "verified", "new_version", "catalog_update", "note"]
Verdict = Literal["fits", "sheet_geometry", "feed_variance", "printer", "none"]


class SheetTypeLogNotFoundError(SheetApiError):
    status = 404
    error_code = "sheet_type_log_not_found"
    message = "No such log entry on this sheet type."


# ---------------------------------------------------------------------------- read models


class LogReading(BaseModel):
    """One crossing of a check print: the ink's offset from the crossing on the real sheet."""

    col_gap: int = Field(description="Vertical gap S1, S2, ... counted from 1.")
    row_gap: int = Field(description="Horizontal gap Z1, Z2, ... counted from 1.")
    across_mm: float = Field(description="Ink offset to the right, mm.")
    down_mm: float = Field(description="Ink offset downwards, mm.")
    residual_mm: float | None = Field(
        default=None, description="Distance from the fit of all readings of the entry, mm."
    )


class LogPrinter(BaseModel):
    id: UUID
    name: str


class LogRun(BaseModel):
    """The calibration run the entry is about; ``null`` once the run is gone."""

    id: UUID
    run_no: int
    kind: str = Field(description="`calibration_page`, `check_page` or `probe_page`.")
    pass_no: int | None = Field(description="The pass of a check sheet, else null.")


class LogCorrection(BaseModel):
    """The calibration history entry in effect; ``null`` once retention dropped it."""

    id: UUID
    seq: int = Field(description="The entry's number on its printer and tray.")
    offset_x_mm: float | None
    offset_y_mm: float | None


class SheetTypeLogRead(BaseModel):
    id: UUID
    kind: LogKind = Field(
        description=(
            "`check_print` (readings of a check print entered), `verified` (marked as verified), "
            "`new_version`, `catalog_update` or `note` (a hand entry)."
        )
    )
    type_version: int | None = Field(
        description="The sheet type version the entry is about; null once the version is gone."
    )
    printer: LogPrinter | None = Field(
        description="The printer, null for an entry without one or after the printer was deleted."
    )
    tray: str | None
    tray_label: str | None = Field(description="The tray's label on that printer, if known.")
    run: LogRun | None
    correction: LogCorrection | None
    readings: list[LogReading] | None = Field(description="The readings per crossing, if entered.")
    verdict: Verdict
    note: str | None
    actor: str | None
    created_at: datetime


class LogNoteCreate(BaseModel):
    """A hand entry (kind `note`)."""

    printer_id: UUID | None = Field(default=None, description="The printer the note is about.")
    tray: str | None = Field(
        default=None, min_length=1, max_length=100, description="Tray key; only with a printer."
    )
    verdict: Verdict = "none"
    note: str | None = Field(
        default=None,
        max_length=NOTE_MAX * 4,
        description="At most 2000 characters after cleaning; line breaks are kept.",
    )


class LogEntryPatch(BaseModel):
    """Verdict and note of an entry; an omitted field is left as it is, `note: null` clears it."""

    verdict: Verdict | None = None
    note: str | None = Field(
        default=None,
        max_length=NOTE_MAX * 4,
        description="At most 2000 characters after cleaning; line breaks are kept.",
    )


# ---------------------------------------------------------------------------- write


def clean_note(note: str | None) -> str | None:
    """A note as stored: cleaned (line breaks kept), at most ``NOTE_MAX``; empty is ``None``."""
    if note is None:
        return None
    try:
        return sanitise_text(note, max_len=NOTE_MAX, multiline=True) or None
    except ValueError:
        raise TextTooLongError("note") from None


async def log_entry(
    session: AsyncSession,
    *,
    type_id: UUID,
    version_id: UUID | None,
    kind: LogKind,
    printer_id: UUID | None = None,
    tray: str | None = None,
    history_id: UUID | None = None,
    run_id: UUID | None = None,
    readings: Sequence[dict[str, Any]] | None = None,
    verdict: Verdict = "none",
    note: str | None = None,
    actor: str | None,
) -> SheetTypeLog:
    """Add an entry to the caller's write transaction (no commit) and drop the type's entries
    beyond ``LOG_RETENTION``, oldest first. ``note`` is cleaned here."""
    entry = SheetTypeLog(
        type_id=type_id,
        type_version_id=version_id,
        kind=kind,
        printer_id=printer_id,
        tray=tray,
        history_id=history_id,
        run_id=run_id,
        readings=list(readings) if readings is not None else None,
        verdict=verdict,
        note=clean_note(note),
        actor=actor,
    )
    session.add(entry)
    await session.flush()
    await _drop_old(session, type_id)
    return entry


async def _drop_old(session: AsyncSession, type_id: UUID) -> None:
    old = (
        (
            await session.execute(
                select(col(SheetTypeLog.id))
                .where(col(SheetTypeLog.type_id) == type_id)
                .order_by(col(SheetTypeLog.created_at).desc(), col(SheetTypeLog.id).desc())
                .offset(LOG_RETENTION)
            )
        )
        .scalars()
        .all()
    )
    if old:
        await session.execute(delete(SheetTypeLog).where(col(SheetTypeLog.id).in_(old)))


async def add_note(
    session: AsyncSession,
    key: str,
    body: LogNoteCreate,
    *,
    actor: str,
) -> SheetTypeLogRead:
    """A hand entry on the type's current version; the printer must exist."""
    from app.repositories.write_lock import write_transaction
    from app.sheets import type_service as ts  # deferred: it imports this module

    if body.tray is not None and body.printer_id is None:
        raise InvalidFieldError("tray", "needs printer_id")
    tray = None if body.tray is None else sanitise_text(body.tray, max_len=100) or None
    note = clean_note(body.note)
    async with write_transaction(session):
        t = await ts.get_type(session, key)
        if body.printer_id is not None and await session.get(Printer, body.printer_id) is None:
            raise PrinterNotFoundError()
        version = await ts.current_version(session, t.id)
        entry = await log_entry(
            session,
            type_id=t.id,
            version_id=version.id,
            kind="note",
            printer_id=body.printer_id,
            tray=tray,
            verdict=body.verdict,
            note=note,
            actor=actor,
        )
        await session.commit()
    return (await _reads(session, [entry]))[0]


async def patch_entry(
    session: AsyncSession, key: str, entry_id: UUID, changes: dict[str, Any]
) -> SheetTypeLogRead:
    """Set the verdict and note of an entry of ``key``; nothing else of it changes."""
    from app.repositories.write_lock import write_transaction
    from app.sheets import type_service as ts

    if "verdict" in changes and changes["verdict"] is None:
        raise InvalidFieldError("verdict", "must not be null")
    note = clean_note(changes["note"]) if "note" in changes else None
    async with write_transaction(session):
        t = await ts.get_type(session, key)
        entry = await session.get(SheetTypeLog, entry_id, populate_existing=True)
        if entry is None or entry.type_id != t.id:
            raise SheetTypeLogNotFoundError()
        if "verdict" in changes:
            entry.verdict = changes["verdict"]
        if "note" in changes:
            entry.note = note
        await session.commit()
    return (await _reads(session, [entry]))[0]


# ---------------------------------------------------------------------------- read


def _hmm_mm(value: int | None) -> float | None:
    return None if value is None else value / 100


def _aware(dt: datetime) -> datetime:
    # SQLite hands back naive datetimes; every stored time is UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _reads(session: AsyncSession, entries: Sequence[SheetTypeLog]) -> list[SheetTypeLogRead]:
    """The read models of ``entries``, with the names and numbers of what they refer to."""

    def ids(attr: str) -> set[UUID]:
        return {v for e in entries if (v := getattr(e, attr)) is not None}

    async def by_id(model: Any, wanted: set[UUID]) -> dict[UUID, Any]:
        if not wanted:
            return {}
        rows = await session.execute(select(model).where(col(model.id).in_(wanted)))
        return {r.id: r for r in rows.scalars()}

    versions = await by_id(SheetTypeVersion, ids("type_version_id"))
    printers = await by_id(Printer, ids("printer_id"))
    runs = await by_id(CalibrationRun, ids("run_id"))
    history = await by_id(CalibrationHistory, ids("history_id"))
    labels: dict[tuple[UUID, str], str] = {}
    if printers:
        rows = await session.execute(
            select(PrinterTray).where(col(PrinterTray.printer_id).in_(set(printers)))
        )
        labels = {(r.printer_id, r.tray_key): r.label for r in rows.scalars() if r.label}
    out: list[SheetTypeLogRead] = []
    for e in entries:
        v = versions.get(e.type_version_id) if e.type_version_id else None
        p = printers.get(e.printer_id) if e.printer_id else None
        run = runs.get(e.run_id) if e.run_id else None
        h = history.get(e.history_id) if e.history_id else None
        out.append(
            SheetTypeLogRead(
                id=e.id,
                kind=e.kind,  # type: ignore[arg-type]
                type_version=v.version if v is not None else None,
                printer=LogPrinter(id=p.id, name=p.name) if p is not None else None,
                tray=e.tray,
                tray_label=labels.get((p.id, e.tray)) if p is not None and e.tray else None,
                run=(
                    LogRun(id=run.id, run_no=run.run_no, kind=run.kind, pass_no=run.pass_no)
                    if run is not None
                    else None
                ),
                correction=(
                    LogCorrection(
                        id=h.id,
                        seq=h.seq,
                        offset_x_mm=_hmm_mm(h.offset_x_hmm),
                        offset_y_mm=_hmm_mm(h.offset_y_hmm),
                    )
                    if h is not None
                    else None
                ),
                readings=(
                    [LogReading.model_validate(r) for r in e.readings]
                    if e.readings is not None
                    else None
                ),
                verdict=e.verdict,  # type: ignore[arg-type]
                note=e.note,
                actor=e.actor,
                created_at=_aware(e.created_at),
            )
        )
    return out


async def list_entries(
    session: AsyncSession,
    key: str,
    *,
    allowed_printer_ids: Sequence[str],
    printer_id: UUID | None = None,
    tray: str | None = None,
    kind: LogKind | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[SheetTypeLogRead], bool]:
    """The type's entries, newest first, and whether more follow.

    ``allowed_printer_ids`` is the key's printer restriction (empty: every printer): a restricted
    key sees the entries of its printers and the entries without a printer, nothing else.
    """
    from app.sheets import type_service as ts

    t = await ts.get_type(session, key)
    query = select(SheetTypeLog).where(col(SheetTypeLog.type_id) == t.id)
    if allowed_printer_ids:
        allowed = [UUID(p) for p in allowed_printer_ids]
        query = query.where(
            or_(col(SheetTypeLog.printer_id).is_(None), col(SheetTypeLog.printer_id).in_(allowed))
        )
    if printer_id is not None:
        query = query.where(col(SheetTypeLog.printer_id) == printer_id)
    if tray is not None:
        query = query.where(col(SheetTypeLog.tray) == tray)
    if kind is not None:
        query = query.where(col(SheetTypeLog.kind) == kind)
    rows = (
        (
            await session.execute(
                query.order_by(col(SheetTypeLog.created_at).desc(), col(SheetTypeLog.id).desc())
                .offset(offset)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    return await _reads(session, rows[:limit]), len(rows) > limit


async def read_entry(session: AsyncSession, entry: SheetTypeLog) -> SheetTypeLogRead:
    return (await _reads(session, [entry]))[0]
