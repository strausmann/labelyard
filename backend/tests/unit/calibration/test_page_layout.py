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


def _layout(
    key: str, hw: int, *, passes: int = 1, bottom_lines: int | None = None
) -> tuple[Geometry, PageLayout]:
    g, qr = _type(key)
    area = printable_area(g, _hw(hw), IDENTITY)
    return g, check_page_layout(g, area, allow_qr=qr, passes=passes, bottom_lines=bottom_lines)


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
            _assert_at_inner_corner(g, spot, num)


def _assert_at_inner_corner(g: Geometry, spot: lay.CodeSpot, num: lay.CellNumber) -> None:
    """The cell's number sits in the corner opposite the page code, 2.5 mm in from both edges."""
    assert spot.cell is not None
    c, r = (spot.cell - 1) % g.cols, (spot.cell - 1) // g.cols
    x0 = (g.margin_left + c * g.pitch_x) / 100
    y0 = (g.margin_top + r * g.pitch_y) / 100
    x1, y1 = x0 + g.label_w / 100, y0 + g.label_h / 100
    inset = lay.CELL_INSET_HMM / 100
    nb = _r(num.box)
    if spot.corner in ("top_left", "bottom_left"):  # the code is left: the number goes right
        assert nb[2] == pytest.approx(x1 - inset, abs=1e-6), (spot, num)
    else:
        assert nb[0] == pytest.approx(x0 + inset, abs=1e-6), (spot, num)
    if spot.corner in ("top_left", "top_right"):  # the code is on top: the number goes down
        assert nb[3] == pytest.approx(y1 - inset, abs=1e-6), (spot, num)
    else:
        assert nb[1] == pytest.approx(y0 + inset, abs=1e-6), (spot, num)


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
    # Deep margins above and below the grid: both blocks at 6 pt, at the worst offsets.
    g = replace(L4736, page_size=page, page_w=w, page_h=h, rows=8, margin_top=4000)  # type: ignore[arg-type]
    layout = check_page_layout(g, printable_area(g, _hw(hw), WORST_DEV), allow_qr=True, passes=1)
    top, bottom = layout.block("top"), layout.block("bottom")
    assert top is not None and bottom is not None
    assert layout.margin_text == {"top": "full", "bottom": "full"}
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


# ---------------------------------------------------------------------------- fix round 1


def _grid_hull(g: Geometry) -> tuple[float, float, float, float]:
    """Every label and every gap between labels (spec 7.2: nothing across the grid)."""
    return (
        g.margin_left / 100,
        g.margin_top / 100,
        (g.margin_left + (g.cols - 1) * g.pitch_x + g.label_w) / 100,
        (g.margin_top + (g.rows - 1) * g.pitch_y + g.label_h) / 100,
    )


@pytest.mark.parametrize(("key", "hw"), _cases())
def test_no_margin_block_overlaps_the_grid(key: str, hw: int) -> None:
    g, layout = _layout(key, hw)
    hull = _grid_hull(g)
    area = layout.area.rect_mm()
    assert set(layout.margin_text) == {"top", "bottom"}
    for block in layout.margin_blocks:
        b = _r(block.box)
        assert not _overlap(b, hull), (key, hw, block)
        assert area[0] <= b[0] and b[2] <= area[2] and area[1] <= b[1] and b[3] <= area[3]
        assert lay.MIN_BLOCK_PT <= block.size_pt <= lay.BLOCK_PT
        status = layout.margin_text[block.edge]
        assert status == ("full" if block.size_pt == lay.BLOCK_PT else "reduced")
    edges = {b.edge for b in layout.margin_blocks}
    for edge, status in layout.margin_text.items():
        assert (status == "omitted") == (edge not in edges)


def test_6121_on_5_mm_keeps_its_run_information_in_the_margin() -> None:
    # A catalog 6121 differs from nothing and has no gap: correction and checkerboard lines.
    lines = lay.bottom_line_count(geometry_diff=False, checkerboard=True)
    g, layout = _layout("avery-zweckform-6121", 500, bottom_lines=lines)
    hull = _grid_hull(g)
    top, bottom = layout.block("top"), layout.block("bottom")
    assert top is not None and bottom is not None
    # 4.8 mm and 4.86 mm of margin: the 6 pt block does not fit, a smaller one does.
    assert layout.margin_text == {"top": "reduced", "bottom": "reduced"}
    assert _r(top.box)[3] <= hull[1] and _r(bottom.box)[1] >= hull[3]
    assert lay.MIN_BLOCK_PT <= top.size_pt < lay.BLOCK_PT


