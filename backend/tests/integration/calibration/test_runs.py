# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Calibration runs (#336, ADR 0029): numbering, gaps, retention, the derived ``submitted`` and
the submit with its compensation.

``submit_with_run`` is tested with fake submit functions: what the real queue does is covered by
the API tests (``test_calibration_runs_api.py``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import app.models  # noqa: F401
import pytest
from app.calibration import runs
from app.models.calibration_run import (
    CalibrationCheckSheet,
    CalibrationHistory,
    CalibrationRun,
    CalibrationRunCounter,
)
from app.models.job import Job
from app.models.printer import Printer
from app.models.printer_tray import PrinterTray
from app.services.page_jobs import PagePayload
from app.sheets import type_service as ts
from app.sheets.geometry import Geometry
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio

Factory = async_sessionmaker[AsyncSession]
G48 = Geometry("A4", 21000, 29700, 4570, 2120, 4, 12, 4820, 2120, 2130, 985, None, "rectangle")


async def _printer(factory: Factory) -> UUID:
    async with factory() as s:
        p = Printer(
            id=uuid4(),
            name=f"Office {uuid4().hex[:4]}",
            slug=f"p-{uuid4().hex[:6]}",
            model="m",
            backend="ipp",
            connection={"uri": "ipp://printer.example.invalid/ipp/print"},
        )
        s.add(p)
        s.add(PrinterTray(printer_id=p.id, tray_key="tray-1", label="Tray 1", is_default=True))
        s.add(PrinterTray(printer_id=p.id, tray_key="tray-2", label="Tray 2"))
        await s.commit()
        return p.id


async def _alloc(
    factory: Factory,
    printer_id: UUID,
    tray: str = "tray-1",
    *,
    job_id: UUID | None = None,
    version_id: UUID | None = None,
) -> CalibrationRun:
    async with factory() as s:
        return await runs.allocate_run(
            s,
            printer_id=printer_id,
            tray=tray,
            kind="calibration_page",
            job_id=job_id or uuid4(),
            type_version_id=version_id or uuid4(),
            correction=None,
            actor="user:admin",
        )


async def _job_row(factory: Factory, printer_id: UUID, job_id: UUID) -> None:
    async with factory() as s:
        s.add(Job(id=job_id, printer_id=printer_id, payload={"kind": "calibration_page"}))
        await s.commit()


async def _submitted(factory: Factory, printer_id: UUID, tray: str = "tray-1") -> CalibrationRun:
    """A run whose submit succeeded (the job row exists): retention applies after it."""

    async def ok(payload: PagePayload, job_id: UUID) -> UUID:
        del payload
        await _job_row(factory, printer_id, job_id)
        return job_id

    async with factory() as s:
        run, _ = await runs.submit_with_run(
            s,
            _payload(tray),
            printer_id=printer_id,
            kind="calibration_page",
            type_version_id=uuid4(),
            actor="user:admin",
            submit=ok,
        )
    return run


async def _listed(
    factory: Factory, printer_id: UUID, tray: str = "tray-1", retention_days: int = 30
) -> list[runs.CalibrationRunRead]:
    async with factory() as s:
        return await runs.list_runs(s, printer_id, tray, retention_days=retention_days)


def _payload(tray: str = "tray-1") -> PagePayload:
    return PagePayload(
        kind="calibration_page",
        media="A4",
        tray=tray,
        render_dpi=600,
        job_name="Calibration page · acme v1",
        detail={"type_key": "acme", "version": 1},
    )


# ---------------------------------------------------------------------------- numbering


async def test_first_run_of_a_tray_is_1(async_session_factory: Factory) -> None:
    pid = await _printer(async_session_factory)
    run = await _alloc(async_session_factory, pid)
    assert run.run_no == 1
    assert run.requested_tray == "tray-1" and run.media_source_reported is False
    async with async_session_factory() as s:
        counter = await s.get(CalibrationRunCounter, (pid, "tray-1"))
    assert counter is not None and counter.next_run == 2


async def test_run_numbers_increase_per_printer_and_tray(async_session_factory: Factory) -> None:
    pid, other = await _printer(async_session_factory), await _printer(async_session_factory)
    numbers = [(await _alloc(async_session_factory, pid)).run_no for _ in range(3)]
    assert numbers == [1, 2, 3]
    assert (await _alloc(async_session_factory, other)).run_no == 1  # another printer starts at 1


async def test_two_trays_count_separately(async_session_factory: Factory) -> None:
    pid = await _printer(async_session_factory)
    assert (await _alloc(async_session_factory, pid, "tray-1")).run_no == 1
    assert (await _alloc(async_session_factory, pid, "tray-1")).run_no == 2
    assert (await _alloc(async_session_factory, pid, "tray-2")).run_no == 1
    assert (await _alloc(async_session_factory, pid, "tray-1")).run_no == 3


async def test_retention_keeps_1000_runs(async_session_factory: Factory) -> None:
    pid = await _printer(async_session_factory)
    assert runs.RUN_RETENTION == 1000
    old = datetime(2026, 1, 1, tzinfo=UTC)
    async with async_session_factory() as s:  # 1000 runs already there, the counter at 1001
        s.add_all(
            CalibrationRun(
                printer_id=pid,
                tray="tray-1",
                run_no=n,
                kind="calibration_page",
                job_id=uuid4(),
                type_version_id=uuid4(),
                requested_tray="tray-1",
                created_at=old + timedelta(minutes=n),
            )
            for n in range(1, 1001)
        )
        s.add(CalibrationRunCounter(printer_id=pid, tray="tray-1", next_run=1001))
        await s.commit()
    other_tray = await _submitted(async_session_factory, pid, "tray-2")
    run = await _submitted(async_session_factory, pid)
    assert run.run_no == 1001
    async with async_session_factory() as s:
        nos = (
            (
                await s.execute(
                    select(CalibrationRun.run_no).where(
                        CalibrationRun.printer_id == pid, CalibrationRun.tray == "tray-1"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert await s.get(CalibrationRun, other_tray.id) is not None  # another tray untouched
    assert len(nos) == 1000 and min(nos) == 2 and max(nos) == 1001  # the oldest one went


async def test_retention_counts_rows_not_numbers(async_session_factory: Factory) -> None:
    """Codex review on #440: gaps from failed submits must not push a kept run out. One run, a
    counter that 1500 failed submits moved on: the next run is 1502 and run 1 stays."""
    pid = await _printer(async_session_factory)
    first = await _alloc(async_session_factory, pid)
    async with async_session_factory() as s:
        counter = await s.get(CalibrationRunCounter, (pid, "tray-1"))
        assert counter is not None
        counter.next_run = 1502
        await s.commit()
    run = await _submitted(async_session_factory, pid)
    assert run.run_no == 1502
    assert [r.run_no for r in await _listed(async_session_factory, pid)] == [1502, first.run_no]


async def test_failed_submit_at_the_retention_bound_keeps_the_oldest_run(
    async_session_factory: Factory,
) -> None:
    """Codex review on #440: a failed submit must not evict history. 1000 runs, the next submit
    fails: all 1000 stay, run 1 included."""
    pid = await _printer(async_session_factory)
    async with async_session_factory() as s:
        s.add_all(
            CalibrationRun(
                printer_id=pid, tray="tray-1", run_no=n, kind="calibration_page", job_id=uuid4(),
                type_version_id=uuid4(), requested_tray="tray-1",
            )
            for n in range(1, 1001)
        )  # fmt: skip
        s.add(CalibrationRunCounter(printer_id=pid, tray="tray-1", next_run=1001))
        await s.commit()

    async def failing(payload: PagePayload, job_id: UUID) -> UUID:
        del payload, job_id
        raise RuntimeError("queue full")

    async with async_session_factory() as s:
        with pytest.raises(RuntimeError):
            await runs.submit_with_run(
                s, _payload(), printer_id=pid, kind="calibration_page", type_version_id=uuid4(),
                actor="user:admin", submit=failing,
            )  # fmt: skip
    async with async_session_factory() as s:
        nos = (await s.execute(select(CalibrationRun.run_no))).scalars().all()
    assert len(nos) == 1000 and min(nos) == 1 and max(nos) == 1000


# ---------------------------------------------------------------------------- submitted


async def test_orphan_run_is_listed_as_not_submitted(async_session_factory: Factory) -> None:
    pid = await _printer(async_session_factory)
    orphan = await _alloc(async_session_factory, pid)
    sent = await _alloc(async_session_factory, pid)
    await _job_row(async_session_factory, pid, sent.job_id)
    listed = {r.run_no: r for r in await _listed(async_session_factory, pid)}
    assert listed[orphan.run_no].submitted is False
    assert listed[sent.run_no].submitted is True
    async with async_session_factory() as s:  # derived, never stored, nothing deleted
        assert await s.get(CalibrationRun, orphan.id) is not None


async def test_run_older_than_job_retention_is_not_marked_not_submitted(
    async_session_factory: Factory,
) -> None:
    pid = await _printer(async_session_factory)
    run = await _alloc(async_session_factory, pid)
    async with async_session_factory() as s:  # 31 days old; its job row was cleaned up
        row = await s.get(CalibrationRun, run.id)
        assert row is not None
        row.created_at = datetime.now(UTC) - timedelta(days=31)
        await s.commit()
    [listed] = await _listed(async_session_factory, pid, retention_days=30)
    assert listed.submitted is True
    [within] = await _listed(async_session_factory, pid, retention_days=40)
    assert within.submitted is False


# ---------------------------------------------------------------------------- submit


async def test_failed_submit_leaves_a_gap_not_a_reuse(async_session_factory: Factory) -> None:
    """The newest run's submit fails: its run goes, its number is never handed out again."""
    pid = await _printer(async_session_factory)
    await _alloc(async_session_factory, pid)  # run 1

    async def failing(payload: PagePayload, job_id: UUID) -> UUID:
        del payload, job_id
        raise RuntimeError("queue full")

    async with async_session_factory() as s:
        with pytest.raises(RuntimeError):
            await runs.submit_with_run(
                s,
                _payload(),
                printer_id=pid,
                kind="calibration_page",
                type_version_id=uuid4(),
                actor="user:admin",
                submit=failing,
            )
    async with async_session_factory() as s:
        count = (await s.execute(select(func.count()).select_from(CalibrationRun))).scalar_one()
    assert count == 1  # run 2 was compensated
    assert (await _alloc(async_session_factory, pid)).run_no == 3  # a gap, never a reuse


async def test_submit_with_run_stamps_the_payload_and_uses_the_run_job_id(
    async_session_factory: Factory,
) -> None:
    pid = await _printer(async_session_factory)
    seen: list[tuple[PagePayload, UUID]] = []

    async def submit(payload: PagePayload, job_id: UUID) -> UUID:
        seen.append((payload, job_id))
        await _job_row(async_session_factory, pid, job_id)
        return job_id

    async with async_session_factory() as s:
        run, job_id = await runs.submit_with_run(
            s,
            _payload(),
            printer_id=pid,
            kind="calibration_page",
            type_version_id=uuid4(),
            actor="user:admin",
            submit=submit,
        )
    [(payload, sent_id)] = seen
    assert sent_id == job_id == run.job_id
    assert payload.detail["run_no"] == 1 and payload.detail["run_id"] == str(run.id)
    assert payload.detail["time"] == run.created_at.astimezone().strftime("%H:%M")
    assert payload.detail["type_key"] == "acme"  # the rest of the detail stays


async def test_cancel_after_the_job_row_keeps_the_run(async_session_factory: Factory) -> None:
    """A cancellation is never compensated: the queue may have committed the job row already
    and print it on the next start (``print_queue.submit_page_job``). The route cannot tell, so
    the run stays even where no row is visible yet."""
    pid = await _printer(async_session_factory)

    async def cancelled(payload: PagePayload, job_id: UUID) -> UUID:
        del payload, job_id
        raise asyncio.CancelledError

    async with async_session_factory() as s:
        with pytest.raises(asyncio.CancelledError):
            await runs.submit_with_run(
                s,
                _payload(),
                printer_id=pid,
                kind="calibration_page",
                type_version_id=uuid4(),
                actor="user:admin",
                submit=cancelled,
            )
    [listed] = await _listed(async_session_factory, pid)
    assert listed.run_no == 1 and listed.submitted is False


async def test_failed_submit_after_the_job_row_keeps_the_run(
    async_session_factory: Factory,
) -> None:
    """An error after the queue stored the row: the job exists, so the run stays."""
    pid = await _printer(async_session_factory)

    async def stored_then_failed(payload: PagePayload, job_id: UUID) -> UUID:
        del payload
        await _job_row(async_session_factory, pid, job_id)
        raise RuntimeError("worker gone")

    async with async_session_factory() as s:
        with pytest.raises(RuntimeError):
            await runs.submit_with_run(
                s,
                _payload(),
                printer_id=pid,
                kind="calibration_page",
                type_version_id=uuid4(),
                actor="user:admin",
                submit=stored_then_failed,
            )
    [listed] = await _listed(async_session_factory, pid)
    assert listed.submitted is True


# ---------------------------------------------------------------------------- media source


async def test_actual_tray_differs_is_stored(async_session_factory: Factory) -> None:
    pid = await _printer(async_session_factory)
    run = await _alloc(async_session_factory, pid)
    payload = runs.stamp(_payload(), run)
    async with async_session_factory() as s:
        await runs.record_media_source(s, payload, "tray-2")
    [listed] = await _listed(async_session_factory, pid)
    assert listed.media_source_reported is True and listed.actual_media_source == "tray-2"


async def test_actual_tray_equal_to_the_request_is_reported_without_a_value(
    async_session_factory: Factory,
) -> None:
    pid = await _printer(async_session_factory)
    run = await _alloc(async_session_factory, pid)
    async with async_session_factory() as s:
        await runs.record_media_source(s, runs.stamp(_payload(), run), "tray-1")
    [listed] = await _listed(async_session_factory, pid)
    assert listed.media_source_reported is True and listed.actual_media_source is None


async def test_printer_without_media_source_reports_not_reported(
    async_session_factory: Factory,
) -> None:
    pid = await _printer(async_session_factory)
    run = await _alloc(async_session_factory, pid)
    async with async_session_factory() as s:
        await runs.record_media_source(s, runs.stamp(_payload(), run), None)
    [listed] = await _listed(async_session_factory, pid)
    assert listed.media_source_reported is False and listed.actual_media_source is None


async def test_printer_supplied_media_source_is_bounded_text(
    async_session_factory: Factory,
) -> None:
    """Security: the printer's answer is stored cleaned and at most 64 characters, never more."""
    pid = await _printer(async_session_factory)
    run = await _alloc(async_session_factory, pid)
    hostile = "tray‮-9\x00\n" + "x" * 300
    async with async_session_factory() as s:
        await runs.record_media_source(s, runs.stamp(_payload(), run), hostile)
    [listed] = await _listed(async_session_factory, pid)
    assert listed.media_source_reported is True
    assert listed.actual_media_source is not None
    assert len(listed.actual_media_source) <= runs.MEDIA_SOURCE_MAX == 64
    assert "‮" not in listed.actual_media_source and "\x00" not in listed.actual_media_source


@pytest.mark.parametrize("answer", ["", "   ", "\x00\x01\u200b"])
async def test_an_unusable_media_source_stays_not_reported(
    async_session_factory: Factory, answer: str
) -> None:
    """Codex review on #440: an answer that cleans to nothing is not a confirmed tray."""
    pid = await _printer(async_session_factory)
    run = await _alloc(async_session_factory, pid)
    async with async_session_factory() as s:
        await runs.record_media_source(s, runs.stamp(_payload(), run), answer)
    [listed] = await _listed(async_session_factory, pid)
    assert listed.media_source_reported is False and listed.actual_media_source is None


async def test_media_source_of_a_payload_without_a_run_is_ignored(
    async_session_factory: Factory,
) -> None:
    """A calibration page stored before 1.8.0 has no run: nothing to record, nothing fails."""
    async with async_session_factory() as s:
        await runs.record_media_source(s, _payload(), "tray-2")
    assert runs.wants_media_source(_payload()) is False


# ---------------------------------------------------------------------------- types, printers


async def test_delete_of_a_calibrated_type_keeps_runs_history_and_check_sheets_readable(
    async_session_factory: Factory,
) -> None:
    pid = await _printer(async_session_factory)
    async with async_session_factory() as s:
        await ts.create_type(
            s, ts.TypeFields("acme", "ACME", "Acme", [], None, None), G48, actor="t"
        )
    async with async_session_factory() as s:
        version = await ts.current_version(s, (await ts.get_type(s, "acme")).id)
    run = await _alloc(async_session_factory, pid, version_id=version.id)
    [before] = await _listed(async_session_factory, pid)
    assert (before.type_key, before.version) == ("acme", 1)
    async with async_session_factory() as s:
        s.add(
            CalibrationHistory(
                printer_id=pid,
                tray="tray-1",
                seq=1,
                action="save",
                method="manual",
                measured_on_type_version_id=version.id,
                run_id=run.id,
            )
        )
        s.add(
            CalibrationCheckSheet(
                printer_id=pid, tray="tray-1", type_version_id=version.id, passes_total=3
            )
        )
        await s.commit()
    async with async_session_factory() as s:
        await ts.delete_type(s, "acme", actor="t")
    [after] = await _listed(async_session_factory, pid)
    assert after.run_no == run.run_no and (after.type_key, after.version) == (None, None)
    async with async_session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(CalibrationHistory))).scalar_one()
        assert (
            await s.execute(select(func.count()).select_from(CalibrationCheckSheet))
        ).scalar_one()
