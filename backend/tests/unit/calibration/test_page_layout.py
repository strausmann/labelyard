# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The calibration page layout inside the printable area (#240, design 7.2): the pure function."""

from __future__ import annotations

from dataclasses import replace
from functools import cache

import pytest
from app.calibration import layout as lay
from app.calibration.fit import crossing_point
from app.calibration.layout import (
    CORNER_LEG_INNER_HMM,
    CORNER_LEG_OUTER_HMM,
    PageLayout,
    PrintableArea,
    check_page_layout,
    printable_area,
)
from app.calibration.page import fit_type_line
from app.catalog import loader
from app.page_render.pdf import text_width_mm
from app.page_render.transform import IDENTITY, Deviation
from app.printer_backends.ipp.attributes import Margins
from app.sheets.exchange import geometry_from_doc
from app.sheets.geometry import Geometry
from app.sheets.planner import margin_bands_hmm

HW_HMM = (300, 500, 800)
L4736 = Geometry("A4", 21000, 29700, 4570, 2120, 4, 12, 4820, 2120, 2130, 985, None, "rectangle")
# Labels 25.4 x 10 mm: an 8 mm cell code does not fit inside 2.5 mm of the label edges.
SMALL = Geometry("A4", 21000, 29700, 2540, 1000, 7, 27, 2790, 1000, 1350, 860, 150, "rectangle")


@cache
def _catalog() -> tuple[tuple[str, Geometry, bool], ...]:
    out = []
    for entry in loader.load_index():
        doc = loader.load_entry(entry.key)
        g = geometry_from_doc(doc.version.geometry)
        if g.cols >= 2 and g.rows >= 2:
            out.append((entry.key, g, doc.type.allow_qr))
    return tuple(out)


def _cases() -> list[tuple[str, int]]:
    return [(key, hw) for key, _, _ in _catalog() for hw in HW_HMM]


def _type(key: str) -> tuple[Geometry, bool]:
    [(g, qr)] = [(g, qr) for k, g, qr in _catalog() if k == key]
    return g, qr


def _hw(hmm: int) -> Margins:
    return Margins(top=hmm, left=hmm, bottom=hmm, right=hmm)


def _layout(key: str, hw: int, *, passes: int = 1) -> tuple[Geometry, PageLayout]:
    g, qr = _type(key)
    return g, check_page_layout(g, printable_area(g, _hw(hw), IDENTITY), allow_qr=qr, passes=passes)


def _r(box: lay.Box) -> tuple[float, float, float, float]:
    return box.rect_mm()


