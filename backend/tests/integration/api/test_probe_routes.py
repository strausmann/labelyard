# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""HTTP tests of the probe print (#241, design 7.3): the job, the run, the retry, the refusals.

The full app with its lifespan (``running``: the real queue, the registrations of ``main.py``,
the fake IPP printer), so the probe page really reaches the printer, its run is numbered, its
retry is listed with the run and the printer's answer about the tray is recorded. The download's
render limit is tested on the full app without a printer, like the calibration page's.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from app.calibration import probe as probe_module
from app.models.calibration_run import CalibrationRun
from app.models.job import Job
from app.models.printer_status_cache import PrinterStatusCache
from app.models.sheet import Sheet, SheetCell
from app.services import render_slots
from sqlalchemy import select
from sqlmodel import col

from tests.integration.api.test_calibration_page_api import _factory as _api_factory
from tests.integration.api.test_calibration_page_api import _key, _printer, _type
from tests.integration.api.test_page_job_retry import REJECTED, _until_job
from tests.integration.api.test_sheet_print_api import TIMEOUT_S, Rig, _count, running
from tests.integration.api.test_sheet_print_api import _printer as _rig_printer
from tests.integration.api.test_sheet_types_api import _factory, _insert_key

pytestmark = pytest.mark.asyncio

TYPE = "acme-t1"  # created by ``running``


def _url(rig: Rig, printer: UUID | None = None, tray: str = "auto") -> str:
    return f"/api/v1/printers/{printer or rig.printer_id}/calibration/{tray}/probe"


async def _probe(rig: Rig, **kw: Any) -> httpx.Response:
    body = {"type": TYPE, **kw.pop("body", {})}
    return await rig.client.post(
        _url(rig, kw.pop("printer", None)), json=body, headers=kw.pop("headers", rig.key)
    )


def _prints(rig: Rig) -> int:
    return sum(1 for r in rig.fake.received if r.operation == "Print-Job")


def _problem(resp: httpx.Response, status: int, kind: str) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    detail: dict[str, Any] = resp.json()["detail"]
    assert detail.get("type", detail.get("error_code")) == kind, detail
    return detail


async def _runs(rig: Rig) -> list[dict[str, Any]]:
    r = await rig.client.get(
        f"/api/v1/printers/{rig.printer_id}/calibration/auto/runs", headers=rig.reader
    )
    assert r.status_code == 200, r.text
    data: list[dict[str, Any]] = r.json()
    return data


async def _run_of(job_id: str) -> CalibrationRun:
    async with _factory()() as s:
        query = select(CalibrationRun).where(col(CalibrationRun.job_id) == UUID(job_id))
        return (await s.execute(query)).scalar_one()


async def _status(rig: Rig, *, online: bool, age: timedelta) -> None:
    """Write the printer's cached status after the producer's first answer (it polls every 30 s,
    so this one outlives the test)."""
    async with asyncio.timeout(TIMEOUT_S):
        while True:
            async with _factory()() as s:
                row = await s.get(PrinterStatusCache, rig.printer_id)
                # The seeded row has no margins; the producer's first answer has the fake's.
                answered = (
                    row is not None
                    and row.parsed is not None
                    and ((row.parsed.get("summary") or {}).get("margins_hmm") is not None)
                )
                if answered and row is not None and row.parsed is not None:
                    row.parsed = {**row.parsed, "online": online}
                    row.updated_at = datetime.now(UTC) - age
                    await s.commit()
                    return
            await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------- the job and the run


