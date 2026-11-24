# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Calibration runs: one number for every calibration, check and probe page (#336, ADR 0029).

``allocate_run`` takes the tray's counter row under the sheet write lock and inserts the run in the
same transaction, so concurrent submits get distinct numbers and a number is never handed out
twice: a failed submit deletes its run and leaves a gap, retention drops old runs without touching
the counter. ``submit_with_run`` is the submit with its compensation; "not submitted" is derived
from the job row in ``list_runs`` and never stored.

The tray the printer reports after the job ended reaches ``record_media_source`` through
``RunMediaSource``, registered with the page job runner. It is printer input: cleaned, at most
``MEDIA_SOURCE_MAX`` characters, never interpreted, and shown only to callers with access to the
printer.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Final, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field
from sqlalchemy import delete, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import col

from app.models.calibration_run import (
    CalibrationCheckSheetPass,
    CalibrationRun,
    CalibrationRunCounter,
)
from app.models.job import PAGE_KIND_EXPR, Job, json_at
from app.models.sheet import SheetType, SheetTypeVersion
from app.repositories.write_lock import write_transaction
from app.services.page_jobs import PagePayload
from app.settings_store.registry import SETTINGS
from app.sheets.text import sanitise_text

logger = logging.getLogger(__name__)

RUN_RETENTION: Final = 1000
"""Runs kept per printer and tray (design 7.5); the oldest go first."""
MEDIA_SOURCE_MAX: Final = 64
"""Longest printer-reported tray name stored."""

_JOB_RETENTION: Final = int(SETTINGS["jobs.retention_days"].default)
"""``jobs.retention_days`` when the caller does not pass the setting."""

RunKind = Literal["calibration_page", "check_page", "probe_page"]

_RUN_ID_EXPR = json_at("$.detail.run_id")
_RETRY_OF_EXPR = json_at("$.retry_of")


@dataclass(frozen=True)
class CheckSheetChoice:
    """The pass of a check sheet a check print takes (filled by the check-sheet flow)."""

    check_sheet_id: UUID
    pass_no: int


class CalibrationRunRead(BaseModel):
    """One print of a run: the run itself, or a retry of its job (same number, ``retry``)."""

    id: UUID = Field(description="The run id; the reading form sends it with a fit and a save.")
    run_no: int = Field(description="Number per printer and tray, from 1, never reused.")
    kind: RunKind
    created_at: datetime = Field(description="When the run was sent, or the retry.")
    job_id: UUID = Field(description="The job; it may have been cleaned up since.")
    retry: bool = Field(description="A retry of the run's job: shown as `#7 (retry)`.")
    submitted: bool = Field(
        description=(
            "False when the run has no job row although jobs are still kept for it "
            "(`jobs.retention_days`): its submit failed or was interrupted."
        )
    )
    requested_tray: str
    actual_media_source: str | None = Field(
        description="The tray the printer reported, only when it differs from the request."
    )
    media_source_reported: bool = Field(
        description='False: the printer did not say which tray it took ("not reported").'
    )
    check_sheet_id: UUID | None
    pass_no: int | None
    type_key: str | None = Field(description="Null when the sheet type was deleted since.")
    version: int | None = Field(description="Null when the sheet type was deleted since.")


# ---------------------------------------------------------------------------- allocate


async def allocate_run(
    session: AsyncSession,
    *,
    printer_id: UUID,
    tray: str,
    kind: RunKind,
    job_id: UUID,
    type_version_id: UUID,
    correction: dict[str, Any] | None,
    actor: str | None,
    check_sheet: CheckSheetChoice | None = None,
) -> CalibrationRun:
    """Take the next number of ``(printer_id, tray)``, insert the run and commit.

    One write transaction: the counter is read and raised under the sheet write lock, the run
    (and its check sheet pass) inserted. Retention is applied by ``submit_with_run`` only after
    the submit succeeded, so a failed submit never evicts a kept run.
    """
    async with write_transaction(session):
        # Read fresh under the lock: a long-lived session may hold a stale copy (review M2).
        counter = await session.get(
            CalibrationRunCounter, (printer_id, tray), populate_existing=True
        )
        if counter is None:
            run_no = 1
            session.add(CalibrationRunCounter(printer_id=printer_id, tray=tray, next_run=2))
        else:
            run_no = counter.next_run
            counter.next_run = run_no + 1
        run = CalibrationRun(
            id=uuid4(),
            printer_id=printer_id,
            tray=tray,
            run_no=run_no,
            kind=kind,
            job_id=job_id,
            type_version_id=type_version_id,
            correction=correction,
            check_sheet_id=check_sheet.check_sheet_id if check_sheet else None,
            pass_no=check_sheet.pass_no if check_sheet else None,
            requested_tray=tray,
            actor=actor,
        )
        session.add(run)
        await session.flush()
        if check_sheet is not None:
            session.add(
                CalibrationCheckSheetPass(
                    check_sheet_id=check_sheet.check_sheet_id,
                    pass_no=check_sheet.pass_no,
                    run_id=run.id,
                )
            )
        await session.commit()
    return run


