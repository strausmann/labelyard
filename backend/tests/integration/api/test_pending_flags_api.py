# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The waiting signal in the API (#403, spec 5.6): preview and print say when the sheet code or
the feed mark waits for a page still in the queue, and when the mark has no free place.

The full app (``test_sheet_print_api``'s rig). A ``queued`` job row that no worker picks up keeps
the sheet's margin code or feed mark in flight for the test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from app.models.job import Job
from app.models.sheet import Sheet, SheetCell
from app.sheets.sheet_service import HUB_SOURCE
from sqlalchemy import select
from sqlmodel import col

from tests.integration.api.test_sheet_print_api import _body, _sheet, running
from tests.integration.api.test_sheet_types_api import _factory

pytestmark = pytest.mark.asyncio


async def _sheet_id(code: str) -> UUID:
    async with _factory()() as s:
        row = (await s.execute(select(Sheet).where(col(Sheet.code) == code))).scalar_one()
        return row.id


async def _queued_page(sheet_id: UUID, printer_id: UUID, **detail: Any) -> UUID:
    """A ``queued`` sheet page of the sheet that carries ``detail`` (no worker picks it up)."""
    payload = {"kind": "sheet_page", "detail": {"sheet_id": str(sheet_id), **detail}}
    async with _factory()() as s:
        job = Job(printer_id=printer_id, payload=payload, state="queued")
        s.add(job)
        await s.commit()
        return job.id


async def _preview(rig: Any, body: dict[str, Any]) -> dict[str, Any]:
    resp = await rig.client.post(f"{rig.url()}/preview", json=body, headers=rig.reader)
    assert resp.status_code == 200, resp.text
    out: dict[str, Any] = resp.json()
    return out


async def test_preview_reports_id_pending_with_a_margin_id_in_flight() -> None:
    async with running() as rig:
        code = await _sheet(placement="margin")
        await _queued_page(await _sheet_id(code), rig.printer_id, margin_id={"band": "top"})
        body = _body(code, 2)

        preview = await _preview(rig, body)

        (page,) = preview["pages"]
        assert page["margin_id"] is False
        assert page["id_pending"] is True
        assert page["id_not_printed"] is False
        resp = await rig.print({**body, "expected_cells": preview["expected_cells"]})
        assert resp.status_code == 202, resp.text
        (printed,) = resp.json()["pages"]
        assert printed["margin_id"] is False
        assert printed["id_pending"] is True and printed["id_not_printed"] is False
        # Response-only: nothing of it is stored with the job.
        payload = (await rig.job_payload(printed["job_id"]))["detail"]
        assert "id_pending" not in payload and "feed_mark_pending" not in payload


async def test_preview_reports_the_feed_mark_in_flight() -> None:
    async with running() as rig:
        code = await _sheet(placement="manual")
        await _queued_page(await _sheet_id(code), rig.printer_id, feed_mark={"text": "x"})

        preview = await _preview(rig, _body(code, 1))

        (page,) = preview["pages"]
        assert page["feed_mark"] is False
        assert page["feed_mark_pending"] is True
        assert page["feed_mark_reason"] == "in_flight"
        assert page["id_pending"] is False


async def test_preview_without_waiting_reports_nothing_pending() -> None:
    async with running() as rig:
        code = await _sheet(placement="margin")

        preview = await _preview(rig, _body(code, 1))

        (page,) = preview["pages"]
        assert page["margin_id"] is True and page["feed_mark"] is True
        assert page["id_pending"] is False and page["feed_mark_pending"] is False
        assert page["feed_mark_reason"] is None


async def test_cell_sheet_with_a_reserved_id_cell_reports_id_pending() -> None:
    async with running() as rig:
        code = await _sheet(placement="margin")
        sheet_id = await _sheet_id(code)
        job_id = await _queued_page(sheet_id, rig.printer_id, id_cell=1)
        async with _factory()() as s:
            # A cell sheet is created through its ID-only job; this one's is still queued.
            row = await s.get(Sheet, sheet_id)
            assert row is not None
            row.id_placement = "cell"
            s.add(
                SheetCell(
                    sheet_id=sheet_id,
                    cell_no=1,
                    state="reserved",
                    job_id=job_id,
                    source=HUB_SOURCE,
                    purpose="sheet_id",
                    ref=None,
                    marked_by_hand=False,
                    updated_at=datetime.now(UTC),
                    updated_by="t",
                )
            )
            await s.commit()

        preview = await _preview(rig, _body(code, 1))

        (page,) = preview["pages"]
        assert page["id_pending"] is True and page["id_not_printed"] is False
        assert all(not c["is_id"] for c in page["cells"] if c["state"] == "new")
