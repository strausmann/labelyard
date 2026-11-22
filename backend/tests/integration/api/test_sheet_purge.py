# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""``POST /api/v1/sheets/purge`` (#204, spec 8.3, S5): admin only, a dry run unless asked.

Deletes at most ``limit`` discarded sheets, oldest ``discarded_at`` first, that were discarded
before the cut-off, with their cells, in one write transaction; ``more`` says another call is
needed. The prefix counter is never touched, so a purged code is never issued again.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from app.models.job import Job
from app.models.sheet import Sheet, SheetCell, SheetIdCounter
from app.sheets import sheet_service as ss
from sqlalchemy import select

from tests.integration.api.test_page_job_retry import _fail_cleanly, _job_count, _retry
from tests.integration.api.test_sheet_print_api import (
    Rig,
    _body,
    _count,
    _err,
    _factory,
    _sheet,
    running,
)
from tests.integration.api.test_sheet_types_api import _insert_key

pytestmark = pytest.mark.asyncio


async def _admin() -> dict[str, str]:
    return {"X-Label-Hub-Key": await _insert_key(["admin"])}


async def _discarded(code: str, days_ago: float) -> None:
    async with _factory()() as s:
        await ss.discard_sheet(s, code, actor="t")
    async with _factory()() as s:
        sheet = (await s.execute(select(Sheet).where(Sheet.code == code))).scalar_one()
        sheet.discarded_at = datetime.now(UTC) - timedelta(days=days_ago)
        await s.commit()


async def _purge(rig: Rig, headers: dict[str, str], **body: Any) -> httpx.Response:
    return await rig.client.post("/api/v1/sheets/purge", json=body, headers=headers)


async def _codes() -> list[str]:
    async with _factory()() as s:
        return sorted((await s.execute(select(Sheet.code))).scalars().all())


async def test_purge_dry_run_lists_without_deleting() -> None:
    async with running() as rig:
        code = await _sheet(used=3)
        await _discarded(code, 40)
        admin = await _admin()

        resp = await _purge(rig, admin, older_than_days=30)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"codes": [code], "dry_run": True, "more": False}
        assert await _codes() == [code]
        assert await _count(SheetCell) == 3


async def test_purge_deletes_old_discarded_sheets_and_cells() -> None:
    async with running() as rig:
        old = await _sheet(used=3)
        older = await _sheet(used=2)
        await _discarded(old, 40)
        await _discarded(older, 400)
        admin = await _admin()

        resp = await _purge(rig, admin, older_than_days=30, dry_run=False)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"codes": [older, old], "dry_run": False, "more": False}
        assert await _codes() == []
        assert await _count(SheetCell) == 0
        gone = await rig.client.get(f"/api/v1/sheets/{old}", headers=rig.reader)
        assert gone.status_code == 404


async def test_purge_keeps_active_and_recent() -> None:
    async with running() as rig:
        active = await _sheet(used=1)
        recent = await _sheet(used=1)
        old = await _sheet(used=1)
        await _discarded(recent, 29)
        await _discarded(old, 31)
        admin = await _admin()

        resp = await _purge(rig, admin, older_than_days=30, dry_run=False)

        assert resp.json()["codes"] == [old]
        assert await _codes() == sorted([active, recent])
        assert await _count(SheetCell) == 2


async def test_purge_stops_at_limit_and_reports_more() -> None:
    async with running() as rig:
        codes = [await _sheet() for _ in range(3)]
        for days, code in zip((50, 70, 60), codes, strict=True):
            await _discarded(code, days)
        admin = await _admin()

        dry = await _purge(rig, admin, older_than_days=30, limit=2)
        assert dry.json() == {"codes": [codes[1], codes[2]], "dry_run": True, "more": True}

        first = await _purge(rig, admin, older_than_days=30, limit=2, dry_run=False)
        assert first.json() == {"codes": [codes[1], codes[2]], "dry_run": False, "more": True}
        second = await _purge(rig, admin, older_than_days=30, limit=2, dry_run=False)
        assert second.json() == {"codes": [codes[0]], "dry_run": False, "more": False}


