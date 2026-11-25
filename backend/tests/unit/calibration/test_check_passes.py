# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Check passes on one sheet (#240, design 7.2): which crossings each pass prints."""

from __future__ import annotations

from functools import cache

import pytest
from app.calibration.check_sheet import MAX_PASSES, check_passes
from app.catalog import loader
from app.sheets.exchange import geometry_from_doc
from app.sheets.geometry import Geometry


@cache
def _catalog() -> tuple[tuple[str, Geometry], ...]:
    out = []
    for entry in loader.load_index():
        g = geometry_from_doc(loader.load_entry(entry.key).version.geometry)
        out.append((entry.key, g))
    return tuple(out)


def _grid(key: str) -> tuple[int, int]:
    g = next(g for k, g in _catalog() if k == key)
    return g.cols, g.rows


def _touch(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1])) < 2


@pytest.mark.parametrize("key", [k for k, _ in _catalog()])
def test_passes_disjoint_and_non_adjacent_on_every_catalog_grid(key: str) -> None:
    cols, rows = _grid(key)
    passes = check_passes(cols, rows)
    assert len(passes) <= MAX_PASSES
    for n, crossings in enumerate(passes):
        assert len(crossings) >= 3, (key, n)
        assert len(set(crossings)) == len(crossings)
        for c in crossings:
            assert 1 <= c[0] <= cols - 1 and 1 <= c[1] <= rows - 1, (key, n, c)
        for earlier in passes[:n]:
            hits = [(a, b) for a in crossings for b in earlier if _touch(a, b)]
            assert hits == [], (key, n, hits)


def test_pass_1_is_corners_and_centre() -> None:
    # Avery Zweckform 6121: 5 x 13 labels, 4 x 12 crossings.
    passes = check_passes(5, 13)
    assert set(passes[0]) == {(1, 1), (4, 1), (1, 12), (4, 12), (2, 6)}


def test_the_6121_takes_three_passes_and_the_later_ones_are_shifted_inwards() -> None:
    passes = check_passes(5, 13)
    assert [len(p) for p in passes] == [5, 5, 3]
    # Pass 2: the corners one column and two rows further in.
    assert {(2, 3), (3, 3), (2, 10), (3, 10)} <= set(passes[1])


@pytest.mark.parametrize(("cols", "rows"), [(2, 3), (2, 4), (3, 2), (2, 2), (1, 9), (9, 1), (2, 7)])
def test_grid_with_fewer_than_five_crossings_has_no_passes(cols: int, rows: int) -> None:
    # (2, 7): six crossings in one column lie on one line, nothing can be fitted from them.
    assert check_passes(cols, rows) == []


def test_two_pass_grid_says_two() -> None:
    assert len(check_passes(*_grid("avery-5160"))) == 2  # 3 x 10, 2 x 9 crossings
    assert len(check_passes(*_grid("avery-zweckform-l4736rev"))) == 2


def test_a_grid_of_five_crossings_or_more_has_at_least_the_first_pass() -> None:
    assert len(check_passes(3, 4)) == 1  # 2 x 3 crossings


def test_passes_are_deterministic() -> None:
    assert check_passes(8, 28) == check_passes(8, 28)
    assert [len(p) for p in check_passes(7, 27)] == [5, 5, 5]


# ---------------------------------------------------------------------------- the pages of a pass

from dataclasses import asdict  # noqa: E402
from typing import Any  # noqa: E402

from app.calibration import layout as lay  # noqa: E402
from app.calibration.layout import check_page_layout, printable_area  # noqa: E402
from app.calibration.page import payload_layout, render_calibration_page  # noqa: E402
from app.page_render.pdf import PT_PER_MM  # noqa: E402
from app.page_render.transform import IDENTITY  # noqa: E402
from app.printer_backends.ipp.attributes import Margins  # noqa: E402
from app.services.page_jobs import CalibrationSnapshot, PagePayload  # noqa: E402

from tests.helpers import pdf_inspect as pi  # noqa: E402

SNAP = CalibrationSnapshot(method="manual", dx_hmm=40, dy_hmm=-60)


def _hw(hmm: int) -> Margins:
    return Margins(top=hmm, left=hmm, bottom=hmm, right=hmm)