def test_a_margin_too_narrow_for_4_5_pt_leaves_the_block_out() -> None:
    narrow = replace(L4736, margin_top=900)  # 3 mm of margin above the grid at 5 mm hardware
    layout = check_page_layout(
        narrow, printable_area(narrow, _hw(500), IDENTITY), allow_qr=True, passes=1
    )
    assert layout.margin_text["top"] == "omitted" and layout.block("top") is None
    assert layout.margin_text["bottom"] == "full"


def test_the_block_shrinks_its_font_step_by_step_before_it_is_left_out() -> None:
    sizes = []
    for top_mm in (2000, 1300, 1200, 1150, 1100, 1050, 900):
        g = replace(L4736, margin_top=top_mm)
        block = check_page_layout(
            g, printable_area(g, _hw(500), IDENTITY), allow_qr=True, passes=1
        ).block("top")
        sizes.append(None if block is None else block.size_pt)
    assert sizes[0] == lay.BLOCK_PT and sizes[-1] is None
    shown = [s for s in sizes if s is not None]
    assert shown == sorted(shown, reverse=True)  # never larger in a narrower margin
    assert all(s in lay.BLOCK_PT_STEPS for s in shown)
    assert min(shown) >= lay.MIN_BLOCK_PT == 4.5


def test_the_bottom_block_holds_exactly_the_lines_it_draws() -> None:
    g = L4736  # no row gap: the checkerboard sentence goes along
    area = printable_area(g, _hw(500), IDENTITY)
    plain = check_page_layout(g, area, allow_qr=True, passes=1, bottom_lines=2)
    diffed = check_page_layout(g, area, allow_qr=True, passes=1, bottom_lines=3)
    assert plain.block("bottom").lines == 2  # type: ignore[union-attr]
    assert diffed.block("bottom").lines == 3  # type: ignore[union-attr]
    assert lay.bottom_line_count(geometry_diff=False, checkerboard=True) == 2
    assert lay.bottom_line_count(geometry_diff=True, checkerboard=True) == 3
    assert lay.bottom_line_count(geometry_diff=False, checkerboard=False) == 1


# A 2 x 2 sheet of 34 x 30 mm labels 10 mm from the edges: the page codes go to the corner cells,
# where the page code's quiet zone (to 13.58 mm) reaches into the cell code's own quiet zone
# (13.0 to 14.10 mm) but never its symbol, and the page symbol (to 12.5 mm) stays clear of it.
QUIET_TOUCH = Geometry("A4", 21000, 29700, 3400, 3000, 2, 2, 3400, 3000, 1000, 1000, None,
                       "rectangle")  # fmt: skip


def test_the_cell_code_stays_when_only_the_quiet_zones_touch() -> None:
    g = QUIET_TOUCH
    layout = check_page_layout(g, printable_area(g, _hw(500), IDENTITY), allow_qr=True, passes=1)
    quiet = lay.PAGE_QUIET_HMM / 100
    [spot] = [s for s in layout.page_codes if s.corner == "top_left"]
    assert (spot.spot, spot.cell) == ("corner_cell", 1)
    [code] = [c for c in layout.cell_codes if c.cell == 1]
    assert _overlap(_grow(_r(spot.box), quiet), _r(code.box))  # the quiet zones touch ...
    cell_quiet = lay.CELL_QR_HMM / 100 * lay.QUIET_MODULES / 29
    assert not _overlap(_grow(_r(spot.box), quiet), _grow(_r(code.box), -cell_quiet))
    assert not _overlap(_r(spot.box), _r(code.box))  # ... no ink meets the other's zone
    assert {c.cell for c in layout.cell_codes} == {1, 2, 3, 4}


def test_a_spot_never_overlaps_a_page_code_already_placed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every corner is offered the same clear place: only the first may take it."""
    g = replace(L4736, cols=2)  # two columns end at 103.75 mm: the right half stays blank
    fixed = (150.0, 150.0, 160.0, 160.0)  # inside the area, on that blank paper
    monkeypatch.setattr(lay, "_spot_at", lambda x, y, corner: fixed)
    layout = check_page_layout(g, printable_area(g, _hw(500), IDENTITY), allow_qr=True, passes=1)
    assert len(layout.page_codes) == 1, layout.page_codes