async def test_probe_goes_through_the_queue_and_is_retryable() -> None:
    async with running() as rig:
        sheets, cells = await _count(Sheet), await _count(SheetCell)
        r = await _probe(rig)
        assert r.status_code == 202, r.text
        job_id, run_no = r.json()["job_id"], r.json()["run_no"]
        assert run_no == 1
        await _until_job(job_id, "done")
        assert _prints(rig) == 1
        async with _factory()() as s:
            row = await s.get(Job, UUID(job_id))
        assert row is not None and row.payload["kind"] == "probe_page"
        assert row.payload["detail"]["run_no"] == 1 and row.payload["detail"]["grid"] is False
        run = await _run_of(job_id)
        assert (run.kind, run.run_no, run.tray) == ("probe_page", 1, "auto")
        # Retried from Jobs: the same page again, the same run number.
        retried = await rig.client.post(f"/api/v1/jobs/{job_id}/retry", headers=rig.key)
        assert retried.status_code == 201, retried.text
        retry_id = retried.json()["id"]
        again = await _until_job(retry_id, "done")
        assert again.payload["kind"] == "probe_page"
        assert again.payload["detail"]["run_no"] == 1
        assert again.payload["detail"]["run_id"] == str(run.id)
        listed = await _runs(rig)
        assert [(x["run_no"], x["kind"], x["retry"], x["job_id"]) for x in listed] == [
            (1, "probe_page", True, retry_id),
            (1, "probe_page", False, job_id),
        ]
        # No sheet, no cell (design 7.3).
        assert (await _count(Sheet), await _count(SheetCell)) == (sheets, cells)
        # The next probe takes the next number; the measurement sheet is a probe page too.
        grid = await _probe(rig, body={"grid": True})
        assert grid.status_code == 202 and grid.json()["run_no"] == 2, grid.text
        grid_row = await _until_job(grid.json()["job_id"], "done")
        assert grid_row.payload["detail"]["grid"] is True
        assert grid_row.payload["calibration"] is None


async def test_probe_records_the_tray_the_printer_took() -> None:
    """13a M5: the tray listener of ``main.py`` covers probe pages too."""
    async with running() as rig:
        rig.fake.job_media_source = "tray-2"
        r = await _probe(rig)
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        await _until_job(job_id, "done")
        async with asyncio.timeout(TIMEOUT_S):
            while not (run := await _run_of(job_id)).media_source_reported:
                await asyncio.sleep(0.02)
        assert run.actual_media_source == "tray-2"
        [listed] = await _runs(rig)
        assert listed["actual_media_source"] == "tray-2" and listed["media_source_reported"]


async def test_probe_held_while_device_swapped() -> None:
    async with running() as rig:
        rig.fake.print_status = REJECTED
        first = await _probe(rig)
        assert first.status_code == 202, first.text
        await _until_job(first.json()["job_id"], "failed")
        rig.fake.print_status = 0
        jobs = await _count(Job)
        rig.app.state.print_queue.set_identity_hold(rig.printer_id, True)
        held = await _probe(rig)
        _problem(held, 409, "printer-identity-mismatch")
        retried = await rig.client.post(
            f"/api/v1/jobs/{first.json()['job_id']}/retry", headers=rig.key
        )
        _problem(retried, 409, "printer-identity-mismatch")
        assert await _count(Job) == jobs and _prints(rig) == 1
        rig.app.state.print_queue.set_identity_hold(rig.printer_id, False)
        assert (await _probe(rig)).status_code == 202


# ---------------------------------------------------------------------------- refusals


async def test_probe_disabled_printer_409() -> None:
    async with running(printer_enabled=False) as rig:
        _problem(await _probe(rig), 409, "printer_disabled")
        assert await _count(CalibrationRun) == 0


async def test_probe_unknown_printer_404() -> None:
    async with running() as rig:
        _problem(await _probe(rig, printer=uuid4()), 404, "printer_not_found")


async def test_probe_fresh_offline_status_409() -> None:
    async with running() as rig:
        await _status(rig, online=False, age=timedelta(seconds=0))
        _problem(await _probe(rig), 409, "printer-offline")
        assert await _count(CalibrationRun) == 0 and _prints(rig) == 0


async def test_probe_stale_offline_status_prints() -> None:
    """A stale reading never blocks: the printer may be back (design 7.3)."""
    async with running() as rig:
        await _status(rig, online=False, age=timedelta(hours=1))
        r = await _probe(rig)
        assert r.status_code == 202, r.text
        await _until_job(r.json()["job_id"], "done")


