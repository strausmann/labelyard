# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""HTTP tests of calibration runs (#336, #240 C; ADR 0029).

Two apps, as in ``test_calibration_page_api.py``: the full app for keys and the printer
restriction, and a small app with a real ``PrintQueue`` over the fake IPP printer, where a page
really goes out, ends, and the printer is asked which tray it took.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import app.models  # noqa: F401
import bcrypt
import httpx
import pytest
import pytest_asyncio
from app.api.routes.calibration import router as calibration_router
from app.api.routes.jobs import router as jobs_router
from app.auth.dependencies import AuthContext
from app.auth.scope_deps import require_admin, require_print, require_read
from app.calibration import runs
from app.calibration.page import render_calibration_page
from app.db.session import get_session
from app.models.api_key import ApiKey
from app.models.calibration_run import CalibrationRun
from app.models.job import Job
from app.models.printer import Printer
from app.models.printer_tray import PrinterTray
from app.printer_backends.ipp.client import IppClient
from app.services import page_jobs
from app.services.job_store_sqlite import SQLiteJobStore
from app.services.page_job_runner import PageJobRunner
from app.services.page_jobs import IppPagePrinter
from app.services.print_queue import PrintQueue
from app.services.producers.ipp_status_producer import IppPrinterState
from app.sheets import type_service as ts
from app.sheets.geometry import Geometry
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers.fake_ipp_printer import FAKE_URI, FakeIppPrinter
from tests.helpers.page_jobs import RecordingHandler, registered

pytestmark = pytest.mark.asyncio

G48 = Geometry("A4", 21000, 29700, 4570, 2120, 4, 12, 4820, 2120, 2130, 985, None, "rectangle")


async def _type(factory: async_sessionmaker[AsyncSession], key: str) -> None:
    async with factory() as s:
        await ts.create_type(
            s, ts.TypeFields(key, key.replace("-", "").upper()[:8], "Acme", [], None, None), G48,
            actor="t",
        )  # fmt: skip


async def _printer(factory: async_sessionmaker[AsyncSession]) -> UUID:
    async with factory() as s:
        p = Printer(
            id=uuid4(),
            name=f"Office ({uuid4().hex[:4]})",
            slug=f"p-{uuid4().hex[:6]}",
            model="m",
            backend="ipp",
            connection={"uri": FAKE_URI},
        )
        s.add(p)
        s.add(PrinterTray(printer_id=p.id, tray_key="tray-1", label="Tray 1", is_default=True))
        await s.commit()
        return p.id


def _factory() -> async_sessionmaker[AsyncSession]:
    import app.db.engine as _engine_module

    return _engine_module.async_session


async def _key(scopes: list[str], allowed: list[str] | None = None) -> dict[str, str]:
    plaintext = f"lh_pat_{uuid4().hex}"
    hashed = bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt(rounds=4)).decode()
    async with _factory()() as s:
        s.add(
            ApiKey(
                name=f"calr-{uuid4().hex[:6]}",
                key_hash=hashed,
                key_prefix=plaintext[:16],
                scopes=scopes,
                allowed_printer_ids=allowed or [],
                enabled=True,
                rate_limit_per_minute=6000,
            )
        )
        await s.commit()
    return {"X-Label-Hub-Key": plaintext}


# ---------------------------------------------------------------------------- the full app


async def test_runs_route_restricted_key_other_printer_403(
    api_client_with_seed: httpx.AsyncClient,
) -> None:
    c = api_client_with_seed
    pid, other = await _printer(_factory()), await _printer(_factory())
    bound = await _key(["read"], allowed=[str(other)])
    r = await c.get(f"/api/v1/printers/{pid}/calibration/tray-1/runs", headers=bound)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error_code"] == "printer_not_allowed"
    own = await c.get(f"/api/v1/printers/{other}/calibration/tray-1/runs", headers=bound)
    assert own.status_code == 200 and own.json() == []
    missing = await c.get(f"/api/v1/printers/{uuid4()}/calibration/tray-1/runs", headers=bound)
    assert missing.status_code == 403  # an unknown id is refused before it is looked up
    anyone = await _key(["read"])
    unknown = await c.get(f"/api/v1/printers/{uuid4()}/calibration/tray-1/runs", headers=anyone)
    assert unknown.status_code == 404 and unknown.json()["detail"]["error_code"] == (
        "printer_not_found"
    )
    refused = await c.get(f"/api/v1/printers/{pid}/calibration/tray-1/runs")
    assert refused.status_code == 401