def _overlap(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    eps = 1e-6
    return a[0] < b[2] - eps and b[0] < a[2] - eps and a[1] < b[3] - eps and b[1] < a[3] - eps


def _grow(r: tuple[float, ...], d: float) -> tuple[float, float, float, float]:
    return r[0] - d, r[1] - d, r[2] + d, r[3] + d


def _ink(layout: PageLayout, *, but_cell: int | None = None) -> list[tuple[float, ...]]:
    """Every inked box of the page except the page codes (and one cell's content)."""
    half = 0.05
    ink: list[tuple[float, ...]] = [
        (min(s[0], s[2]) - half, min(s[1], s[3]) - half, max(s[0], s[2]) + half,
         max(s[1], s[3]) + half)
        for m in layout.marks for s in m.segs
    ]  # fmt: skip
    ink += [lay.text_box_mm(t.x, t.y, t.text, t.size_pt, t.anchor) for t in layout.labels]
    ink += [_r(c.box) for c in layout.cell_codes if c.cell != but_cell]
    ink += [_r(n.box) for n in layout.cell_numbers if n.cell != but_cell]
    ink += [_r(b.box) for b in layout.margin_blocks]
    ink += [_r(s) for s in layout.run_line_slots]
    return ink


# ---------------------------------------------------------------------------- the area


def test_the_printable_area_matches_the_margin_bands_of_the_sheet_planner() -> None:
    g = L4736
    dev = Deviation(120, -340, 1_001_000, 999_000, 500)
    hw = Margins(top=420, left=380, bottom=510, right=290)
    area = printable_area(g, hw, dev)
    bands = margin_bands_hmm(g, hw=hw, dev=dev)
    right_edge = g.margin_left + (g.cols - 1) * g.pitch_x + g.label_w
    bottom_edge = g.margin_top + (g.rows - 1) * g.pitch_y + g.label_h
    assert g.margin_top - area.top == bands["top"]
    assert g.margin_left - area.left == bands["left"]
    assert area.right - right_edge == bands["right"]
    assert area.bottom - bottom_edge == bands["bottom"]
    # Unknown margins count as 5 mm, plus the |offset| and 1 mm.
    assert printable_area(g, None, IDENTITY) == PrintableArea(600, 600, 20400, 29100)


# ---------------------------------------------------------------------------- page codes


@pytest.mark.parametrize(("key", "hw"), _cases())
def test_page_codes_clear_of_cell_elements_and_scales(key: str, hw: int) -> None:
    g, layout = _layout(key, hw)
    area = layout.area.rect_mm()
    quiet = lay.PAGE_QUIET_HMM / 100
    assert 0 <= len(layout.page_codes) <= 4
    assert len({s.corner for s in layout.page_codes}) == len(layout.page_codes)
    for spot in layout.page_codes:
        sym = _r(spot.box)
        assert spot.box.w == spot.box.h == 1000  # 10 mm
        assert area[0] - 1e-6 <= sym[0] and sym[2] <= area[2] + 1e-6, spot
        assert area[1] - 1e-6 <= sym[1] and sym[3] <= area[3] + 1e-6, spot
        hits = [i for i in _ink(layout) if _overlap(_grow(sym, quiet), i)]
        assert hits == [], (key, hw, spot, hits[:3])
        if spot.spot == "corner_cell":
            # The cell's number moved to its inner corner, still inside the label.
            [num] = [n for n in layout.cell_numbers if n.cell == spot.cell]
            c, r = (spot.cell - 1) % g.cols, (spot.cell - 1) // g.cols
            x0 = (g.margin_left + c * g.pitch_x) / 100
            y0 = (g.margin_top + r * g.pitch_y) / 100
            nb = _r(num.box)
            assert x0 <= nb[0] and nb[2] <= x0 + g.label_w / 100
            assert y0 <= nb[1] and nb[3] <= y0 + g.label_h / 100


@pytest.mark.parametrize(("key", "hw"), _cases())
def test_every_cell_carries_its_number_clear_of_scales_and_corner_marks(key: str, hw: int) -> None:
    g, layout = _layout(key, hw)
    assert [n.cell for n in layout.cell_numbers] == list(range(1, g.cols * g.rows + 1))
    marks = [s for m in layout.marks for s in m.segs]
    for n in layout.cell_numbers:
        box = _grow(_r(n.box), 0.05)
        assert not any(_overlap(box, _grow(
            (min(s[0], s[2]), min(s[1], s[3]), max(s[0], s[2]), max(s[1], s[3])), 0.05))
            for s in marks), n  # fmt: skip
    for code in layout.cell_codes:
        assert code.box.w == code.box.h == 800  # 8 mm, its quiet zone included
        box = _r(code.box)
        assert not any(_overlap(box, (min(s[0], s[2]), min(s[1], s[3]), max(s[0], s[2]),
                                      max(s[1], s[3]))) for s in marks), code  # fmt: skip


def test_6121_codes_land_in_cells_1_5_61_65_with_their_cell_qr_kept() -> None:
    g, layout = _layout("avery-zweckform-6121", 500)
    spots = {s.corner: (s.spot, s.cell) for s in layout.page_codes}
    assert spots == {
        "top_left": ("corner_cell", 1),
        "top_right": ("corner_cell", 5),
        "bottom_left": ("corner_cell", 61),
        "bottom_right": ("corner_cell", 65),
    }
    assert {c.cell for c in layout.cell_codes} == set(range(1, 66))  # every cell code stays
    # The page code sits 2.5 mm in from the outer corner of cell 1.
    top_left = next(s for s in layout.page_codes if s.corner == "top_left")
    assert (top_left.box.x, top_left.box.y) == (g.margin_left + 250, g.margin_top + 250)
    # The printable corner was too narrow: 9.9 mm of margin less 6 mm leaves 3.9 mm.
    assert g.margin_left - layout.area.left == 390


def test_allow_qr_off_prints_numbers_only_page_codes_stay() -> None:
    g, _ = _type("avery-zweckform-6121")
    area = printable_area(g, _hw(500), IDENTITY)
    on = check_page_layout(g, area, allow_qr=True, passes=1)
    off = check_page_layout(g, area, allow_qr=False, passes=1)
    assert off.cell_codes == ()
    assert [n.cell for n in off.cell_numbers] == list(range(1, 66))
    assert len(off.page_codes) == len(on.page_codes) == 4


def test_cell_too_small_for_8_mm_qr_prints_number_only() -> None:
    layout = check_page_layout(
        SMALL, printable_area(SMALL, _hw(500), IDENTITY), allow_qr=True, passes=1
    )
    assert layout.cell_codes == ()
    assert len(layout.cell_numbers) == SMALL.cols * SMALL.rows
    # A label one millimetre taller than the code needs carries it.
    tall = replace(SMALL, label_h=1400, pitch_y=1400, rows=19)
    tall_layout = check_page_layout(
        tall, printable_area(tall, _hw(500), IDENTITY), allow_qr=True, passes=1
    )
    # Every cell but those whose code gave way to a page code in their corner.
    in_cells = {s.cell for s in tall_layout.page_codes if s.spot == "corner_cell"}
    assert {c.cell for c in tall_layout.cell_codes} == set(range(1, 134)) - in_cells


# ---------------------------------------------------------------------------- corner marks


def _legs(layout: PageLayout) -> list[tuple[float, float, float, float]]:
    """The strokes of the corner marks: the two-segment marks of the layout."""
    return [s for m in layout.marks if len(m.segs) == 2 for s in m.segs]


def test_corner_legs_inner_0_9_outer_1_8_and_dropped_next_to_a_tick() -> None:
    g = L4736
    layout = check_page_layout(g, printable_area(g, _hw(300), IDENTITY), allow_qr=True, passes=1)
    legs = _legs(layout)
    assert (CORNER_LEG_INNER_HMM, CORNER_LEG_OUTER_HMM) == (90, 180)
    # On a 2.5 mm gap every corner next to a crossing comes too near a tick: only outer legs.
    assert {round(abs(s[2] - s[0]) + abs(s[3] - s[1]), 3) for s in legs} == {1.8}
    x0, y0 = g.margin_left / 100, g.margin_top / 100

    def at(x: float, y: float) -> list[float]:
        return sorted(
            round(abs(s[2] - s[0]) + abs(s[3] - s[1]), 3)
            for s in legs
            if abs(s[0] - x) < 1e-6 and abs(s[1] - y) < 1e-6
        )

    assert at(x0, y0) == [1.8, 1.8]  # the grid's outer corner
    # Cell 2's left edge at row 2: next to crossing (1, 1); the 2.5 mm gap puts its legs between
    # two ticks of the scale, so the mark is left out (#348).
    cx, cy = crossing_point(g, 1, 1)
    assert at(cx + (g.pitch_x - g.label_w) / 200, cy) == []
    # A corner next to a crossing of a sheet with a wide gap keeps 0.9 mm legs.
    wide = Geometry("A4", 21000, 29700, 4000, 2000, 3, 4, 4600, 2600, 2000, 1000, None, "rectangle")
    wl = check_page_layout(wide, printable_area(wide, _hw(300), IDENTITY), allow_qr=True, passes=1)
    corner = ((1000 + 4000) / 100, (2000 + 2000) / 100)  # bottom right of cell 1, by crossing 1,1
    found = sorted(
        round(abs(s[2] - s[0]) + abs(s[3] - s[1]), 3)
        for s in _legs(wl)
        if abs(s[0] - corner[0]) < 1e-6 and abs(s[1] - corner[1]) < 1e-6
    )
    assert found == [0.9, 0.9]


@pytest.mark.parametrize(("key", "hw"), _cases())
def test_marks_outside_the_area_are_left_out_as_a_whole(key: str, hw: int) -> None:
    _, layout = _layout(key, hw)
    x0, y0, x1, y1 = layout.area.rect_mm()
    for m in layout.marks:
        for s in m.segs:
            assert x0 - 1e-6 <= min(s[0], s[2]) - 0.05 and max(s[0], s[2]) + 0.05 <= x1 + 1e-6
            assert y0 - 1e-6 <= min(s[1], s[3]) - 0.05 and max(s[1], s[3]) + 0.05 <= y1 + 1e-6
    for t in layout.labels:
        bx0, by0, bx1, by1 = lay.text_box_mm(t.x, t.y, t.text, t.size_pt, t.anchor)
        assert x0 - 1e-6 <= bx0 and bx1 <= x1 + 1e-6 and y0 - 1e-6 <= by0 and by1 <= y1 + 1e-6


# ---------------------------------------------------------------------------- margin blocks


WORST_DEV = Deviation(-1000, -1000, 950_000, 950_000, -2_000_000)
WORST_DIFF = {"PX": 100_000, "PY": 100_000, "MT": 100_000, "ML": 100_000}


@pytest.mark.parametrize("page", ["A4", "Letter"])
@pytest.mark.parametrize("hw", HW_HMM)
def test_margin_block_fits_a4_and_letter(page: str, hw: int) -> None:
    w, h = {"A4": (21000, 29700), "Letter": (21590, 27940)}[page]
    g = replace(L4736, page_size=page, page_w=w, page_h=h)  # type: ignore[arg-type]
    layout = check_page_layout(g, printable_area(g, _hw(hw), WORST_DEV), allow_qr=True, passes=1)
    top, bottom = layout.margin_blocks
    fixed_top = [lay.run_line(999_999, "2026-11-21", "23:59", corrected=c) for c in (False, True)]
    fixed_bottom = [
        lay.correction_line(WORST_DEV),
        lay.geometry_line(WORST_DIFF) or "",
        lay.CHECKERBOARD_SENTENCE,
    ]
    for line in fixed_top:
        assert text_width_mm(line, lay.BLOCK_PT) <= top.box.w / 100
    for line in fixed_bottom:
        assert text_width_mm(line, lay.BLOCK_PT) <= bottom.box.w / 100
    area = layout.area.rect_mm()
    for block in layout.margin_blocks:
        bx0, by0, bx1, by1 = _r(block.box)
        assert area[0] <= bx0 and bx1 <= area[2] and area[1] <= by0 and by1 <= area[3]
    # The type line shortens the printer name first and keeps the version.
    line = fit_type_line("k" * 30, 7, "n" * 200, "t" * 20, room_mm=top.box.w / 100)
    assert " v7 " in line and "k" * 30 in line and "t" * 20 in line and "nnn..." in line
    assert text_width_mm(line, lay.BLOCK_PT) <= top.box.w / 100


def test_footer_raises_only_when_the_fixed_part_cannot_fit() -> None:
    # The degrade order: printer name, then type key, then tray, down to nothing.
    full = fit_type_line("key", 3, "Office", "tray-1", room_mm=200)
    assert full == "key v3 · Office · tray tray-1"
    bare = lay.type_line("", 3, "", "")
    room = text_width_mm(bare, lay.BLOCK_PT) + 0.01
    assert fit_type_line("k" * 50, 3, "n" * 50, "t" * 50, room_mm=room) == bare
    with pytest.raises(ValueError, match="fixed part"):
        fit_type_line("k", 3, "n", "t", room_mm=room - 0.02)
    # Room for the tray only after the printer name and the type key are gone.
    keyed = lay.type_line("", 3, "", "t" * 10)
    shortened = fit_type_line(
        "k" * 30, 3, "n" * 30, "t" * 10, room_mm=text_width_mm(keyed, lay.BLOCK_PT) + 0.01
    )
    assert shortened == keyed


def test_blocks_are_text_independent_and_do_not_overlap_each_other() -> None:
    g = L4736
    layout = check_page_layout(g, printable_area(g, _hw(500), IDENTITY), allow_qr=True, passes=1)
    top, bottom = layout.margin_blocks
    assert (top.edge, bottom.edge) == ("top", "bottom")
    assert top.box.y == layout.area.top and bottom.box.y + bottom.box.h == layout.area.bottom
    assert not _overlap(_r(top.box), _r(bottom.box))
    assert bottom.lines == 3  # L4736 has no row gap: the checkerboard sentence goes along


# ---------------------------------------------------------------------------- run-line slots


@pytest.mark.parametrize(("key", "hw"), _cases())
def test_run_line_slots_inside_the_area_clear_of_blocks_spots_and_each_other(
    key: str, hw: int
) -> None:
    g, layout = _layout(key, hw, passes=3)
    assert len(layout.run_line_slots) in (0, 2)
    area = layout.area.rect_mm()
    grid = (
        g.margin_left / 100,
        g.margin_top / 100,
        (g.margin_left + (g.cols - 1) * g.pitch_x + g.label_w) / 100,
        (g.margin_top + (g.rows - 1) * g.pitch_y + g.label_h) / 100,
    )
    slots = [_r(s) for s in layout.run_line_slots]
    for s in slots:
        assert area[0] <= s[0] and s[2] <= area[2] and area[1] <= s[1] and s[3] <= area[3]
        assert not _overlap(s, grid)
        assert not any(_overlap(s, _r(b.box)) for b in layout.margin_blocks)
        quiet = lay.PAGE_QUIET_HMM / 100
        assert not any(_overlap(s, _grow(_r(p.box), quiet)) for p in layout.page_codes)
    if len(slots) == 2:
        assert not _overlap(slots[0], slots[1]) and slots[0][0] < slots[1][0]  # pass 2 left
    one = check_page_layout(g, layout.area, allow_qr=True, passes=1)
    assert one.run_line_slots == ()


def test_a_sheet_with_room_in_its_margin_gets_both_slots() -> None:
    _, layout = _layout("avery-5160", 300, passes=3)
    assert len(layout.run_line_slots) == 2
    _, two = _layout("avery-5160", 300, passes=2)
    assert len(two.run_line_slots) == 1


def test_the_page_code_records_are_the_drawn_boxes() -> None:
    _, layout = _layout("avery-zweckform-6121", 500)
    records = layout.page_code_records()
    assert [r["cell"] for r in records] == [1, 5, 61, 65]
    assert all(r["size_hmm"] == 1000 for r in records)
    assert records[0]["x_hmm"] == round(layout.page_codes[0].box.x)
