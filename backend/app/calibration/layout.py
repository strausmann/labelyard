# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Where everything on a calibration page goes, inside the printer's printable area (#240, 7.2).

One pure function, ``check_page_layout``, decides the page for the renderer, the print dialog's
warnings, the run record and the tests alike, so they cannot disagree:

* the **printable area**: the page less the printer's hardware margin of each side (5 mm when the
  printer does not report it), the tray's ``|offset|`` on that axis and 1 mm of safety, exactly as
  ``planner.margin_bands_hmm``;
* the **margin blocks** (the run information, top and bottom) and, for a check sheet with later
  passes, the **run-line slots** of passes 2 and 3;
* every **grid mark** (gap lines, mini scales, rulers, corner marks, ``S``/``Z`` labels). A mark
  that would leave the area, or touch a margin block or a slot, is left out as a whole; a gap line
  is clipped instead;
* every cell's **number** and **cell code** (an 8 mm QR), centred and clear of the scales and the
  corner marks; and
* the four **page codes** (a 10 mm QR each), each at the first spot of design 7.2 that is inside
  the area with its quiet zone free of ink: the printable page corner, else the outer corner of the
  corner cell (the cell's number moves to its inner corner and its code is dropped only if it would
  touch the page code), else none.

Lengths of the public types are hundredths of a millimetre; the marks are kept in millimetres, as
the pen draws them.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from app.calibration import scale
from app.calibration.codes import (
    CELL_QR_HMM,
    GEOMETRY_FIELDS,
    PAGE_MIN_VERSION,
    PAGE_QR_HMM,
    QUIET_MODULES,
    GeometryField,
)
from app.calibration.fit import crossing_point
from app.page_render.pdf import text_width_mm
from app.page_render.transform import IDENTITY, Deviation, correction_mm
from app.printer_backends.ipp.attributes import Margins
from app.sheets.errors import CalibrationTypeUnsuitableError
from app.sheets.geometry import Geometry
from app.sheets.planner import DEFAULT_HW_HMM, SAFETY_HMM

__all__ = [
    "BLOCK_PT",
    "BLOCK_PT_STEPS",
    "CELL_INSET_HMM",
    "CELL_NUMBER_PT",
    "CHECKERBOARD_SENTENCE",
    "CORNER_LEG_INNER_HMM",
    "CORNER_LEG_OUTER_HMM",
    "K_LABEL_PT",
    "MIN_BLOCK_PT",
    "RUN_LINE_PT",
    "Box",
    "CellCode",
    "CellNumber",
    "CodeSpot",
    "Corner",
    "MarginBlock",
    "MarginText",
    "Mark",
    "PageLayout",
    "PrintableArea",
    "TextMark",
    "bottom_line_count",
    "check_page_layout",
    "correction_line",
    "geometry_line",
    "needs_checkerboard",
    "pass_run_line",
    "printable_area",
    "run_line",
    "text_box_mm",
    "type_line",
]

CORNER_LEG_INNER_HMM: Final = 90
"""Leg of a corner mark at a label corner next to a crossing (0.9 mm)."""
CORNER_LEG_OUTER_HMM: Final = 180
"""Leg of a corner mark at a corner on the grid's outer edge (1.8 mm)."""
CELL_INSET_HMM: Final = 250
"""Cell content keeps 2.5 mm from the label edges: clear of the corner marks and the scales."""
BLOCK_PT: Final = 6.0
"""Size of the margin block lines."""
BLOCK_PT_STEPS: Final = (6.0, 5.5, 5.0, 4.5)
"""The sizes a margin block tries, largest first, when its margin is too narrow for 6 pt."""
MIN_BLOCK_PT: Final = BLOCK_PT_STEPS[-1]
"""Below this the block is left out (operator decision, 25.11.2026)."""
CELL_NUMBER_PT: Final = 6.0
RUN_LINE_PT: Final = 6.0
BLOCK_LEADING: Final = 1.15
"""Baseline to baseline in a margin block, in ems."""
PAGE_QUIET_HMM: Final = PAGE_QR_HMM * QUIET_MODULES / (17 + 4 * PAGE_MIN_VERSION)
"""The page code's 4-module quiet zone (1.08 mm), blank paper around the 10 mm symbol."""
GAP_HMM: Final = 50
"""Room kept between two laid-out things (0.5 mm)."""
CORNER_RESERVE_HMM: Final = PAGE_QR_HMM + math.ceil(PAGE_QUIET_HMM) + GAP_HMM
"""Width a margin block leaves at each end for the page code at the printable corner."""
WORST_RUN_LINE: Final = "#999999 · 2026-11-21 14:05 · K3 · without correction"
"""The widest run line of a later check pass: a slot is reserved this wide."""
K_LABEL_PT: Final = 5.0
"""Size of the ``K1``/``K2``/``K3`` label next to each cross of a check pass."""
_K_LABEL_GAP_MM: Final = 1.2
"""The ``K`` label keeps this far from its crossing on both axes: clear of the scale's ticks."""

_PT_MM: Final = 25.4 / 72
_EPS: Final = 1e-6
_RULER_STEP_MM: Final = 0.5
_RULER_TICK_MM: Final = 1.0
LABEL_PT: Final = 2.5
_CELL_NUMBER_GAP_MM: Final = 0.5

Corner = Literal["top_left", "top_right", "bottom_left", "bottom_right"]
MarginText = Literal["full", "reduced", "omitted"]
"""A margin block at 6 pt, at a smaller size, or left out (no room in the margin)."""
CORNERS: Final[tuple[Corner, ...]] = ("top_left", "top_right", "bottom_left", "bottom_right")
Anchor = Literal["start", "middle", "end"]
_Seg = tuple[float, float, float, float]
_Rect = tuple[float, float, float, float]  # x0, y0, x1, y1 in mm