# ---------------------------------------------------------------------------- queue + fake printer


class _Timeouts(httpx.AsyncBaseTransport):
    """The fake printer, except that a Get-Job-Attributes asking for the tray times out."""

    def __init__(self, fake: FakeIppPrinter) -> None:
        self._inner = httpx.ASGITransport(app=fake)
        self.timed_out = 0
        self.enabled = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        if self.enabled and b"media-col-actual" in body:
            self.timed_out += 1
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return await self._inner.handle_async_request(request)


class _Env:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)

    app: FastAPI
    queue: PrintQueue
    fake: FakeIppPrinter
    transport: _Timeouts
    factory: async_sessionmaker[AsyncSession]
    printer_id: UUID

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://t")

    async def post_page(self, **body: Any) -> str:
        async with self.client() as c:
            r = await c.post(
                f"/api/v1/printers/{self.printer_id}/calibration/tray-1/page",
                json={"type": "cal-run", **body},
            )
        assert r.status_code == 202, r.text
        job_id: str = r.json()["job_id"]
        async with asyncio.timeout(10):
            await self.queue.wait_for_job(job_id, timeout_s=10)
        return job_id

    async def runs(self) -> list[dict[str, Any]]:
        async with self.client() as c:
            r = await c.get(f"/api/v1/printers/{self.printer_id}/calibration/tray-1/runs")
        assert r.status_code == 200, r.text
        data: list[dict[str, Any]] = r.json()
        return data

    async def run_row(self, job_id: str) -> CalibrationRun:
        async with self.factory() as s:
            row = (
                await s.execute(select(CalibrationRun).where(CalibrationRun.job_id == UUID(job_id)))
            ).scalar_one()
        return row

    async def settled(self, job_id: str) -> CalibrationRun:
        """The run once the end-of-job question was answered (or failed), at most 5 s."""
        async with asyncio.timeout(5):
            while True:
                row = await self.run_row(job_id)
                if row.media_source_reported or self.transport.timed_out:
                    return row
                if not self.queue_runner_busy():
                    return row
                await asyncio.sleep(0.02)

    def queue_runner_busy(self) -> bool:
        return bool(self.runner._active)  # type: ignore[attr-defined]