def _settled(cutoff: datetime) -> Any:
    """A run whose submit is decided: its job row exists, or it is older than the job retention
    window (its job was cleaned up). The same rule as ``submitted`` in ``list_runs``."""
    return or_(col(Job.id).is_not(None), col(CalibrationRun.created_at) <= cutoff)


async def _drop_old_runs(
    session: AsyncSession, printer_id: UUID, tray: str, *, retention_days: int
) -> None:
    """Keep the newest ``RUN_RETENTION`` settled runs of the tray. Counted in rows, not numbers
    (a failed submit leaves a gap), and a run still in flight (no job row yet, inside the job
    retention window) neither counts nor goes: another request may still submit or compensate
    it, and its failure must not have pushed a kept run out."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    old_ids = (
        (
            await session.execute(
                select(col(CalibrationRun.id))
                .outerjoin(Job, col(Job.id) == col(CalibrationRun.job_id))
                .where(
                    col(CalibrationRun.printer_id) == printer_id,
                    col(CalibrationRun.tray) == tray,
                    _settled(cutoff),
                )
                .order_by(col(CalibrationRun.run_no).desc())
                .offset(RUN_RETENTION)
            )
        )
        .scalars()
        .all()
    )
    if not old_ids:
        return
    # The pass rows go explicitly too: the cascade only acts where foreign keys are on.
    await session.execute(
        delete(CalibrationCheckSheetPass).where(col(CalibrationCheckSheetPass.run_id).in_(old_ids))
    )
    await session.execute(delete(CalibrationRun).where(col(CalibrationRun.id).in_(old_ids)))


def hub_zone(name: str) -> tzinfo:
    """The zone of the setting ``general.time_zone``; an unknown zone is UTC."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def stamp(payload: PagePayload, run: CalibrationRun, *, time_zone: str = "UTC") -> PagePayload:
    """The payload with the run's number, time (``HH:MM`` in the hub's ``time_zone``, the setting
    ``general.time_zone``; an unknown zone is UTC) and id in ``detail``.

    ``detail`` ignores unknown keys in every release since 1.7.0 (ADR 0034); a retry copies the
    payload and so prints the same number and time.
    """
    detail = {
        **payload.detail,
        "run_no": run.run_no,
        "time": run.created_at.astimezone(hub_zone(time_zone)).strftime("%H:%M"),
        "run_id": str(run.id),
    }
    return payload.model_copy(update={"detail": detail})


SubmitFn = Callable[[PagePayload, UUID], Awaitable[UUID]]


async def submit_with_run(
    session: AsyncSession,
    payload: PagePayload,
    *,
    printer_id: UUID,
    kind: RunKind,
    type_version_id: UUID,
    actor: str | None,
    submit: SubmitFn,
    check_sheet: CheckSheetChoice | None = None,
    time_zone: str = "UTC",
    retention_days: int = _JOB_RETENTION,
) -> tuple[CalibrationRun, UUID]:
    """Allocate a run, stamp the payload and ``submit(payload, job_id)`` with the run's job id.

    On an ``Exception`` from ``submit`` the run (and its pass) is deleted in a compensating
    transaction if no job row with that id exists; the counter stays advanced, a gap and never a
    reuse. A ``CancelledError`` (a ``BaseException``) is not compensated: the queue may have
    committed the row already and prints it on the next start.
    """
    job_id = uuid4()
    run = await allocate_run(
        session,
        printer_id=printer_id,
        tray=payload.tray,
        kind=kind,
        job_id=job_id,
        type_version_id=type_version_id,
        correction=payload.calibration.model_dump() if payload.calibration else None,
        actor=actor,
        check_sheet=check_sheet,
    )
    # A failing retention or compensation rolls the session back, which would expire the run
    # and break every later read of it; the caller keeps a detached, fully loaded copy.
    session.expunge(run)
    try:
        submitted = await submit(stamp(payload, run, time_zone=time_zone), job_id)
    except Exception:
        await _discard_unsubmitted(session, run)
        raise
    await prune_runs(session, printer_id, payload.tray, retention_days=retention_days)
    return run, submitted


async def prune_runs(
    session: AsyncSession, printer_id: UUID, tray: str, *, retention_days: int = _JOB_RETENTION
) -> None:
    """Keep the newest ``RUN_RETENTION`` settled runs of the tray; a failure is logged, never
    raised (the page is already queued)."""
    try:
        async with write_transaction(session):
            await _drop_old_runs(session, printer_id, tray, retention_days=retention_days)
            await session.commit()
    except Exception:
        logger.exception("Calibration runs: retention of a tray failed; the next submit retries")


async def _discard_unsubmitted(session: AsyncSession, run: CalibrationRun) -> None:
    try:
        async with write_transaction(session):
            if await session.get(Job, run.job_id) is None:
                await session.execute(
                    delete(CalibrationCheckSheetPass).where(
                        col(CalibrationCheckSheetPass.run_id) == run.id
                    )
                )
                await session.execute(
                    delete(CalibrationRun).where(col(CalibrationRun.id) == run.id)
                )
            await session.commit()
    except Exception:
        # The run stays and is listed as not submitted; the caller's error is the one to report.
        logger.exception("Calibration run %s: compensation after a failed submit failed", run.id)