@dataclass(frozen=True)
class PrintableArea:
    """The part of the page every element of the calibration page lies in (page hmm, y down)."""

    left: int
    top: int
    right: int
    bottom: int

    def rect_mm(self) -> _Rect:
        return self.left / 100, self.top / 100, self.right / 100, self.bottom / 100


@dataclass(frozen=True)
class Box:
    """A rectangle in hundredths of a millimetre from the page's top left (y down)."""

    x: float
    y: float
    w: float
    h: float

    @classmethod
    def of_mm(cls, r: _Rect) -> Box:
        # Rounded to a millionth of a hundredth: float noise must not change a box's size.
        x0, y0, x1, y1 = (round(v * 100, 6) for v in r)
        return cls(x0, y0, round(x1 - x0, 6), round(y1 - y0, 6))

    def rect_mm(self) -> _Rect:
        return self.x / 100, self.y / 100, (self.x + self.w) / 100, (self.y + self.h) / 100


@dataclass(frozen=True)
class Mark:
    """A drawn mark made of straight strokes (mm); ``dash`` as the pen takes it."""

    segs: tuple[_Seg, ...]
    dash: tuple[float, ...] | None = None


@dataclass(frozen=True)
class TextMark:
    x: float
    y: float
    text: str
    size_pt: float
    anchor: Anchor


@dataclass(frozen=True)
class MarginBlock:
    """Where a block of run information goes: ``lines`` baselines, centred in ``box``."""

    edge: Literal["top", "bottom"]
    box: Box
    lines: int
    size_pt: float = BLOCK_PT

    def baselines_mm(self) -> list[float]:
        em = self.size_pt * _PT_MM
        top = self.box.y / 100
        return [top + em + i * BLOCK_LEADING * em for i in range(self.lines)]

    def baselines_for(self, count: int) -> list[float]:
        """The baselines ``count`` lines are drawn on: the lowest ones, so a line left empty is
        never the one nearest the page edge."""
        if count > self.lines:
            raise ValueError("margin block: more lines than the block holds")
        return self.baselines_mm()[self.lines - count :]


@dataclass(frozen=True)
class CodeSpot:
    """A page code: the 10 mm symbol's box; ``cell`` is the corner cell for the cell spot."""

    corner: Corner
    spot: Literal["page_corner", "corner_cell"]
    cell: int | None
    box: Box


@dataclass(frozen=True)
class CellCode:
    cell: int
    box: Box
    """The 8 mm square of the cell code, its quiet zone included."""


@dataclass(frozen=True)
class CellNumber:
    cell: int
    x: float
    y: float
    """The text's anchor point and baseline, in mm."""
    anchor: Anchor
    box: Box


@dataclass(frozen=True)
class PageLayout:
    area: PrintableArea
    margin_blocks: tuple[MarginBlock, ...]
    page_codes: tuple[CodeSpot, ...]
    cell_codes: tuple[CellCode, ...]
    cell_numbers: tuple[CellNumber, ...]
    run_line_slots: tuple[Box, ...]
    marks: tuple[Mark, ...]
    """Gap lines, mini scales, rulers and corner marks that are drawn."""
    labels: tuple[TextMark, ...]
    """The ``S``/``Z`` gap labels that are drawn."""
    checkerboard: bool
    """The bottom block carries the checkerboard sentence (a sheet without a gap on one axis)."""
    margin_text: dict[str, MarginText]
    """Per block (``top``, ``bottom``): drawn at 6 pt, smaller, or left out for lack of margin."""
    pass_no: int | None = None
    """The pass of a check sheet this page prints; ``None`` for a full calibration or check page."""
    run_line: Box | None = None
    """Pass 2 or 3: the slot its run line is drawn in; ``None`` when pass 1 left no slot."""

    def block(self, edge: Literal["top", "bottom"]) -> MarginBlock | None:
        """The block at ``edge``; ``None`` when the margin had no room for it."""
        return next((b for b in self.margin_blocks if b.edge == edge), None)

    def page_code_records(self) -> list[dict[str, object]]:
        """The drawn page codes as the run stores them (``calibration_runs.page_codes``)."""
        return [
            {
                "corner": s.corner,
                "spot": s.spot,
                "cell": s.cell,
                "x_hmm": round(s.box.x),
                "y_hmm": round(s.box.y),
                "size_hmm": round(s.box.w),
            }
            for s in self.page_codes
        ]


# ---------------------------------------------------------------------------- area