async def test_purge_request_is_bounded() -> None:
    async with running() as rig:
        admin = await _admin()
        for bad in (
            {},
            {"older_than_days": 0},
            {"older_than_days": 36501},
            {"older_than_days": 30, "limit": 0},
            {"older_than_days": 30, "limit": 1001},
            {"older_than_days": 30, "extra": 1},
        ):
            resp = await _purge(rig, admin, **bad)
            assert resp.status_code == 422, bad


async def test_purged_code_is_not_reissued() -> None:
    async with running() as rig:
        code = await _sheet()
        await _discarded(code, 40)
        async with _factory()() as s:
            before = (await s.get(SheetIdCounter, "AT1")).next_value  # type: ignore[union-attr]
        admin = await _admin()

        assert (await _purge(rig, admin, older_than_days=30, dry_run=False)).status_code == 200
        async with _factory()() as s:
            after = await s.get(SheetIdCounter, "AT1")
        assert after is not None and after.next_value == before
        new = await rig.client.post(
            "/api/v1/sheets", json={"type": "acme-t1", "id_placement": "manual"}, headers=rig.key
        )

        assert new.status_code == 201, new.text
        assert new.json()["code"] != code
        assert new.json()["code"] == "AT1-0002"


async def test_purge_needs_admin() -> None:
    async with running() as rig:
        code = await _sheet()
        await _discarded(code, 40)
        for headers in (rig.key, rig.reader):
            resp = await _purge(rig, headers, older_than_days=30, dry_run=False)
            assert resp.status_code == 403, resp.text
        assert await _codes() == [code]


async def test_purge_twice_reports_empty_the_second_time(production_engine) -> None:
    """Two purges at once delete each sheet once; a third finds nothing left."""
    async with running() as rig:
        codes = sorted([await _sheet(used=2) for _ in range(4)])
        for n, code in enumerate(codes):
            await _discarded(code, 40 + n)
        admin = await _admin()

        results = await asyncio.gather(
            *(_purge(rig, admin, older_than_days=30, dry_run=False) for _ in range(2))
        )

        assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
        got = [r.json()["codes"] for r in results]
        assert sorted(got[0] + got[1]) == codes
        assert not set(got[0]) & set(got[1])
        again = await _purge(rig, admin, older_than_days=30, dry_run=False)
        assert again.json() == {"codes": [], "dry_run": False, "more": False}
        assert await _codes() == [] and await _count(SheetCell) == 0


async def test_retry_of_a_job_whose_sheet_was_purged_is_a_clean_error(
    production_engine,
) -> None:
    async with running() as rig:
        code = await _sheet()
        first_id, _ = await _fail_cleanly(rig, _body(code, 2))
        await _discarded(code, 40)
        admin = await _admin()
        jobs = await _job_count()

        retry, purge = await asyncio.gather(
            _retry(rig, first_id),
            _purge(rig, admin, older_than_days=30, dry_run=False),
        )

        assert purge.status_code == 200 and purge.json()["codes"] == [code], purge.text
        assert retry.status_code in (404, 409), retry.text
        assert _err(retry)["error_code"] in ("sheet_discarded", "sheet_purged")
        after = await _retry(rig, first_id)
        assert after.status_code == 409, after.text
        err = _err(after)
        assert err["error_code"] == "sheet_purged"
        assert err["error_detail"] == {"sheet_code": code}
        assert await _job_count() == jobs
        assert await _count(SheetCell) == 0
        async with _factory()() as s:  # the job keeps its payload
            job = await s.get(Job, UUID(first_id))
        assert job is not None and job.payload["detail"]["sheet_code"] == code


async def test_purge_db_error_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.repositories import sheets as repo

    async with running() as rig:
        code = await _sheet(used=3)
        await _discarded(code, 40)
        admin = await _admin()
        real = repo.delete_sheets

        async def broken(session, sheet_ids):  # type: ignore[no-untyped-def]
            await real(session, sheet_ids)  # the cells and the sheets are gone in the session
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr(repo, "delete_sheets", broken)

        transport = httpx.ASGITransport(app=rig.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.post(
                "/api/v1/sheets/purge",
                json={"older_than_days": 30, "dry_run": False},
                headers=admin,
            )

        assert resp.status_code == 500
        assert "disk" not in resp.text
        assert await _codes() == [code]
        assert await _count(SheetCell) == 3