# ---------------------------------------------------------------------------- list


async def list_runs(
    session: AsyncSession,
    printer_id: UUID,
    tray: str,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> list[CalibrationRunRead]:
    """The tray's runs and the retries of their jobs, newest first.

    ``submitted`` is derived: false only for a run whose job row is missing while jobs of its age
    are still kept (``created_at`` within ``retention_days``); an older run without a job row had
    its job cleaned up, not lost.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    rows = (
        await session.execute(
            select(
                CalibrationRun,
                col(Job.id).is_not(None),
                col(SheetType.key),
                col(SheetTypeVersion.version),
            )
            .outerjoin(Job, col(Job.id) == col(CalibrationRun.job_id))
            .outerjoin(
                SheetTypeVersion,
                col(SheetTypeVersion.id) == col(CalibrationRun.type_version_id),
            )
            .outerjoin(SheetType, col(SheetType.id) == col(SheetTypeVersion.type_id))
            .where(col(CalibrationRun.printer_id) == printer_id, col(CalibrationRun.tray) == tray)
        )
    ).all()
    if not rows:
        return []
    by_id = {str(run.id): (run, key, version) for run, _, key, version in rows}
    out = [
        _read(run, key, version, job_id=run.job_id, created_at=run.created_at, retry=False,
              submitted=has_job or _aware(run.created_at) <= cutoff)
        for run, has_job, key, version in rows
    ]  # fmt: skip
    retries = (
        await session.execute(
            select(col(Job.id), col(Job.created_at), col(Job.payload)).where(
                col(Job.printer_id) == printer_id,
                literal_column(PAGE_KIND_EXPR) == "calibration_page",
                literal_column(_RUN_ID_EXPR).in_(list(by_id)),
                literal_column(_RETRY_OF_EXPR).is_not(None),
            )
        )
    ).all()
    for job_id, created_at, payload in retries:
        run, key, version = by_id[str(payload["detail"]["run_id"])]
        out.append(
            _read(run, key, version, job_id=job_id, created_at=created_at, retry=True,
                  submitted=True)
        )  # fmt: skip
    out.sort(key=lambda r: (_aware(r.created_at), r.run_no, r.retry), reverse=True)
    return out


def _aware(value: datetime) -> datetime:
    """SQLite gives naive datetimes back; they are stored in UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _read(
    run: CalibrationRun,
    key: str | None,
    version: int | None,
    *,
    job_id: UUID,
    created_at: datetime,
    retry: bool,
    submitted: bool,
) -> CalibrationRunRead:
    return CalibrationRunRead(
        id=run.id,
        run_no=run.run_no,
        kind=run.kind,  # type: ignore[arg-type]  # the check constraint keeps it a RunKind
        created_at=_aware(created_at),
        job_id=job_id,
        retry=retry,
        submitted=submitted,
        requested_tray=run.requested_tray,
        actual_media_source=run.actual_media_source,
        media_source_reported=run.media_source_reported,
        check_sheet_id=run.check_sheet_id,
        pass_no=run.pass_no,
        type_key=key,
        version=version,
    )


# ---------------------------------------------------------------------------- media source


def _run_id(payload: PagePayload) -> UUID | None:
    raw = payload.detail.get("run_id")
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def wants_media_source(payload: PagePayload) -> bool:
    """Only a page that belongs to a run is worth asking the printer about."""
    return _run_id(payload) is not None


def _bounded(reported: str) -> str | None:
    try:
        cleaned = sanitise_text(reported[:MEDIA_SOURCE_MAX], max_len=MEDIA_SOURCE_MAX)
    except ValueError:
        return None
    return cleaned or None


async def record_media_source(
    session: AsyncSession, payload: PagePayload, reported: str | None
) -> None:
    """Store what the printer said about the tray of the payload's run.

    The newest print's answer stands (a retry is the newer print of the same run). ``None`` (the
    printer named no source) or an answer that is empty after cleaning leaves the run "not
    reported", also when an earlier print of it had reported a tray. A named source marks it
    reported and is stored only when it differs from the requested tray. A question that failed
    never reaches this function, so it changes nothing.
    """
    run_id = _run_id(payload)
    if run_id is None:
        return
    source = None if reported is None else _bounded(reported)
    async with write_transaction(session):
        run = await session.get(CalibrationRun, run_id)
        if run is not None:
            run.media_source_reported = source is not None
            run.actual_media_source = (
                source if source is not None and source != run.requested_tray else None
            )
        await session.commit()


class RunMediaSource:
    """The page job runner's ``MediaSourceListener`` for calibration pages."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    def wants(self, payload: PagePayload) -> bool:
        return wants_media_source(payload)

    async def record(self, job_id: UUID, payload: PagePayload, media_source: str | None) -> None:
        del job_id  # the run is named by the payload: a retry has its own job id
        async with self._factory() as session:
            await record_media_source(session, payload, media_source)
