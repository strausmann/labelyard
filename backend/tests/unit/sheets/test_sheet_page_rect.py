# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The stored margin ID rectangle and the left-end feed mark in the renderers (#246, S2, S15).

A payload stored by 1.7.x has no ``margin_id_rect`` and must render exactly as 1.7.x did: the
golden digest below is of the drawing operations (positions, sizes, text) the renderer of the
base of milestone 1.8.0 (``a3f0367a``) produced for this payload; the PDF bytes were identical
too (``356ebcdc…``), but the bytes include the cell images, which depend on the Pillow version of
the CI matrix, the operations do not.
"""

from __future__ import annotations

import hashlib
import io
import json
from typing import Any

from app.services.page_jobs import PagePayload
from app.sheets.geometry import Geometry
from app.sheets.sheet_page import (
    PREVIEW_DPI,
    FeedMarkPayload,
    MarginIdPayload,
    MarginRect,
    SheetPageDetail,
    render_preview_png,
    render_sheet_page,
)
from PIL import Image

from tests.helpers import pdf_inspect as pi

L4736 = Geometry("A4", 21000, 29700, 4570, 2120, 4, 12, 4820, 2120, 2130, 985, None, "rectangle")
_G = {
    "page_size": "A4", "page_w": 21000, "page_h": 29700, "label_w": 4570, "label_h": 2120,
    "cols": 4, "rows": 12, "pitch_x": 4820, "pitch_y": 2120, "margin_top": 2130,
    "margin_left": 985, "corner_radius": None, "shape": "rectangle",
}  # fmt: skip
_CELL = {
    "cell": 18,
    "content_type": "qr_two_lines",
    "mapped": False,
    "label_data": {
        "source_app": "t",
        "primary_id": "A-18",
        "title": "Item 18",
        "qr_payload": "https://h.example.invalid/18",
    },
}
EN_MARK = "▲ Top \u2013 this edge first"
# The drawing operations of the payload of ``_stored_1_7_1`` as 1.7.x rendered it (a3f0367a).
GOLDEN_OPS = "1376ba8d7893832643c28cf75ab53d9e89a497987b431c677e5aa3d375e4e937"


def _stored_1_7_1() -> dict[str, Any]:
    """A ``SheetPageDetail`` as 1.7.1 stored it: no ``margin_id_rect``, no ``feed_mark_end``."""
    return {
        "sheet_id": "00000000-0000-0000-0000-000000000001",
        "sheet_code": "L4736-0001",
        "type_key": "l4736rev",
        "type_version_id": "00000000-0000-0000-0000-000000000002",
        "geometry": _G,
        "allow_qr": True,
        "cells": [_CELL],
        "id_cell": None,
        "margin_id": {
            "text": "L4736-0001 · Avery Zweckform L4736REV",
            "qr_payload": "https://hub.example.invalid/sheet/L4736-0001",
            "usable_hmm": 1530,
            "band": "top",
            "corner": "top_left",
            "qr_allowed": True,
            "text_end_hmm": 17100,
        },
        "feed_mark": {
            "text": EN_MARK,
            "x_hmm": 17300,
            "baseline_y_hmm": 1354,
            "size_pt": 7.0,
        },
        "job_summary": {"sheet_code": "L4736-0001", "cell_count": 1},
    }


def _pdf(detail: dict[str, Any]) -> bytes:
    payload = PagePayload(
        kind="sheet_page",
        media="A4",
        tray="auto",
        render_dpi=600,
        job_name="Sheet L4736-0001",
        calibration=None,
        detail=detail,
    )
    return render_sheet_page(payload, compress=False)


def _ops_digest(pdf: bytes) -> str:
    return hashlib.sha256(json.dumps([pi.operations(pdf), pi.texts(pdf)]).encode()).hexdigest()


def test_payload_without_margin_id_rect_renders_as_today() -> None:
    pdf = _pdf(_stored_1_7_1())

    assert _ops_digest(pdf) == GOLDEN_OPS
    assert pi.text_positions(pdf) == [(490.3937, 803.5087), (76.96063, 802.063)]


def test_stored_rectangle_moves_the_drawn_id() -> None:
    """(guard) The renderer draws the stored rectangle when there is one: the text moves with it."""
    plain = _pdf(_stored_1_7_1())
    detail = _stored_1_7_1()
    detail["margin_id_rect"] = {
        "x_hmm": 5085, "y_hmm": 500, "w_hmm": 9000, "h_hmm": 1530, "content": "qr_code_type",
    }  # fmt: skip

    moved = _pdf(detail)

    (_, id_text_plain), (_, id_text_moved) = (pi.text_positions(plain), pi.text_positions(moved))
    k = 72 / 25.4 / 100  # hmm to pt
    assert round(id_text_moved[0] - id_text_plain[0], 2) == round((5085 - 985) * k, 2)


def _left_mark() -> FeedMarkPayload:
    return FeedMarkPayload(text=EN_MARK, x_hmm=985, baseline_y_hmm=1354, size_pt=7.0)


def test_mark_at_the_left_end_renders_at_its_x() -> None:
    detail = _stored_1_7_1()
    detail["margin_id"] = None
    detail["feed_mark"] = _left_mark().model_dump(mode="json")
    detail["feed_mark_end"] = "top_left"

    pdf = _pdf(detail)
    without_end = {**detail}
    del without_end["feed_mark_end"]

    (x, _), *_ = pi.text_positions(pdf)
    assert round(x / (72 / 25.4) * 100) == 985  # the stored x, nothing re-placed
    assert _ops_digest(_pdf(without_end)) == _ops_digest(pdf)  # the end key is only a report
    png = render_preview_png(
        L4736, [], None, render_dpi=600, feed_mark=FeedMarkPayload(**detail["feed_mark"])
    )
    ink = Image.open(io.BytesIO(png)).convert("L").point(lambda v: 255 - v)
    k = PREVIEW_DPI / 2540
    left = ink.crop((round(985 * k), 0, round(4000 * k), round(2130 * k))).getbbox()
    right = ink.crop((round(15000 * k), 0, round(21000 * k), round(2130 * k))).getbbox()
    assert left is not None and right is None


def test_detail_keeps_the_new_keys_and_the_old_payloads_validate() -> None:
    detail = SheetPageDetail.model_validate(
        {
            **_stored_1_7_1(),
            "margin_id_rect": MarginRect(
                x_hmm=1, y_hmm=2, w_hmm=3, h_hmm=4, content="code"
            ).model_dump(),
            "feed_mark_end": "top_left",
        }
    )
    assert detail.margin_id_rect is not None and detail.margin_id_rect.content == "code"
    assert SheetPageDetail.model_validate(_stored_1_7_1()).margin_id_rect is None
    assert isinstance(detail.margin_id, MarginIdPayload)