def _pass_payload(key: str, hw: int, pass_no: int, *, run_no: int = 12) -> PagePayload:
    g = next(g for k, g in _catalog() if k == key)
    doc = loader.load_entry(key)
    passes = check_passes(g.cols, g.rows)
    area = printable_area(g, _hw(hw), IDENTITY)
    detail: dict[str, Any] = {
        "type_key": key,
        "version": 1,
        "printer_name": "Office",
        "date": "2026-11-25",
        "corrected": True,
        "geometry": asdict(g),
        "prefix": doc.type.prefix,
        "allow_qr": doc.type.allow_qr,
        "geometry_diff": {},
        "printable_area": asdict(area),
        "passes_total": len(passes),
        "run_no": run_no,
        "time": "14:05",
        "run_id": "00000000-0000-0000-0000-0000000000aa",
        "pass_no": pass_no,
        "pass_crossings": [list(c) for c in passes[pass_no - 1]],
    }
    return PagePayload(
        kind="calibration_page",
        media=g.page_size,
        tray="tray-1",
        render_dpi=600,
        job_name="Calibration page",
        calibration=SNAP,
        detail=detail,
    )


def _sized(pdf: bytes, mm: float) -> list[pi.PlacedImage]:
    return [i for i in pi.placed_images(pdf) if abs(i.w_pt / PT_PER_MM - mm) < 0.02]


def _longest_stroke_mm(layout: lay.PageLayout) -> float:
    return max(max(abs(x2 - x1), abs(y2 - y1)) for m in layout.marks for x1, y1, x2, y2 in m.segs)


def test_pass_1_draws_cell_numbers_codes_and_blocks() -> None:
    key = "avery-zweckform-6121"
    payload = _pass_payload(key, 500, 1)
    layout = payload_layout(payload)
    pdf = render_calibration_page(payload, compress=False)
    assert len(_sized(pdf, 10.0)) == len(layout.page_codes) == 4
    assert len(_sized(pdf, 8.0)) == len(layout.cell_codes) > 0
    texts = pi.texts(pdf)
    assert {str(n) for n in range(1, 66)} <= set(texts)
    assert texts.count("K1") == 5
    assert any(
        t.startswith("Run #12 · 2026-11-25 14:05") and t.endswith("pass 1 of 3") for t in texts
    )
    assert "No correction applied" not in texts and any(
        t.startswith("Correction applied") for t in texts
    )
    # Crosses only: no gap line and no ruler, so no stroke longer than a mini scale.
    assert _longest_stroke_mm(layout) <= 2 * 2.0 + 1e-6
    assert not any(t.text.startswith(("S", "Z")) for t in layout.labels)
    # The scales sit on pass 1's crossings and nowhere else: two marks per crossing.
    assert len([m for m in layout.marks if len(m.segs) > 2]) == 2 * 5


@pytest.mark.parametrize("pass_no", [2, 3])
def test_passes_2_and_3_draw_no_qr_no_cell_number_no_block(pass_no: int) -> None:
    key = "avery-zweckform-6121"
    payload = _pass_payload(key, 500, pass_no, run_no=12 + pass_no)
    layout = payload_layout(payload)
    pdf = render_calibration_page(payload, compress=False)
    assert pi.placed_images(pdf) == []
    assert layout.page_codes == () and layout.cell_codes == () and layout.cell_numbers == ()
    assert layout.margin_blocks == ()
    texts = pi.texts(pdf)
    crossings = check_passes(5, 13)[pass_no - 1]
    assert texts.count(f"K{pass_no}") == len(crossings)
    assert [t for t in texts if t != f"K{pass_no}" and not t.startswith(f"#{12 + pass_no} ")] == []
    # No corner marks either: every mark is a mini scale of the pass.
    assert all(len(m.segs) > 2 for m in layout.marks)
    assert len(layout.marks) == 2 * len(crossings)
    assert layout.page_code_records() == []


def _r(b: lay.Box) -> tuple[float, float, float, float]:
    return b.rect_mm()