async def test_probe_read_key_403() -> None:
    async with running() as rig:
        _problem(await _probe(rig, headers=rig.reader), 403, "insufficient_scope")
        pdf = await rig.client.get(f"{_url(rig)}/pdf?type={TYPE}", headers=rig.reader)
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


async def test_restricted_key_on_another_printer_is_403() -> None:
    async with running() as rig:
        bound = {"X-Label-Hub-Key": await _insert_key(["print"], [str(uuid4())])}
        _problem(await _probe(rig, headers=bound), 403, "printer_not_allowed")
        pdf = await rig.client.get(f"{_url(rig)}/pdf?type={TYPE}", headers=bound)
        _problem(pdf, 403, "printer_not_allowed")


async def test_probe_queue_unavailable_409() -> None:
    async with running() as rig:
        unknown = await _rig_printer("page-late")  # in the database, never attached to the queue
        _problem(await _probe(rig, printer=unknown), 409, "printer-unavailable")
        async with _factory()() as s:
            left = (await s.execute(select(CalibrationRun))).scalars().all()
        assert [r for r in left if r.printer_id == unknown] == []  # the run was given back


# ---------------------------------------------------------------------------- the download


async def test_probe_pdf_download(api_client_with_seed: httpx.AsyncClient) -> None:
    await _type(_api_factory(), "probe-dl")
    pid = await _printer(_api_factory())
    headers = await _key(["read"])
    url = f"/api/v1/printers/{pid}/calibration/auto/probe/pdf?type=probe-dl"
    plain = await api_client_with_seed.get(url, headers=headers)
    grid = await api_client_with_seed.get(f"{url}&grid=true", headers=headers)
    for resp in (plain, grid):
        assert resp.status_code == 200, resp.text
        assert resp.content.startswith(b"%PDF")
    assert plain.headers["content-disposition"] == 'attachment; filename="probe-probe-dl-v1.pdf"'
    assert grid.headers["content-disposition"] == (
        'attachment; filename="measurement-sheet-probe-dl-v1.pdf"'
    )
    assert plain.content != grid.content


async def test_probe_pdf_render_timeout_504(
    api_client_with_seed: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _type(_api_factory(), "probe-stuck")
    pid = await _printer(_api_factory())
    headers = await _key(["read"])
    release = threading.Event()
    seen: dict[str, Any] = {}
    real_build = probe_module.build_probe_payload

    async def build(session: Any, *args: Any, **kwargs: Any) -> Any:
        seen["session"] = session
        return await real_build(session, *args, **kwargs)

    def hung(payload: Any) -> bytes:
        seen.setdefault("in_transaction_at_render", seen["session"].in_transaction())
        release.wait(10.0)
        return b"%PDF-fake"

    monkeypatch.setattr(probe_module, "build_probe_payload", build)
    monkeypatch.setattr(probe_module, "render_probe_page", hung)
    monkeypatch.setattr(render_slots, "default_timeout_s", lambda: 0.05)
    url = f"/api/v1/printers/{pid}/calibration/auto/probe/pdf?type=probe-stuck"
    try:
        r = await api_client_with_seed.get(url, headers=headers)
        assert r.status_code == 504, r.text
        assert r.json()["detail"]["error_code"] == "render-timeout"
        # No read transaction stayed open while the page drew (#405).
        assert seen["in_transaction_at_render"] is False
        monkeypatch.setattr(render_slots, "default_timeout_s", lambda: 30.0)
        again = await api_client_with_seed.get(url, headers=headers)
        assert again.status_code == 429, again.text
        assert again.json()["detail"]["error_code"] == "too-many-previews"
    finally:
        release.set()
    async with asyncio.timeout(10):
        while render_slots.stuck_renders() or render_slots.free_slots() < 2:
            await asyncio.sleep(0.01)
