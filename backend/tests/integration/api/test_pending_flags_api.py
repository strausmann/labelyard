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
from app.sheets import type_service as ts
from app.sheets.sheet_service import HUB_SOURCE
from sqlalchemy import select
from sqlmodel import col

from tests.integration.api.test_sheet_print_api import _body, _sheet, _used, running
from tests.integration.api.test_sheet_types_api import _factory

pytestmark = pytest.mark.asyncio

# Both ends of the top band of an acme-t1 sheet are preprinted: the feed mark finds no place.
BOTH_ENDS = [
    {"x": 985, "y": 500, "w": 3000, "h": 1530, "note": "Left"},
    {"x": 17000, "y": 500, "w": 3015, "h": 1530, "note": "Right"},
]


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


async def _ended_page(sheet_id: UUID, printer_id: UUID, **detail: Any) -> UUID:
    """A ``failed`` sheet page of the sheet that carries ``detail``: it has ended."""
    payload = {"kind": "sheet_page", "detail": {"sheet_id": str(sheet_id), **detail}}
    async with _factory()() as s:
        job = Job(printer_id=printer_id, payload=payload, state="failed")
        s.add(job)
        await s.commit()
        return job.id


async def _cell(
    sheet_id: UUID, cell_no: int, state: str, job_id: UUID, *, source: str = HUB_SOURCE
) -> None:
    async with _factory()() as s:
        s.add(
            SheetCell(
                sheet_id=sheet_id,
                cell_no=cell_no,
                state=state,
                job_id=job_id,
                source=source,
                purpose="sheet_id" if source == HUB_SOURCE else "test",
                ref=None,
                marked_by_hand=False,
                updated_at=datetime.now(UTC),
                updated_by="t",
            )
        )
        await s.commit()


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
        assert page["id_pending"] is True and page["id_pending_reason"] == "in_flight"
        assert page["id_not_printed"] is False
        resp = await rig.print({**body, "expected_cells": preview["expected_cells"]})
        assert resp.status_code == 202, resp.text
        (printed,) = resp.json()["pages"]
        assert printed["margin_id"] is False
        assert printed["id_pending"] is True and printed["id_not_printed"] is False
        assert printed["id_pending_reason"] == "in_flight"
        # Response-only: nothing of it is stored with the job.
        detail = (await rig.job_payload(printed["job_id"]))["detail"]
        waiting = {"id_pending", "id_pending_reason", "feed_mark_pending", "feed_mark_reason"}
        assert waiting.isdisjoint(detail)


async def test_preview_reports_the_feed_mark_in_flight() -> None:
    async with running() as rig:
        code = await _sheet(placement="manual")
        await _queued_page(await _sheet_id(code), rig.printer_id, feed_mark={"text": "x"})

        body = _body(code, 1)

        preview = await _preview(rig, body)

        (page,) = preview["pages"]
        assert page["feed_mark"] is False
        assert page["feed_mark_pending"] is True
        assert page["feed_mark_reason"] == "in_flight"
        assert page["id_pending"] is False and page["id_pending_reason"] is None
        resp = await rig.print({**body, "expected_cells": preview["expected_cells"]})
        assert resp.status_code == 202, resp.text
        (printed,) = resp.json()["pages"]
        assert printed["feed_mark"] is False
        assert printed["feed_mark_pending"] is True and printed["feed_mark_reason"] == "in_flight"


async def test_preview_without_waiting_reports_nothing_pending() -> None:
    async with running() as rig:
        code = await _sheet(placement="margin")

        preview = await _preview(rig, _body(code, 1))

        (page,) = preview["pages"]
        assert page["margin_id"] is True and page["feed_mark"] is True
        assert page["id_pending"] is False and page["feed_mark_pending"] is False
        assert page["feed_mark_reason"] is None
        assert page["id_pending_reason"] is None


async def test_margin_id_of_an_unconfirmed_page_reports_unconfirmed() -> None:
    """Operator ruling on #443: the job that carried the code has ended and owns an unconfirmed
    cell; nothing prints the code on its own, a human resolves the cell."""
    async with running() as rig:
        code = await _sheet(placement="margin")
        sheet_id = await _sheet_id(code)
        job_id = await _ended_page(sheet_id, rig.printer_id, margin_id={"band": "top"})
        await _cell(sheet_id, 1, "unconfirmed", job_id, source="api")

        preview = await _preview(rig, _body(code, 1))

        (page,) = preview["pages"]
        assert page["margin_id"] is False and page["id_not_printed"] is False
        assert page["id_pending"] is True and page["id_pending_reason"] == "unconfirmed"


async def test_mark_without_place_stays_due_after_printing() -> None:
    """The page without a place for the mark prints, and the sheet's mark is still not printed."""
    async with running() as rig:
        async with _factory()() as s:
            await ts.patch_type(s, "acme-t1", {"preprint_zones": BOTH_ENDS}, actor="t")
        code = await _sheet(placement="manual")
        body = _body(code, 1)
        preview = await _preview(rig, body)
        (page,) = preview["pages"]
        assert page["feed_mark_reason"] == "no_free_place"

        resp = await rig.print({**body, "expected_cells": preview["expected_cells"]})

        assert resp.status_code == 202, resp.text
        (printed,) = resp.json()["pages"]
        assert printed["feed_mark_pending"] is True
        assert printed["feed_mark_reason"] == "no_free_place"
        sheet = await rig.until_sheet(code, _used(1))
        assert sheet["feed_mark_printed"] is False


@pytest.mark.parametrize(
    ("state", "reason"), [("reserved", "in_flight"), ("unconfirmed", "unconfirmed")]
)
async def test_cell_sheet_with_a_held_id_cell_reports_id_pending(state: str, reason: str) -> None:
    async with running() as rig:
        code = await _sheet(placement="margin")
        sheet_id = await _sheet_id(code)
        job_id = await _queued_page(sheet_id, rig.printer_id, id_cell=1)
        async with _factory()() as s:
            # A cell sheet is created through its ID-only job; this one's is still queued.
            row = await s.get(Sheet, sheet_id)
            assert row is not None
            row.id_placement = "cell"
            await s.commit()
        await _cell(sheet_id, 1, state, job_id)

        preview = await _preview(rig, _body(code, 1))

        (page,) = preview["pages"]
        assert page["id_pending"] is True and page["id_not_printed"] is False
        assert page["id_pending_reason"] == reason
        assert all(not c["is_id"] for c in page["cells"] if c["state"] == "new")