def _overlap(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _slot_cases() -> list[tuple[str, int]]:
    return [
        (k, hw)
        for k, g in _catalog()
        if len(check_passes(g.cols, g.rows)) >= 2
        for hw in (300, 500, 800)
    ]


@pytest.mark.parametrize(("key", "hw"), _slot_cases())
def test_run_line_slots_inside_area_and_clear_of_blocks_spots_and_each_other(
    key: str, hw: int
) -> None:
    payload = _pass_payload(key, hw, 1)
    layout = payload_layout(payload)
    passes_total = payload.detail["passes_total"]
    assert len(layout.run_line_slots) in (0, passes_total - 1)
    area = layout.area.rect_mm()
    quiet = lay.PAGE_QUIET_HMM / 100
    slots = [_r(s) for s in layout.run_line_slots]
    ink = [_r(b.box) for b in layout.margin_blocks]
    ink += [lay._grow(_r(p.box), quiet) for p in layout.page_codes]
    ink += [_r(c.box) for c in layout.cell_codes] + [_r(n.box) for n in layout.cell_numbers]
    ink += [lay.text_box_mm(t.x, t.y, t.text, t.size_pt, t.anchor) for t in layout.labels]
    ink += [lay._seg_rect(s) for m in layout.marks for s in m.segs]
    for s in slots:
        assert area[0] <= s[0] and s[2] <= area[2] and area[1] <= s[1] and s[3] <= area[3]
        assert [i for i in ink if _overlap(s, i)] == [], (key, hw)
    if len(slots) == 2:
        assert not _overlap(slots[0], slots[1]) and slots[0][0] < slots[1][0]
    # The run line of each later pass is drawn inside its slot.
    for pass_no in range(2, passes_total + 1):
        later = payload_layout(_pass_payload(key, hw, pass_no))
        assert later.run_line == (layout.run_line_slots[pass_no - 2] if slots else None)


def test_a_later_pass_with_a_slot_prints_its_run_line_in_it() -> None:
    key = "avery-zweckform-l4736rev"  # deep margins: room for the slot at every edge width
    payload = _pass_payload(key, 500, 2, run_no=14)
    layout = payload_layout(payload)
    assert layout.run_line is not None
    pdf = render_calibration_page(payload, compress=False)
    line = "#14 · 2026-11-25 14:05 · K2 · with correction"
    assert line in pi.texts(pdf)


def test_no_room_for_slots_prints_line_less_passes_and_records_them() -> None:
    # The 6121 on a 5 mm edge has no room for the slots (its record is checked in
    # tests/integration/api/test_check_sheets.py::test_line_less_passes_are_recorded).
    key = "avery-zweckform-6121"
    first = payload_layout(_pass_payload(key, 500, 1))
    assert first.run_line_slots == ()
    for pass_no in (2, 3):
        payload = _pass_payload(key, 500, pass_no)
        assert payload_layout(payload).run_line is None
        texts = pi.texts(render_calibration_page(payload, compress=False))
        assert set(texts) == {f"K{pass_no}"}


def test_every_element_of_every_pass_lies_inside_the_printable_area() -> None:
    for key, hw in _slot_cases():
        for pass_no in range(1, len(check_passes(*_grid(key))) + 1):
            layout = payload_layout(_pass_payload(key, hw, pass_no))
            area = layout.area.rect_mm()
            boxes = [lay._seg_rect(s) for m in layout.marks for s in m.segs]
            boxes += [lay.text_box_mm(t.x, t.y, t.text, t.size_pt, t.anchor) for t in layout.labels]
            for b in boxes:
                assert area[0] - 1e-6 <= b[0] and b[2] <= area[2] + 1e-6, (key, hw, pass_no, b)
                assert area[1] - 1e-6 <= b[1] and b[3] <= area[3] + 1e-6, (key, hw, pass_no, b)


def test_a_calibration_page_is_unchanged_by_the_passes() -> None:
    key = "avery-zweckform-6121"
    payload = _pass_payload(key, 500, 1)
    for k in ("passes_total", "pass_no", "pass_crossings"):
        del payload.detail[k]
    g = next(g for k, g in _catalog() if k == key)
    plain = payload_layout(payload)
    assert plain == check_page_layout(
        g,
        plain.area,
        allow_qr=True,
        passes=1,
        bottom_lines=plain.block("bottom").lines,  # type: ignore[union-attr]
    )
    assert any(t.text == "S1" for t in plain.labels) and not any(
        t.text == "K1" for t in plain.labels
    )
