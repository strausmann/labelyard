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
from app.page_render.pdf import PT_PER_MM
from app.printer_backends.ipp.attributes import Margins
from app.services.page_jobs import PagePayload
from app.sheets import type_service as ts
from app.sheets.planner import MARGIN_TEXT_GAP_HMM
from app.sheets.sheet_page import render_sheet_page

from tests.helpers import pdf_inspect as pi
from tests.integration.api.test_page_job_retry import _fail_cleanly, _retried_detail, _retry
from tests.integration.api.test_sheet_print_api import (
    EN_MARK,
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
