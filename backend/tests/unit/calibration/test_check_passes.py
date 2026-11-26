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
from app.calibration.page import (  # noqa: E402
    bottom_lines_of,
    later_run_line_box,
    payload_layout,
    render_calibration_page,
)
from app.page_render.pdf import PT_PER_MM  # noqa: E402
from app.printer_backends.ipp.attributes import Margins  # noqa: E402
from app.services.page_jobs import CalibrationSnapshot, PagePayload, snapshot_to_deviation  # noqa: E402

from tests.helpers import pdf_inspect as pi  # noqa: E402

SNAP = CalibrationSnapshot(method="manual", dx_hmm=40, dy_hmm=-60)


def _hw(hmm: int) -> Margins:
    return Margins(top=hmm, left=hmm, bottom=hmm, right=hmm)


def _pass_payload(key: str, hw: int, pass_no: int, *, run_no: int = 12) -> PagePayload:
    g = next(g for k, g in _catalog() if k == key)
    doc = loader.load_entry(key)
    passes = check_passes(g.cols, g.rows)
    dev = snapshot_to_deviation(SNAP)
    area = printable_area(g, _hw(hw), dev, applied=dev)  # as build_payload does for a check
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
    if pass_no == 1:
        detail["reserved_crossings"] = [list(c) for p in passes[1:] for c in p]
    payload = PagePayload(
        kind="calibration_page",
        media=g.page_size,
        tray="tray-1",
        render_dpi=600,
        job_name="Calibration page",
        calibration=SNAP,
        detail=detail,
    )
    if pass_no > 1:  # as the route stamps it: the slot pass 1 (same printer, same correction)
        box = later_run_line_box(payload, _pass_payload(key, hw, 1))
        if box is not None:
            payload.detail["run_line_box"] = box
    return payload


def _sized(pdf: bytes, mm: float) -> list[pi.PlacedImage]:
    return [i for i in pi.placed_images(pdf) if abs(i.w_pt / PT_PER_MM - mm) < 0.02]


def _longest_stroke_mm(layout: lay.PageLayout) -> float:
    return max(max(abs(x2 - x1), abs(y2 - y1)) for m in layout.marks for x1, y1, x2, y2 in m.segs)


def test_pass_1_draws_cell_numbers_codes_and_blocks() -> None:
    key = "avery-zweckform-6121"
    payload = _pass_payload(key, 300, 1)  # a 3 mm edge: both blocks fit beside the correction
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
        if not slots:
            assert later.run_line is None
            continue
        assert later.run_line is not None
        want = layout.run_line_slots[pass_no - 2].rect_mm()
        assert all(abs(x - y) < 1e-6 for x, y in zip(later.run_line.rect_mm(), want, strict=True))


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
        g, plain.area, allow_qr=True, passes=1, bottom_lines=bottom_lines_of(payload)
    )
    assert any(t.text == "S1" for t in plain.labels) and not any(
        t.text == "K1" for t in plain.labels
    )


def test_pass_1_draws_no_corner_mark_where_a_later_pass_puts_its_scale() -> None:
    """Codex on #449: on a gapless sheet the corner marks meet at the crossings; pass 1 must leave
    the crossings of passes 2 and 3 clear, or their scales land among its old corner legs."""
    from app.calibration import scale
    from app.calibration.fit import crossing_point

    for key in ("avery-zweckform-6121", "avery-zweckform-l4736rev", "avery-5160"):
        g = next(g for k, g in _catalog() if k == key)
        later = [c for p in check_passes(g.cols, g.rows)[1:] for c in p]
        assert later
        layout = payload_layout(_pass_payload(key, 500, 1))
        reach = scale.HALF_MM + scale.MIN_GAP_MM
        zones = [
            (x - reach, y - reach, x + reach, y + reach)
            for x, y in (crossing_point(g, c, r) for c, r in later)
        ]
        legs = [lay._seg_rect(s) for m in layout.marks if len(m.segs) == 2 for s in m.segs]
        assert legs, key  # corner marks are still drawn elsewhere
        hits = [leg for leg in legs for z in zones if _overlap(leg, z)]
        assert hits == [], (key, hits[:3])


@pytest.mark.parametrize("hw_later", [500, 300], ids=["same-margins", "margins-now-3-mm"])
def test_a_later_run_line_lands_where_pass_1_reserved_it_after_the_correction_changed(
    hw_later: int,
) -> None:
    """Codex on #449: the correction moved by 2 mm between pass 1 and pass 2, and the printer may
    report other hardware margins by then; the run line of pass 2 lands on paper where pass 1's
    printed layout left its slot, never over pass 1's printed blocks."""
    from app.page_render.transform import Deviation, correction_mm

    key = "avery-zweckform-l4736rev"
    g = next(g for k, g in _catalog() if k == key)
    first = CalibrationSnapshot(method="manual", dy_hmm=200)
    dev1 = Deviation(0, 200, 1_000_000, 1_000_000, 0)
    one = _pass_payload(key, 500, 1)
    one = one.model_copy(
        update={
            "calibration": first,
            "detail": {
                **one.detail,
                "printable_area": asdict(printable_area(g, _hw(500), dev1, applied=dev1)),
            },
        }
    )
    layout1 = payload_layout(one)
    assert layout1.run_line_slots, "L4736 at 5 mm reserves the slot"
    two = _pass_payload(key, hw_later, 2)  # SNAP: dy -0.6 mm, perhaps other margins
    two.detail.pop("run_line_box", None)
    box = later_run_line_box(two, one)
    assert box is not None
    two.detail["run_line_box"] = box
    layout2 = payload_layout(two)
    assert layout2.run_line is not None
    c1 = correction_mm(dev1, g.page_size)
    from app.services.page_jobs import snapshot_to_deviation

    c2 = correction_mm(snapshot_to_deviation(SNAP), g.page_size)

    def on_paper(c: Any, r: tuple[float, float, float, float]) -> tuple[float, ...]:
        pts = [c.apply(p) for p in ((r[0], r[1]), (r[2], r[1]), (r[0], r[3]), (r[2], r[3]))]
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts),
                max(p[1] for p in pts))  # fmt: skip

    reserved = on_paper(c1, layout1.run_line_slots[0].rect_mm())
    printed = on_paper(c2, layout2.run_line.rect_mm())
    assert all(abs(a - b) < 0.02 for a, b in zip(reserved, printed, strict=True))
    for block in layout1.margin_blocks:
        assert not _overlap(printed, on_paper(c1, block.box.rect_mm()))
    # Without the reserved slot (pass 1's run gone) the pass prints its crosses only.
    two.detail.pop("run_line_box")
    assert payload_layout(two).run_line is None