def printable_area(
    g: Geometry, hw: Margins | None, dev: Deviation, *, applied: Deviation | None = None
) -> PrintableArea:
    """The page less the hardware margin (5 mm unknown), ``|offset|`` per axis and 1 mm.

    The same arithmetic as ``planner.margin_bands_hmm``: every side of an axis loses the full
    ``|offset|`` of that axis, whatever its sign. ``applied`` is the correction the page is
    printed with: the renderer moves all content through its inverse (one ``cm``), and a scale
    or rotation there moves the edges further than the offset does. The area then shrinks until
    every corrected corner still lies inside the hardware margin plus 1 mm, so nothing is drawn
    where the printer cannot print (review on #447).
    """
    m = hw or Margins(
        top=DEFAULT_HW_HMM, left=DEFAULT_HW_HMM, bottom=DEFAULT_HW_HMM, right=DEFAULT_HW_HMM
    )
    dx, dy = abs(dev.offset_x_hmm), abs(dev.offset_y_hmm)
    area = [
        float(m.left + dx + SAFETY_HMM),
        float(m.top + dy + SAFETY_HMM),
        float(g.page_w - (m.right + dx + SAFETY_HMM)),
        float(g.page_h - (m.bottom + dy + SAFETY_HMM)),
    ]
    if applied is not None and applied != IDENTITY:
        corr = correction_mm(applied, g.page_size)
        limit = (
            (m.left + SAFETY_HMM) / 100,
            (m.top + SAFETY_HMM) / 100,
            (g.page_w - m.right - SAFETY_HMM) / 100,
            (g.page_h - m.bottom - SAFETY_HMM) / 100,
        )
        for _ in range(50):  # the correction is close to the identity: a few rounds suffice
            x0, y0, x1, y1 = (v / 100 for v in area)
            pts = [corr.apply(p) for p in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
            over = (
                limit[0] - min(p[0] for p in pts),
                limit[1] - min(p[1] for p in pts),
                max(p[0] for p in pts) - limit[2],
                max(p[1] for p in pts) - limit[3],
            )
            if max(over) <= 1e-9:
                break
            for i, o in enumerate(over):
                if o > 0:  # move that side inwards by the overshoot, plus a hundredth
                    area[i] += (o * 100 + 1) * (1 if i < 2 else -1)
    return PrintableArea(
        left=math.ceil(area[0]),
        top=math.ceil(area[1]),
        right=math.floor(area[2]),
        bottom=math.floor(area[3]),
    )


# ---------------------------------------------------------------------------- rectangles


def _inside(r: _Rect, area: _Rect) -> bool:
    return (
        r[0] >= area[0] - _EPS
        and r[1] >= area[1] - _EPS
        and r[2] <= area[2] + _EPS
        and r[3] <= area[3] + _EPS
    )


def _overlap(a: _Rect, b: _Rect) -> bool:
    """The open rectangles share a point (touching edges do not count)."""
    return a[0] < b[2] - _EPS and b[0] < a[2] - _EPS and a[1] < b[3] - _EPS and b[1] < a[3] - _EPS


def _grow(r: _Rect, d: float) -> _Rect:
    return r[0] - d, r[1] - d, r[2] + d, r[3] + d


def _seg_rect(s: _Seg) -> _Rect:
    h = scale.LINE_MM / 2
    return min(s[0], s[2]) - h, min(s[1], s[3]) - h, max(s[0], s[2]) + h, max(s[1], s[3]) + h


def text_box_mm(x: float, y: float, s: str, size_pt: float, anchor: Anchor) -> _Rect:
    """The box a text takes: its width in the embedded font, an em above and a quarter below."""
    width = text_width_mm(s, size_pt)
    em = size_pt * _PT_MM
    left = {"start": x, "middle": x - width / 2, "end": x - width}[anchor]
    return left, y - em, left + width, y + em * 0.25


# ---------------------------------------------------------------------------- grid marks


def needs_checkerboard(g: Geometry) -> bool:
    return not (g.pitch_x > g.label_w and g.pitch_y > g.label_h)


def scale_segments(cx: float, cy: float, *, along_x: bool) -> list[_Seg]:
    """The ticks of the mini scale at a crossing, as (x1, y1, x2, y2)."""
    segs: list[_Seg] = []
    ticks = round(scale.HALF_MM / scale.STEP_MM)
    per_mm = round(1 / scale.STEP_MM)
    for k in range(-ticks, ticks + 1):
        off = k * scale.STEP_MM
        length = (
            scale.TICK_CENTRE_MM
            if k == 0
            else scale.TICK_WHOLE_MM
            if k % per_mm == 0
            else scale.TICK_HALF_MM
        )
        h = length / 2
        segs.append(
            (cx + off, cy - h, cx + off, cy + h)
            if along_x
            else (cx - h, cy + off, cx + h, cy + off)
        )
    return segs


def white_between(a: _Seg, b: _Seg) -> float:
    """The white (mm) between two straight strokes of ``LINE_MM``, edge to edge."""
    (ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1) = _seg_rect(a), _seg_rect(b)
    return math.hypot(max(ax0 - bx1, bx0 - ax1, 0.0), max(ay0 - by1, by0 - ay1, 0.0))


class _Marks:
    """Collects marks under the area and keep-out rule."""

    def __init__(self, area: _Rect, keep_out: Sequence[_Rect]) -> None:
        self.area = area
        self.keep_out = tuple(keep_out)
        self.marks: list[Mark] = []
        self.labels: list[TextMark] = []

    def allowed(self, r: _Rect) -> bool:
        return _inside(r, self.area) and not any(_overlap(r, k) for k in self.keep_out)

    def allowed_all(self, segs: Sequence[_Seg]) -> bool:
        return all(self.allowed(_seg_rect(s)) for s in segs)

    def add(self, segs: Sequence[_Seg], dash: tuple[float, ...] | None = None) -> None:
        """One element; left out as a whole unless every stroke lies in the area, clear of the
        margin blocks and slots."""
        if all(self.allowed(_seg_rect(s)) for s in segs):
            self.marks.append(Mark(tuple(segs), dash))

    def run(
        self, at: float, a: float, b: float, *, vertical: bool, dash: tuple[float, ...] | None
    ) -> None:
        """A long axis-parallel line, clipped to the area and around the keep-out boxes."""
        x0, y0, x1, y1 = self.area
        h = scale.LINE_MM / 2
        across_lo, across_hi, lo, hi = (x0, x1, y0, y1) if vertical else (y0, y1, x0, x1)
        if not (across_lo + h - _EPS <= at <= across_hi - h + _EPS):
            return
        pieces = [(max(a, lo + h), min(b, hi - h))]
        for k in self.keep_out:
            k_across = (k[0], k[2]) if vertical else (k[1], k[3])
            if not (k_across[0] - h < at < k_across[1] + h):
                continue
            k_lo, k_hi = (k[1], k[3]) if vertical else (k[0], k[2])
            nxt: list[tuple[float, float]] = []
            for p, q in pieces:
                if q <= k_lo - h or p >= k_hi + h:
                    nxt.append((p, q))
                    continue
                if p < k_lo - h:
                    nxt.append((p, k_lo - h))
                if q > k_hi + h:
                    nxt.append((k_hi + h, q))
            pieces = nxt
        for p, q in pieces:
            if q - p > _EPS:
                seg = (at, p, at, q) if vertical else (p, at, q, at)
                self.marks.append(Mark((seg,), dash))

    def text(self, x: float, y: float, s: str, *, size_pt: float, anchor: Anchor) -> None:
        if self.allowed(text_box_mm(x, y, s, size_pt, anchor)):
            self.labels.append(TextMark(x, y, s, size_pt, anchor))


DASHED: Final = (1.0, 0.5)
DOTTED: Final = (0.2, 0.4)


def _corner_mark(
    m: _Marks,
    x: float,
    y: float,
    dx: int,
    dy: int,
    *,
    ticks: Sequence[_Seg],
    inner: bool | None = None,
) -> None:
    """An L at a label corner whose legs run along the label's two edges, inwards.

    Next to a crossing (``ticks`` not empty) the legs are 0.9 mm, at the grid's outer edge
    1.8 mm; ``inner`` overrides "next to a crossing" on a check pass, where most crossings carry
    no scale. Left out as a whole when a leg would come nearer than ``scale.MIN_GAP_MM`` of white to
    a tick of a mini scale (#348): a leg cannot be shortened out of the way, it starts at the
    corner, which lies inside the scale.
    """
    near = bool(ticks) if inner is None else inner
    leg = (CORNER_LEG_INNER_HMM if near else CORNER_LEG_OUTER_HMM) / 100
    legs = [(x, y, x + dx * leg, y), (x, y, x, y + dy * leg)]
    if any(white_between(a, b) < scale.MIN_GAP_MM - _EPS for a in legs for b in ticks):
        return
    m.add(legs)


def _k_label(m: _Marks, x: float, y: float, text: str, centre: tuple[float, float]) -> None:
    """The pass label next to a cross, in the quarter towards the page's centre first (so it
    stays out of the corner cells, where the page codes and their moved numbers go), then the
    other quarters; left out when none is free (the cross alone still reads)."""
    gap = _K_LABEL_GAP_MM
    em = K_LABEL_PT * _PT_MM
    right: tuple[float, Anchor] = (x + gap, "start")
    left: tuple[float, Anchor] = (x - gap, "end")
    up = y - gap - 0.25 * em
    down = y + gap + em
    xs = (right, left) if centre[0] >= x else (left, right)
    ys = (down, up) if centre[1] >= y else (up, down)
    spots = [(sx, sy, anchor) for sy in ys for sx, anchor in xs]
    for sx, sy, anchor in spots:
        if m.allowed(text_box_mm(sx, sy, text, K_LABEL_PT, anchor)):
            m.labels.append(TextMark(sx, sy, text, K_LABEL_PT, anchor))
            return


def _pass_marks(
    g: Geometry,
    m: _Marks,
    crossings: Sequence[tuple[int, int]],
    pass_no: int,
    reserved: Sequence[tuple[int, int]] = (),
) -> None:
    """A check pass (design 7.2): the mini scale and a ``K<pass>`` label at each of its crossings,
    never gap lines or edge rulers; pass 1 adds the corner marks of every label, except around
    the ``reserved`` crossings of the later passes: their scales land there later, and old corner
    legs among new ticks would be read wrongly once the correction moved (review on #449)."""
    keep_clear = {(c - 1, r - 1) for c, r in reserved}
    scales: dict[tuple[int, int], list[_Seg]] = {}
    for col_gap, row_gap in crossings:
        x, y = crossing_point(g, col_gap, row_gap)
        along_x = scale_segments(x, y, along_x=True)
        along_y = scale_segments(x, y, along_x=False)
        if m.allowed_all(along_x) and m.allowed_all(along_y):
            m.add(along_x)
            m.add(along_y)
            scales[col_gap - 1, row_gap - 1] = along_x + along_y
            _k_label(m, x, y, f"K{pass_no}", (g.page_w / 200, g.page_h / 200))
    if pass_no != 1:
        return
    for r in range(g.rows):
        for c in range(g.cols):
            lx0, ly0, lx1, ly1 = _label_rect(g, c, r)
            left, top = c - 1, r - 1
            for (cx, cy), (x, y, dx, dy) in (
                ((left, top), (lx0, ly0, 1, 1)),
                ((c, top), (lx1, ly0, -1, 1)),
                ((left, r), (lx0, ly1, 1, -1)),
                ((c, r), (lx1, ly1, -1, -1)),
            ):
                if (cx, cy) in keep_clear:
                    continue
                inner = 0 <= cx < g.cols - 1 and 0 <= cy < g.rows - 1
                _corner_mark(m, x, y, dx, dy, ticks=scales.get((cx, cy), []), inner=inner)


def _grid_marks(g: Geometry, m: _Marks) -> None:
    x0, y0 = g.margin_left / 100, g.margin_top / 100
    gw = ((g.cols - 1) * g.pitch_x + g.label_w) / 100
    gh = ((g.rows - 1) * g.pitch_y + g.label_h) / 100
    col_gap, row_gap = g.pitch_x > g.label_w, g.pitch_y > g.label_h
    col_x = [crossing_point(g, i, 1)[0] for i in range(1, g.cols)]
    row_y = [crossing_point(g, 1, j)[1] for j in range(1, g.rows)]

    for x in col_x:  # solid when there is a gap to centre in, else dotted on the nominal cut
        m.run(x, y0, y0 + gh, vertical=True, dash=None if col_gap else DOTTED)
    for y in row_y:
        m.run(y, x0, x0 + gw, vertical=False, dash=DASHED if row_gap else DOTTED)

    # The ticks of every crossing, by (column line, row line): a corner is checked only against
    # the crossings next to it, not against every tick of the page. A scale is all or nothing:
    # one with ticks missing would be counted wrongly by whoever reads it.
    scales: dict[tuple[int, int], list[_Seg]] = {}
    for i, x in enumerate(col_x):
        for j, y in enumerate(row_y):
            along_x, along_y = (
                scale_segments(x, y, along_x=True),
                scale_segments(x, y, along_x=False),
            )
            m.add(along_x)
            m.add(along_y)
            scales[i, j] = along_x + along_y

    for x in col_x:  # half-millimetre ticks around each gap line, every tick its own element
        for k in range(-6, 7):
            t = x + k * _RULER_STEP_MM
            m.add([(t, y0, t, y0 - _RULER_TICK_MM)])
            m.add([(t, y0 + gh, t, y0 + gh + _RULER_TICK_MM)])
    for y in row_y:
        for k in range(-6, 7):
            t = y + k * _RULER_STEP_MM
            m.add([(x0, t, x0 - _RULER_TICK_MM, t)])
            m.add([(x0 + gw, t, x0 + gw + _RULER_TICK_MM, t)])

    for r in range(g.rows):
        for c in range(g.cols):
            lx0, ly0, lx1, ly1 = _label_rect(g, c, r)
            # A label's left edge borders column line c - 1, its right edge line c; its top edge
            # borders row line r - 1, its bottom edge line r (the outer edges border none).
            left, top = c - 1, r - 1
            _corner_mark(m, lx0, ly0, 1, 1, ticks=scales.get((left, top), []))
            _corner_mark(m, lx1, ly0, -1, 1, ticks=scales.get((c, top), []))
            _corner_mark(m, lx0, ly1, 1, -1, ticks=scales.get((left, r), []))
            _corner_mark(m, lx1, ly1, -1, -1, ticks=scales.get((c, r), []))

    for i, x in enumerate(col_x, start=1):
        m.text(x, y0 - _RULER_TICK_MM - 0.6, f"S{i}", size_pt=LABEL_PT, anchor="middle")
        m.text(x, y0 + gh + _RULER_TICK_MM + 1.1, f"S{i}", size_pt=LABEL_PT, anchor="middle")
    for j, y in enumerate(row_y, start=1):
        m.text(x0 - _RULER_TICK_MM - 0.6, y + 0.3, f"Z{j}", size_pt=LABEL_PT, anchor="end")
        m.text(x0 + gw + _RULER_TICK_MM + 0.6, y + 0.3, f"Z{j}", size_pt=LABEL_PT, anchor="start")


def _label_rect(g: Geometry, c: int, r: int) -> _Rect:
    x = (g.margin_left + c * g.pitch_x) / 100
    y = (g.margin_top + r * g.pitch_y) / 100
    return x, y, x + g.label_w / 100, y + g.label_h / 100


# ---------------------------------------------------------------------------- margin block texts

CHECKERBOARD_SENTENCE: Final = "Peel every other label (checkerboard) if the cut is hard to see."
_PPM: Final = 1_000_000
_GEOMETRY_NAMES: Final[dict[str, str]] = {
    "PX": "pitch x",
    "PY": "pitch y",
    "MT": "margin top",
    "ML": "margin left",
}


def _outcome(corrected: bool) -> str:
    return "check print with correction" if corrected else "printed without correction"


def run_line(
    run_no: int | None,
    day: str,
    time: str | None,
    *,
    corrected: bool,
    pass_of: tuple[int, int] | None = None,
) -> str:
    """The first line of the top block: run, date and time, and what the page is. Never cut.

    ``pass_of`` is ``(pass, passes)`` on pass 1 of a check sheet that takes more than one pass.
    """
    run = f"Run #{run_no}" if run_no is not None else "No run (downloaded PDF)"
    when = f"{day} {time}" if time else day
    line = f"{run} · {when} · Calibration page — {_outcome(corrected)}"
    return f"{line} · pass {pass_of[0]} of {pass_of[1]}" if pass_of else line


def pass_run_line(
    run_no: int | None, day: str, time: str | None, *, pass_no: int, corrected: bool
) -> str:
    """The one line pass 2 or 3 prints in its slot (design 7.2), e.g.
    ``#14 · 2026-11-21 14:05 · K2 · with correction``. Never cut."""
    run = f"#{run_no}" if run_no is not None else "No run"
    when = f"{day} {time}" if time else day
    return f"{run} · {when} · K{pass_no} · {'with' if corrected else 'without'} correction"


def type_line(type_key: str, version: int, printer_name: str, tray: str) -> str:
    """The second line of the top block; the printer name, type key and tray may shorten."""
    return f"{type_key} v{version} · {printer_name} · tray {tray}"


def correction_line(dev: Deviation | None) -> str:
    """The correction the page is printed with, in full (never cut)."""
    if dev is None:
        return "No correction applied"
    return (
        f"Correction applied: dx {dev.offset_x_hmm / 100:.2f} mm · "
        f"dy {dev.offset_y_hmm / 100:.2f} mm · scale x {dev.scale_x_ppm / _PPM:.6f} · "
        f"scale y {dev.scale_y_ppm / _PPM:.6f} · rotation {dev.rotation_udeg / _PPM:.6f}°"
    )


def geometry_line(diff: Mapping[GeometryField, int] | Mapping[str, int]) -> str | None:
    """The sheet-type values that differ from the catalog version, in full; ``None`` for none."""
    parts = [f"{_GEOMETRY_NAMES[k]} {diff[k] / 100:.2f} mm" for k in GEOMETRY_FIELDS if k in diff]
    return "Not as shipped: " + " · ".join(parts) if parts else None


def _widest(size_pt: float, lines: Iterable[str]) -> float:
    return max(text_width_mm(s, size_pt) for s in lines)


def bottom_line_count(*, geometry_diff: bool, checkerboard: bool) -> int:
    """The lines of the bottom block: the correction, then the values not as shipped and the
    checkerboard sentence where they apply."""
    return 1 + int(geometry_diff) + int(checkerboard)


_WORST_DEV: Final = Deviation(-1000, -1000, 950_000, 950_000, -2_000_000)
_WORST_DIFF: Final = dict.fromkeys(GEOMETRY_FIELDS, 100000)


def top_block_width_mm(size_pt: float = BLOCK_PT, *, passes: int = 1) -> float:
    """The width the top block reserves: its longest fixed line (the run line, run 999999; on a
    check sheet of several passes with its ``pass 1 of n``)."""
    pass_of = (1, passes) if passes > 1 else None
    return _widest(
        size_pt,
        [
            run_line(n, "2026-11-21", t, corrected=c, pass_of=pass_of)
            for n, t in ((999_999, "23:59"), (None, None))
            for c in (False, True)
        ],
    )


def bottom_block_width_mm(size_pt: float = BLOCK_PT) -> float:
    """The width the bottom block reserves: its longest fixed line at the bounds of the values."""
    return _widest(
        size_pt,
        [correction_line(_WORST_DEV), geometry_line(_WORST_DIFF) or "", CHECKERBOARD_SENTENCE],
    )


# ---------------------------------------------------------------------------- blocks and slots


def _block_height_mm(lines: int, size_pt: float = BLOCK_PT) -> float:
    em = size_pt * _PT_MM
    return em * (1 + BLOCK_LEADING * (lines - 1) + 0.25)


def _blocks(
    area: _Rect, grid: _Rect, *, bottom_lines: int, passes: int = 1
) -> tuple[tuple[MarginBlock, ...], dict[str, MarginText]]:
    """The top and the bottom block, each in its margin between the area's edge and the grid
    (spec 7.2: nothing across the grid, no label and no gap between labels).

    A block is centred, as wide as its longest fixed line at the bounds of the values and never
    wider than the room between the printable-corner spots. When the margin is too shallow (or
    the room too narrow) at 6 pt, the block tries ``BLOCK_PT_STEPS`` down to ``MIN_BLOCK_PT``;
    below that it is left out (operator decision, 25.11.2026). Its fields never shorten.
    """
    reserve = CORNER_RESERVE_HMM / 100
    room = (area[0] + reserve, area[2] - reserve)
    centre = (area[0] + area[2]) / 2
    pad = GAP_HMM / 100
    gap = GAP_HMM / 100
    bands = {
        "top": (area[1], grid[1] - gap),
        "bottom": (grid[3] + gap, area[3]),
    }
    widths = {
        "top": lambda pt: top_block_width_mm(pt, passes=passes),
        "bottom": bottom_block_width_mm,
    }
    lines = {"top": 2, "bottom": bottom_lines}
    blocks: list[MarginBlock] = []
    text: dict[str, MarginText] = {}
    for edge in ("top", "bottom"):
        lo, hi = bands[edge]
        placed: MarginBlock | None = None
        for pt in BLOCK_PT_STEPS:
            width = widths[edge](pt) + 2 * pad
            height = _block_height_mm(lines[edge], pt)
            if width > room[1] - room[0] + _EPS or height > hi - lo + _EPS:
                continue
            x0, x1 = centre - width / 2, centre + width / 2
            y0 = lo if edge == "top" else hi - height
            placed = MarginBlock(
                edge,
                Box.of_mm((x0, y0, x1, y0 + height)),
                lines[edge],
                pt,
            )
            break
        if placed is None:
            text[edge] = "omitted"
        else:
            blocks.append(placed)
            text[edge] = "full" if placed.size_pt == BLOCK_PT else "reduced"
    return tuple(blocks), text


def _slots(need: int, area: _Rect, blocks: Sequence[MarginBlock], grid: _Rect) -> tuple[Box, ...]:
    """Run-line slots for passes 2 and 3: a row between a margin block and the grid, pass 2 at
    its left, pass 3 at its right, inside the area and clear of the corner spots, the blocks and
    the labels. Bottom first, then top; none when the margin cannot hold them all."""
    if need <= 0:
        return ()
    em = RUN_LINE_PT * _PT_MM
    h = em * 1.25
    w = text_width_mm(WORST_RUN_LINE, RUN_LINE_PT)
    gap = GAP_HMM / 100
    reserve = CORNER_RESERVE_HMM / 100
    left, right = area[0] + reserve, area[2] - reserve
    for block in sorted(blocks, key=lambda b: b.edge != "bottom"):
        _, by0, _, by1 = block.box.rect_mm()
        y0 = by0 - gap - h if block.edge == "bottom" else by1 + gap
        row = [(left, y0, left + w, y0 + h), (right - w, y0, right, y0 + h)][:need]
        others = [b.box.rect_mm() for b in blocks]
        if (
            all(_inside(r, area) and not _overlap(r, grid) for r in row)
            and not any(_overlap(r, o) for r in row for o in others)
            and (len(row) < 2 or not _overlap(_grow(row[0], gap), row[1]))
        ):
            return tuple(Box.of_mm(r) for r in row)
    return ()


# ---------------------------------------------------------------------------- cells


def _cell_content(
    g: Geometry, c: int, r: int, *, allow_qr: bool, area: _Rect, keep_out: Sequence[_Rect]
) -> tuple[CellCode | None, CellNumber | None]:
    """The cell code centred in the label, the number under it (or beside it); the number only
    when the 8 mm code does not fit inside the label less ``CELL_INSET_HMM``."""
    cell = r * g.cols + c + 1
    text = str(cell)
    inset = CELL_INSET_HMM / 100
    lx0, ly0, lx1, ly1 = _label_rect(g, c, r)
    inner = (
        max(lx0 + inset, area[0]),
        max(ly0 + inset, area[1]),
        min(lx1 - inset, area[2]),
        min(ly1 - inset, area[3]),
    )
    cx, cy = (lx0 + lx1) / 2, (ly0 + ly1) / 2
    em = CELL_NUMBER_PT * _PT_MM
    q = CELL_QR_HMM / 200

    def clear(rect: _Rect) -> bool:
        return _inside(rect, inner) and not any(_overlap(rect, k) for k in keep_out)

    def number(x: float, y: float, anchor: Anchor) -> CellNumber | None:
        rect = text_box_mm(x, y, text, CELL_NUMBER_PT, anchor)
        return CellNumber(cell, x, y, anchor, Box.of_mm(rect)) if clear(rect) else None

    if allow_qr:
        qr = (cx - q, cy - q, cx + q, cy + q)
        if clear(qr):
            gap = _CELL_NUMBER_GAP_MM
            below = number(cx, qr[3] + gap + em, "middle")
            beside = number(qr[2] + gap, cy + em * 0.375, "start")
            n = below or beside
            if n is not None:
                return CellCode(cell, Box.of_mm(qr)), n
    # The number alone: centred, else (a label the area cuts, or a margin block over its middle)
    # in the middle of what is left, at the top or at the bottom of it.
    mid = (inner[0] + inner[2]) / 2
    for x, y in (
        (cx, cy + em * 0.375),
        (mid, (inner[1] + inner[3]) / 2 + em * 0.375),
        (mid, inner[1] + em),
        (mid, inner[3] - em * 0.25),
    ):
        alone = number(x, y, "middle")
        if alone is not None:
            return None, alone
    return None, None


def _inner_corner_number(
    g: Geometry, c: int, r: int, corner: Corner, area: _Rect
) -> CellNumber | None:
    """The cell's number at the corner opposite the page code, inset like all cell content."""
    cell = r * g.cols + c + 1
    text = str(cell)
    inset = CELL_INSET_HMM / 100
    lx0, ly0, lx1, ly1 = _label_rect(g, c, r)
    em = CELL_NUMBER_PT * _PT_MM
    right = corner in ("top_left", "bottom_left")  # the number goes to the right side
    lower = corner in ("top_left", "top_right")
    x = lx1 - inset if right else lx0 + inset
    y = ly1 - inset - em * 0.25 if lower else ly0 + inset + em
    anchor: Anchor = "end" if right else "start"
    rect = text_box_mm(x, y, text, CELL_NUMBER_PT, anchor)
    inner = (lx0 + inset, ly0 + inset, lx1 - inset, ly1 - inset)
    if not (_inside(rect, inner) and _inside(rect, area)):
        return None
    return CellNumber(cell, x, y, anchor, Box.of_mm(rect))


# ---------------------------------------------------------------------------- page codes


def _corner_cell(g: Geometry, corner: Corner) -> tuple[int, int]:
    c = 0 if corner in ("top_left", "bottom_left") else g.cols - 1
    r = 0 if corner in ("top_left", "top_right") else g.rows - 1
    return c, r


def _spot_at(x: float, y: float, corner: Corner) -> _Rect:
    """The 10 mm symbol whose ``corner`` lies at (x, y)."""
    s = PAGE_QR_HMM / 100
    left = x if corner in ("top_left", "bottom_left") else x - s
    top = y if corner in ("top_left", "top_right") else y - s
    return left, top, left + s, top + s


def _clear_of(rect: _Rect, ink: Iterable[_Rect]) -> bool:
    return not any(_overlap(rect, i) for i in ink)


# ---------------------------------------------------------------------------- the page


def check_page_layout(
    g: Geometry,
    area: PrintableArea,
    *,
    allow_qr: bool,
    passes: int,
    bottom_lines: int | None = None,
    pass_no: int | None = None,
    crossings: Sequence[tuple[int, int]] | None = None,
    reserved: Sequence[tuple[int, int]] = (),
) -> PageLayout:
    """The calibration page (or pass 1 of a check sheet that takes ``passes`` passes) for ``g``.

    Pure and deterministic: no text of the run goes in, so the print dialog, the run record and
    the renderer agree. Raises nothing for a sheet that leaves no room; the page then carries
    fewer page codes (``page_codes`` 0 to 4), smaller or no margin blocks (``margin_text``)
    and, for ``passes`` above 1, no run-line slots. ``bottom_lines`` is the bottom block's line
    count (``bottom_line_count``); unknown, it reserves room for every line that may appear.
    The page codes carry the run, the sheet type, its version and the correction, so a page
    without margin blocks still names its run.

    ``pass_no`` and ``crossings`` lay out one pass of a check sheet (``check_sheet.check_passes``):
    pass 1 is the full page with crosses (mini scale and ``K1``) only at its crossings and no gap
    lines or rulers, and no corner marks around the ``reserved`` crossings of the later passes;
    passes 2 and 3 are their crosses and, in their run-line slot of pass 1's
    layout (``run_line``), one run line, and nothing else. Without them the page is the full
    calibration or check page.
    """
    if g.cols < 2 or g.rows < 2:
        raise CalibrationTypeUnsuitableError(g.cols, g.rows)
    a = area.rect_mm()
    checker = needs_checkerboard(g)
    x0, y0 = g.margin_left / 100, g.margin_top / 100
    grid = (
        x0,
        y0,
        x0 + ((g.cols - 1) * g.pitch_x + g.label_w) / 100,
        y0 + ((g.rows - 1) * g.pitch_y + g.label_h) / 100,
    )
    if bottom_lines is None:
        bottom_lines = bottom_line_count(geometry_diff=True, checkerboard=checker)
    blocks, margin_text = _blocks(a, grid, bottom_lines=bottom_lines, passes=passes)
    slots = _slots(min(max(passes - 1, 0), 2), a, blocks, grid)
    keep_out = [b.box.rect_mm() for b in blocks] + [s.rect_mm() for s in slots]

    marks = _Marks(a, keep_out)
    if crossings is None:
        _grid_marks(g, marks)
    else:
        if pass_no is None:
            raise ValueError("check pass: crossings without a pass number")
        _pass_marks(g, marks, crossings, pass_no, reserved)
    if pass_no is not None and pass_no > 1:
        # Nothing of pass 1 again: printed after a correction it would land offset over it.
        return PageLayout(
            area=area,
            margin_blocks=(),
            page_codes=(),
            cell_codes=(),
            cell_numbers=(),
            run_line_slots=slots,
            marks=tuple(marks.marks),
            labels=tuple(marks.labels),
            checkerboard=checker,
            margin_text=margin_text,
            pass_no=pass_no,
            run_line=slots[pass_no - 2] if len(slots) >= pass_no - 1 else None,
        )
    # A cross's K label is ink the cell content keeps clear of.
    cell_keep_out = list(keep_out)
    if crossings is not None:
        cell_keep_out += [text_box_mm(t.x, t.y, t.text, t.size_pt, t.anchor) for t in marks.labels]

    codes: dict[int, CellCode] = {}
    numbers: dict[int, CellNumber] = {}
    for r in range(g.rows):
        for c in range(g.cols):
            code, num = _cell_content(g, c, r, allow_qr=allow_qr, area=a, keep_out=cell_keep_out)
            cell = r * g.cols + c + 1
            if code is not None:
                codes[cell] = code
            if num is not None:
                numbers[cell] = num

    ink: list[_Rect] = [_seg_rect(s) for mk in marks.marks for s in mk.segs]
    ink += [text_box_mm(t.x, t.y, t.text, t.size_pt, t.anchor) for t in marks.labels]
    ink += keep_out
    quiet = PAGE_QUIET_HMM / 100
    inset = CELL_INSET_HMM / 100
    spots: list[CodeSpot] = []
    for corner in CORNERS:
        c, r = _corner_cell(g, corner)
        cell = r * g.cols + c + 1
        cells_ink = [v.box.rect_mm() for k, v in codes.items()] + [
            v.box.rect_mm() for v in numbers.values()
        ]
        # The codes placed at earlier corners are ink, and their quiet zones stay blank.
        placed = [_grow(_r_mm(p.box), quiet) for p in spots]
        # 1. The printable corner of the page.
        ax = a[0] if corner in ("top_left", "bottom_left") else a[2]
        ay = a[1] if corner in ("top_left", "top_right") else a[3]
        sym = _spot_at(ax, ay, corner)
        if _inside(sym, a) and _clear_of(_grow(sym, quiet), [*ink, *cells_ink, *placed]):
            spots.append(CodeSpot(corner, "page_corner", None, Box.of_mm(sym)))
            continue
        # 2. The outer corner of the corner cell, inset so the corner marks stay.
        lx0, ly0, lx1, ly1 = _label_rect(g, c, r)
        sx = lx0 + inset if corner in ("top_left", "bottom_left") else lx1 - inset
        sy = ly0 + inset if corner in ("top_left", "top_right") else ly1 - inset
        sym = _spot_at(sx, sy, corner)
        zone = _grow(sym, quiet)
        others = [v.box.rect_mm() for k, v in codes.items() if k != cell] + [
            v.box.rect_mm() for k, v in numbers.items() if k != cell
        ]
        moved = _inner_corner_number(g, c, r, corner, a)
        if (
            moved is None
            or not _inside(sym, a)
            or not _clear_of(zone, [*ink, *others, *placed])
            or _overlap(moved.box.rect_mm(), zone)
            or not _clear_of(moved.box.rect_mm(), [*ink, *others])
        ):
            continue
        spots.append(CodeSpot(corner, "corner_cell", cell, Box.of_mm(sym)))
        numbers[cell] = moved
        own = codes.get(cell)
        if own is not None:
            # Dropped only when ink meets the other code's quiet zone: the cell symbol in the page
            # code's zone, or the page symbol in the cell code's zone. Two quiet zones touching
            # is blank on blank.
            box = own.box.rect_mm()
            cell_symbol = _grow(box, -CELL_QR_HMM / 100 * QUIET_MODULES / (21 + 2 * QUIET_MODULES))
            if (
                _overlap(cell_symbol, zone)
                or _overlap(sym, box)
                or _overlap(box, moved.box.rect_mm())
            ):
                del codes[cell]

    return PageLayout(
        area=area,
        margin_blocks=blocks,
        page_codes=tuple(spots),
        cell_codes=tuple(codes[k] for k in sorted(codes)),
        cell_numbers=tuple(numbers[k] for k in sorted(numbers)),
        run_line_slots=slots,
        marks=tuple(marks.marks),
        labels=tuple(marks.labels),
        checkerboard=checker,
        margin_text=margin_text,
        pass_no=pass_no,
    )


def _r_mm(box: Box) -> _Rect:
    return box.rect_mm()
