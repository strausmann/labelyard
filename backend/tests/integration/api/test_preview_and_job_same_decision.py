# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The preview and the job read one stored decision; a retry decides again (#246, spec S2).

The full app with its lifespan (``test_sheet_print_api``'s rig): the real print queue, the fake IPP
printer, SQLite. The rectangle is read from the stored job payload and checked against what the
renderer draws from that payload.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.models.sheet import Sheet
from app.page_render.pdf import PT_PER_MM
from app.printer_backends.ipp.attributes import Margins
from app.services.page_jobs import PagePayload
from app.sheets import type_service as ts
from app.sheets.planner import MARGIN_TEXT_GAP_HMM
from app.sheets.sheet_page import render_sheet_page
from sqlalchemy import select
from sqlmodel import col

from tests.helpers import pdf_inspect as pi
from tests.integration.api.test_page_job_retry import _fail_cleanly, _retried_detail, _retry
from tests.integration.api.test_sheet_print_api import (
    EN_MARK,
    G48,
    _body,
    _set_margins,
    _sheet,
    _used,
    running,
)
from tests.integration.api.test_sheet_types_api import _factory

pytestmark = pytest.mark.asyncio

LOGO = {"x": 985, "y": 600, "w": 4000, "h": 1000, "note": "Logo"}


def _rect_hmm(mm: dict[str, Any]) -> dict[str, Any]:
    return {
        "x_hmm": round(mm["x_mm"] * 100),
        "y_hmm": round(mm["y_mm"] * 100),
        "w_hmm": round(mm["w_mm"] * 100),
        "h_hmm": round(mm["h_mm"] * 100),
        "content": mm["content"],
    }


def _drawn_id_x_hmm(payload: dict[str, Any]) -> int:
    """Where the renderer starts the margin ID text of a stored page, in hmm along x."""
    pdf = render_sheet_page(PagePayload.model_validate(payload), compress=False)
    positions = [
        (x, t)
        for (x, _), t in zip(pi.text_positions(pdf), pi.texts(pdf), strict=True)
        if t != EN_MARK
    ]
    (x, _), *_ = positions
    return round(x / PT_PER_MM * 100)


def _expected_text_x(rect: dict[str, Any]) -> int:
    lead = rect["h_hmm"] + MARGIN_TEXT_GAP_HMM if rect["content"] != "code" else 0
    return int(rect["x_hmm"] + lead)


async def _sheet_row(code: str) -> Sheet:
    async with _factory()() as s:
        return (await s.execute(select(Sheet).where(col(Sheet.code) == code))).scalar_one()


async def _job_state(job_id: str) -> str:
    from uuid import UUID

    from app.models.job import Job

    async with _factory()() as s:
        row = await s.get(Job, UUID(job_id), populate_existing=True)
    assert row is not None
    return str(row.state)


async def _zone_the_logo() -> None:
    async with _factory()() as s:
        await ts.patch_type(s, "acme-t1", {"preprint_zones": [LOGO]}, actor="t")


async def test_preview_and_job_store_the_same_decision() -> None:
    async with running(margins=Margins(top=423, left=423, bottom=423, right=423)) as rig:
        await _zone_the_logo()
        code = await _sheet(placement="margin")
        body = _body(code, 2)
        preview = await rig.client.post(
            f"/api/v1/print/{rig.printer}/sheet/preview", json=body, headers=rig.reader
        )
        assert preview.status_code == 200, preview.text
        (shown,) = preview.json()["pages"]
        assert shown["margin_id_rect"] is not None
        shown_rect = _rect_hmm(shown["margin_id_rect"])
        assert shown_rect["x_hmm"] == LOGO["x"] + LOGO["w"] + 100  # beside the logo, 1 mm clear

        resp = await rig.print({**body, "expected_cells": preview.json()["expected_cells"]})

        assert resp.status_code == 202, resp.text
        (page,) = resp.json()["pages"]
        payload = await rig.job_payload(page["job_id"])
        stored = payload["detail"]["margin_id_rect"]
        assert stored == shown_rect
        assert _drawn_id_x_hmm(payload) == _expected_text_x(stored)  # the job draws the stored one
        await rig.until_sheet(code, _used(1, 2))
        # review round 3: the sheet records where its code sits, for a feed mark printed later
        recorded = await _sheet_row(code)
        assert recorded.margin_id_rect == {
            "x": stored["x_hmm"], "y": stored["y_hmm"], "w": stored["w_hmm"], "h": stored["h_hmm"],
        }  # fmt: skip


async def test_retry_on_a_printer_with_a_larger_margin_moves_the_id() -> None:
    """The first attempt has no room at the top (17 mm lost) and goes to the bottom band; the
    retry, with the top margin back, decides again and takes the top band right of the logo zone.
    The band rule of 1.7.x would draw that ID at the grid's left edge (9.85 mm), the stored
    rectangle puts it at 50.85 mm: the rendered position proves the renderer reads the rectangle."""
    async with running(margins=Margins(top=1700, left=423, bottom=423, right=423)) as rig:
        await _zone_the_logo()
        code = await _sheet(placement="margin")
        first_id, _ = await _fail_cleanly(rig, _body(code, 2))
        first = (await rig.job_payload(first_id))["detail"]
        assert first["margin_id"]["band"] == "bottom"
        await _set_margins(rig, Margins(top=423, left=423, bottom=423, right=423))

        resp = await _retry(rig, first_id)

        detail = await _retried_detail(rig, resp)
        retried = await rig.job_payload(resp.json()["id"])
        rect = detail["margin_id_rect"]
        assert detail["margin_id"]["band"] == "top"
        assert rect != first["margin_id_rect"]
        assert rect["x_hmm"] == LOGO["x"] + LOGO["w"] + 100  # not the grid's left edge
        assert _drawn_id_x_hmm(retried) == _expected_text_x(rect)
        assert _expected_text_x(rect) != 985 + rect["h_hmm"] + MARGIN_TEXT_GAP_HMM
        await rig.until_sheet(code, lambda s: s["id_printed"])


async def test_margin_reprint_keeps_a_printed_feed_mark_free() -> None:
    """Review round 3 (Codex on #439): on a sheet whose feed mark is printed (1.7.x put it at the
    top band's right end), a reprinted margin code with a long text stops before the mark."""
    from app.page_render.transform import Deviation
    from app.sheets.planner import choose_feed_mark

    async with running(margins=Margins(top=423, left=423, bottom=423, right=423)) as rig:
        code = await _sheet(placement="margin")
        async with _factory()() as s:
            row = (await s.execute(select(Sheet).where(col(Sheet.code) == code))).scalar_one()
            row.id_printed = True
            row.feed_mark_printed = True
            await s.commit()
            await ts.patch_type(
                s, "acme-t1", {"vendor": "Acme " + "very long vendor " * 8}, actor="t"
            )

        resp = await rig.client.post(
            f"/api/v1/sheets/{code}/margin-id", json={"printer": "page-1"}, headers=rig.key
        )

        assert resp.status_code in (200, 201, 202), resp.text
        rect = (await rig.job_payload(resp.json()["job_id"]))["detail"]["margin_id_rect"]
        hw = Margins(top=423, left=423, bottom=423, right=423)
        mark, _ = choose_feed_mark(G48, hw=hw, dev=Deviation(), text=EN_MARK)
        assert mark is not None and mark.end == "top_right"
        from app.sheets.free_rects import Rect

        drawn = Rect(rect["x_hmm"], rect["y_hmm"], rect["w_hmm"], rect["h_hmm"])
        mark_box = Rect(mark.x_hmm, 423, mark.width_hmm, 2130 - 100 - 423)
        assert not drawn.overlaps(mark_box.grown(MARGIN_TEXT_GAP_HMM, MARGIN_TEXT_GAP_HMM))


async def test_reprint_of_an_end_aligned_code_keeps_its_place_and_the_printed_mark() -> None:
    """Review round 4 (Codex on #439): the code at the top band's right end (``align end``) and the
    mark at its left end, printed by 1.8.0. The reprint reads the mark's stored box (not a
    right-end mark rebuilt from scratch), so it fits again at the original place."""
    from app.sheets.free_rects import Rect

    margins = Margins(top=423, left=423, bottom=423, right=423)
    async with running(margins=margins) as rig:
        async with _factory()() as s:
            await ts.patch_type(
                s, "acme-t1", {"id_margin_band": "top", "id_margin_align": "end"}, actor="t"
            )
        code = await _sheet(placement="margin")
        resp = await rig.print(_body(code, 1))
        assert resp.status_code == 202, resp.text
        first = (await rig.job_payload(resp.json()["pages"][0]["job_id"]))["detail"]
        assert first["feed_mark_end"] == "top_left"
        await rig.until_sheet(code, lambda s: s["id_printed"] and s["feed_mark_printed"])
        row = await _sheet_row(code)
        assert (
            row.feed_mark_rect is not None
            and row.feed_mark_rect["x"] == first["feed_mark"]["x_hmm"]
        )

        again = await rig.client.post(
            f"/api/v1/sheets/{code}/margin-id", json={"printer": "page-1"}, headers=rig.key
        )

        assert again.status_code in (200, 201, 202), again.text
        rect = (await rig.job_payload(again.json()["job_id"]))["detail"]["margin_id_rect"]
        assert rect == first["margin_id_rect"]  # the original place
        m = row.feed_mark_rect
        drawn = Rect(rect["x_hmm"], rect["y_hmm"], rect["w_hmm"], rect["h_hmm"])
        assert not drawn.overlaps(Rect(m["x"], m["y"], m["w"], m["h"]))


@pytest.mark.parametrize("recorded", [True, False], ids=["stored_mark", "mark_of_1_7"])
async def test_code_due_after_its_mark_printed_keeps_the_mark_free(recorded: bool) -> None:
    """Review round 5 (Codex on #439, planner.py:749): a job printed the feed mark but not the
    code; the next normal print places the still-due code clear of the printed mark. The mark's
    box is the one its page stored (here the top band's left end), or, for a mark printed by
    1.7.x (nothing recorded), the top band's right end."""
    from app.page_render.transform import Deviation
    from app.sheets.free_rects import Rect
    from app.sheets.planner import choose_feed_mark, legacy_mark_box

    margins = Margins(top=423, left=423, bottom=423, right=423)
    async with running(margins=margins) as rig:
        async with _factory()() as s:
            await ts.patch_type(
                s, "acme-t1", {"id_margin_band": "top", "id_margin_align": "end"}, actor="t"
            )
        code = await _sheet(placement="margin")
        first = await rig.print(_body(code, 1))
        assert first.status_code == 202, first.text
        await rig.until_sheet(code, lambda s: s["id_printed"] and s["feed_mark_printed"])
        async with _factory()() as s:
            row = (await s.execute(select(Sheet).where(col(Sheet.code) == code))).scalar_one()
            if recorded:
                m = row.feed_mark_rect
                assert m is not None and m["x"] == G48.margin_left  # the left end
                mark_box = Rect(m["x"], m["y"], m["w"], m["h"])
                align = "start"  # the code now wants the left end, where the mark is
            else:
                row.feed_mark_rect = None  # a mark printed by 1.7.x: at the right end
                mark, _ = choose_feed_mark(G48, hw=margins, dev=Deviation(), text=EN_MARK)
                assert mark is not None and mark.end == "top_right"
                mark_box = legacy_mark_box(G48, mark, margins, Deviation())
                align = "end"  # the code wants the right end, where that mark is
            row.id_printed = False  # the code did not fit that page: still due
            row.margin_id_rect = None
            await s.commit()
            await ts.patch_type(
                s, "acme-t1", {"id_margin_band": "auto", "id_margin_align": align}, actor="t"
            )

        second = await rig.print(_body(code, 1))

        assert second.status_code == 202, second.text
        detail = (await rig.job_payload(second.json()["pages"][0]["job_id"]))["detail"]
        assert detail["feed_mark"] is None  # printed already
        rect = detail["margin_id_rect"]
        assert rect is not None
        drawn = Rect(rect["x_hmm"], rect["y_hmm"], rect["w_hmm"], rect["h_hmm"])
        assert not drawn.overlaps(mark_box)


async def test_mark_after_a_moved_reprint_keeps_both_codes_free() -> None:
    """Review round 5 (Codex on #439, print_service.py:2358): the first page has no room at the
    top (20 mm lost), so the code goes to the bottom band and the mark stays due. The code is
    reprinted with the top margin back and moves to the top band's right end. The reprint has no
    cells, so no outcome settles it: the request itself records its rectangle on the sheet and
    keeps the first one (review round 5, also the restart window Codex raised on 32990a27); the
    next print places the due mark clear of both codes."""
    import asyncio

    from app.sheets.free_rects import Rect

    async with running(margins=Margins(top=2000, left=423, bottom=423, right=423)) as rig:
        async with _factory()() as s:
            await ts.patch_type(s, "acme-t1", {"id_margin_align": "end"}, actor="t")
        code = await _sheet(placement="margin")
        first = await rig.print(_body(code, 1))
        assert first.status_code == 202, first.text
        detail = (await rig.job_payload(first.json()["pages"][0]["job_id"]))["detail"]
        assert detail["margin_id"]["band"] == "bottom" and detail["feed_mark"] is None
        await rig.until_sheet(code, lambda s: s["id_printed"])
        bottom = (await _sheet_row(code)).margin_id_rect
        assert bottom is not None
        await _set_margins(rig, Margins(top=423, left=423, bottom=423, right=423))

        again = await rig.client.post(
            f"/api/v1/sheets/{code}/margin-id", json={"printer": "page-1"}, headers=rig.key
        )

        assert again.status_code in (200, 201, 202), again.text
        r = (await rig.job_payload(again.json()["job_id"]))["detail"]["margin_id_rect"]
        assert r["y_hmm"] < 2130  # the top band now
        reprinted = {"x": r["x_hmm"], "y": r["y_hmm"], "w": r["w_hmm"], "h": r["h_hmm"]}
        queued = (await _sheet_row(code)).margin_id_rect  # recorded with the request
        assert queued is not None and {k: queued[k] for k in "xywh"} == reprinted
        async with asyncio.timeout(15):  # the reprint is over: nothing is in flight any more
            while (await _job_state(again.json()["job_id"])) != "done":
                await asyncio.sleep(0.05)
        row = await _sheet_row(code)
        assert row.margin_id_rect is not None
        recorded = row.margin_id_rect
        assert {k: recorded[k] for k in "xywh"} == reprinted
        assert recorded["earlier"] == [{k: bottom[k] for k in "xywh"}]

        nxt = await rig.print(_body(code, 1))

        assert nxt.status_code == 202, nxt.text
        mark = (await rig.job_payload(nxt.json()["pages"][0]["job_id"]))["detail"]["feed_mark"]
        assert mark is not None
        box = Rect(mark["x_hmm"], 0, 3000, 2030)  # generous: the mark's text from its x
        assert not box.overlaps(Rect(**{k: reprinted[k] for k in "xywh"}))