@pytest_asyncio.fixture
async def env(async_session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[_Env]:
    listener = runs.RunMediaSource(async_session_factory)
    with registered(RecordingHandler(), render=render_calibration_page):
        page_jobs.register_media_source_listener("calibration_page", listener)
        pid = await _printer(async_session_factory)
        await _type(async_session_factory, "cal-run")
        fake = FakeIppPrinter(job_script=[3, 5, 9])
        transport = _Timeouts(fake)
        client = IppClient(FAKE_URI, transport=transport)
        page = IppPagePrinter(id=pid, client=client, state=IppPrinterState())
        store = SQLiteJobStore(async_session_factory)
        runner = PageJobRunner(
            store=store, notify=lambda *a, **k: None, poll_interval_s=0.01, max_backoff_s=0
        )
        queue = PrintQueue([], store=store, page_printers=[page], page_runner=runner)
        await queue.start()
        app = FastAPI()
        app.include_router(calibration_router)
        app.include_router(jobs_router)
        app.state.print_queue = queue

        async def _session() -> AsyncIterator[AsyncSession]:
            async with async_session_factory() as s:
                yield s

        auth = AuthContext(source="api-key", scope="admin", api_key_id=uuid4(), ip="127.0.0.1")
        for dep in (require_read, require_print, require_admin):
            app.dependency_overrides[dep] = lambda _c=auth: _c
        app.dependency_overrides[get_session] = _session
        try:
            yield _Env(
                app=app,
                queue=queue,
                fake=fake,
                transport=transport,
                factory=async_session_factory,
                printer_id=pid,
                runner=runner,
            )
        finally:
            await queue.stop(timeout_s=2)
            await client.aclose()
            page_jobs.unregister_media_source_listener("calibration_page", listener)


async def test_a_calibration_page_is_run_1_with_its_number_in_the_payload(env: _Env) -> None:
    job_id = await env.post_page()
    run = await env.run_row(job_id)
    assert (run.run_no, run.kind, run.tray, run.requested_tray) == (1, "calibration_page",
                                                                    "tray-1", "tray-1")  # fmt: skip
    assert run.correction is None and run.actor is not None
    async with env.factory() as s:
        job = await s.get(Job, UUID(job_id))
    assert job is not None
    assert job.payload["detail"]["run_no"] == 1
    assert job.payload["detail"]["run_id"] == str(run.id)
    assert job.payload["detail"]["time"] == run.created_at.astimezone().strftime("%H:%M")
    second = await env.post_page(corrected=True)
    assert (await env.run_row(second)).kind == "check_page"
    listed = await env.runs()
    assert [r["run_no"] for r in listed] == [2, 1]  # newest first
    assert listed[1]["submitted"] is True and listed[1]["retry"] is False
    assert (listed[1]["type_key"], listed[1]["version"]) == ("cal-run", 1)


async def test_retry_keeps_the_run_number_and_time(env: _Env) -> None:
    job_id = await env.post_page()
    async with env.client() as c:
        r = await c.post(f"/api/v1/jobs/{job_id}/retry")
    assert r.status_code in (200, 201, 202), r.text
    retry_id = r.json()["id"]
    async with asyncio.timeout(10):
        await env.queue.wait_for_job(retry_id, timeout_s=10)
    async with env.factory() as s:
        first, again = await s.get(Job, UUID(job_id)), await s.get(Job, UUID(retry_id))
    assert first is not None and again is not None
    for key in ("run_no", "time", "run_id"):
        assert again.payload["detail"][key] == first.payload["detail"][key], key
    listed = await env.runs()
    assert [(r["run_no"], r["retry"], r["job_id"]) for r in listed] == [
        (1, True, retry_id),
        (1, False, job_id),
    ]
    async with env.factory() as s:  # a retry is the same run: no second row, no new number
        assert len((await s.execute(select(CalibrationRun))).scalars().all()) == 1


async def test_failed_submit_through_the_route_leaves_a_gap(env: _Env) -> None:
    await env.post_page()  # run 1
    real = env.queue.submit_page_job

    async def broken(*args: Any, **kwargs: Any) -> UUID:
        del args, kwargs
        raise RuntimeError("queue broke")

    env.queue.submit_page_job = broken  # type: ignore[method-assign]
    try:
        async with env.client() as c:
            with pytest.raises(RuntimeError):
                await c.post(
                    f"/api/v1/printers/{env.printer_id}/calibration/tray-1/page",
                    json={"type": "cal-run"},
                )
    finally:
        env.queue.submit_page_job = real  # type: ignore[method-assign]
    assert [r["run_no"] for r in await env.runs()] == [1]
    third = await env.post_page()
    assert (await env.run_row(third)).run_no == 3


async def test_a_printer_the_queue_does_not_know_leaves_no_run(env: _Env) -> None:
    unknown = await _printer(env.factory)  # in the database, never registered with the queue
    async with env.client() as c:
        r = await c.post(
            f"/api/v1/printers/{unknown}/calibration/tray-1/page", json={"type": "cal-run"}
        )
        assert r.status_code == 409, r.text
        listed = await c.get(f"/api/v1/printers/{unknown}/calibration/tray-1/runs")
    assert listed.json() == []


async def test_actual_tray_reported_by_the_printer_is_stored(env: _Env) -> None:
    env.fake.job_media_source = "tray-2"
    job_id = await env.post_page()
    run = await env.settled(job_id)
    assert run.media_source_reported is True and run.actual_media_source == "tray-2"
    [listed] = await env.runs()
    assert listed["actual_media_source"] == "tray-2" and listed["media_source_reported"] is True


async def test_printer_without_media_source_end_to_end(env: _Env) -> None:
    job_id = await env.post_page()
    run = await env.settled(job_id)
    assert run.media_source_reported is False and run.actual_media_source is None
    [listed] = await env.runs()
    assert listed["media_source_reported"] is False


async def test_get_job_attributes_timeout_leaves_the_run_unreported(env: _Env) -> None:
    env.fake.job_media_source = "tray-2"
    env.transport.enabled = True
    job_id = await env.post_page()
    run = await env.settled(job_id)
    assert env.transport.timed_out == 1  # asked once, after the end; never retried
    assert run.media_source_reported is False and run.actual_media_source is None
    job = await env.queue.get(job_id)
    assert job.state.value == "completed"  # the question never changes the job's outcome
