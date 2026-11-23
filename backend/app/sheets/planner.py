# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pure placement planner: which items go into which cells (spec 8, 10.1, 10.2).

Decides ``first_free`` and explicit placements, skipped cells, overflow onto further sheets,
the ID cell and the margin ID, including the band of the sheet margin (top, bottom, right or
left) it is drawn in. It reads ``SheetView`` values and writes nothing; reserving the
cells atomically is the job of the reservation service.

All lengths are hundredths of a millimetre (hmm).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal
from uuid import UUID

from app.page_render.pdf import text_width_mm
from app.page_render.transform import PT_PER_MM, Deviation
from app.printer_backends.ipp.attributes import Margins
from app.sheets.cell_render import qr_edge_px, qr_version
from app.sheets.errors import (
    CellOutOfRangeError,
    CellsTakenError,
    InsufficientCellsError,
    InvalidPlacementsError,
    SheetDiscardedError,
)
from app.sheets.free_rects import Rect, free_rects
from app.sheets.geometry import Geometry, cell_count, cell_origin, remaining_margins

__all__ = [
    "BAND_CORNER",
    "BAND_ORDER",
    "FEED_MARK_MIN_PT",
    "FEED_MARK_PT",
    "FEED_MARK_TEXTS",
    "FeedMarkPlacement",
    "FeedMarkReason",
    "IdPlacementReason",
    "MarginBand",
    "MarginBandRule",
    "MarginContent",
    "MarginContext",
    "MarginCorner",
    "MarginDecision",
    "MarginPlacement",
    "MarginRule",
    "PagePlan",
    "PlacementIn",
    "Plan",
    "SheetView",
    "band_length_hmm",
    "band_rect",
    "choose_feed_mark",
    "choose_margin_placement",
    "decide_margin_and_mark",
    "feed_mark_text",
    "grown_zones",
    "margin_bands_hmm",
    "margin_id_text",
    "margin_placement_with_reason",
    "margin_qr_allowed",
    "margin_qr_fits",
    "margin_text_start_pt",
    "plan",
]

MarginBand = Literal["top", "bottom", "right", "left"]
MarginCorner = Literal["top_left", "top_right", "bottom_left"]
MarginBandRule = Literal["auto", "top", "bottom", "left", "right"]
MarginContent = Literal["qr_code_type", "qr_code", "code"]
FeedMarkEnd = Literal["top_right", "top_left"]
FeedMarkReason = Literal["no_free_place"]
IdPlacementReason = Literal["outside_printable", "overlaps_preprint", "too_small", "no_margin_band"]

BAND_ORDER: Final[tuple[MarginBand, ...]] = ("top", "bottom", "right", "left")  # OC-46
BAND_CORNER: Final[dict[MarginBand, MarginCorner]] = {  # OC-45
    "top": "top_left",
    "bottom": "bottom_left",
    "right": "top_right",
    "left": "top_left",
}

DEFAULT_HW_HMM: Final = 500  # 5.0 mm per side assumed when the printer does not report it (OC-16)
SAFETY_HMM: Final = 100  # 1.0 mm
MIN_MARGIN_TEXT_HMM: Final = 400  # below: margin ID impossible
MIN_MARGIN_QR_HMM: Final = 900  # from here a QR may sit next to the text
MIN_QR_MODULE_HMM: Final = 25  # 0.25 mm
MARGIN_TEXT_GAP_HMM: Final = 200  # 2 mm between the margin QR and the text
MIN_MARGIN_TEXT_RUN_HMM: Final = 1000  # 10 mm of text beside a QR (OC-48)
MAX_MARGIN_ID_HMM: Final = 2000  # usable size cap, 20 mm (OC-52)
VERTICAL_BAND_LEAD_HMM: Final = 200  # a side band must be 2 mm deeper to beat top or bottom
_QUIET_MODULES: Final = 4
_CAP_EM: Final = 0.72  # cap height as a fraction of the font size

FEED_MARK_TEXTS: Final[dict[str, str]] = {  # OC-55, ruling A-5; \u2013 is the en dash (RUF001)
    "en": "▲ Top \u2013 this edge first",
    "de": "▲ Oben \u2013 diese Kante zuerst",
}
FEED_MARK_PT: Final = 7.0  # OC-54
FEED_MARK_MIN_PT: Final = 6.0
MARGIN_TEXT_MAX_PT: Final = 11.0
MARGIN_TEXT_MIN_PT: Final = 6.0


def margin_text_start_pt(depth_hmm: int) -> float:
    """The size the margin text starts at in a band of this depth (the renderer shrinks from it)."""
    return min(MARGIN_TEXT_MAX_PT, max(7.0, depth_hmm / 100 * 1.4))


def margin_id_text(code: str, type_label: str) -> str:
    """The text of the margin ID: the code, then the type (spec 10.2)."""
    return f"{code} · {type_label}"


@dataclass(frozen=True)
class MarginRule:
    """How a sheet type places its margin ID (#246, D33).

    ``zones`` are the effective keep-out zones (the preset's and the type's own, a union), in page
    coordinates (hmm). ``band`` ``auto`` keeps the choice of 1.7.x; a named band is a rule: without
    room there the page carries no margin ID. ``align`` ``start`` measures ``offset_hmm`` from the
    grid's start along the band (left, or top for a side band), ``end`` from its end.
    """

    zones: tuple[Rect, ...] = ()
    band: MarginBandRule = "auto"
    align: Literal["start", "end"] = "start"
    offset_hmm: int = 0


@dataclass
class SheetView:
    """What the planner needs to know about one sheet (an existing one or a new one).

    ``non_free`` maps every cell that has a ``sheet_cells`` row to its state. A new sheet has
    ``code=None`` and no non-free cells. ``margin_id_in_flight`` is computed by the caller
    inside the write lock: another job already carries the margin ID of this sheet.
    """

    code: str | None
    type_key: str
    type_version_id: UUID
    geometry: Geometry
    allow_qr: bool
    non_free: dict[int, str]
    id_placement: Literal["cell", "margin", "manual", "back_side"]
    id_printed: bool
    has_hub_cell: bool
    margin_id_in_flight: bool
    state: Literal["active", "discarded"]
    # The database id of an existing or just created sheet; the planner ignores it.
    sheet_id: UUID | None = None
    # The probable code of a sheet that does not exist yet (a preview); used for the QR payload.
    code_hint: str | None = None
    # The feed-direction mark (spec 10.2a): its own flag, and whether another job carries it.
    feed_mark_printed: bool = False
    feed_mark_in_flight: bool = False
    # The preferred ID cell of a ``cell`` sheet (#350, R-350-7), taken while it is free.
    id_cell_no: int | None = None
    # The type's placement rule and zones (#246) and its label, which the margin text carries.
    margin_rule: MarginRule = MarginRule()
    type_label: str | None = None


@dataclass
class PlacementIn:
    sheet: SheetView
    cells: Literal["first_free"] | list[int]
    skip: list[int] = field(default_factory=list)
    count: int | None = None


@dataclass
class PagePlan:
    index: int
    sheet: SheetView
    item_cells: list[int]
    id_cell: int | None
    margin_id: bool
    hw_margin_cells: list[int]
    margin_placement: MarginPlacement | None = None
    id_not_printed: bool = False
    feed_mark: FeedMarkPlacement | None = None
    feed_mark_reason: FeedMarkReason | None = None


@dataclass
class Plan:
    pages: list[PagePlan]
    # Per page: how many cells of its sheet are still free afterwards. Skipped cells stay free
    # (skip is never stored, OC-38).
    free_after: list[int]


@dataclass(frozen=True)
class MarginContext:
    """What the margin decisions need. ``language`` is required on purpose (no default): a
    caller that leaves it out is reported by ``mypy`` and raises ``TypeError``, so the mark
    never turns English by accident. ``None`` or an unknown value gives English."""

    deviation: Deviation
    scan_payload: Callable[[str], str]
    render_dpi: int
    language: str | None


@dataclass(frozen=True)
class FeedMarkPlacement:
    """The feed-direction mark: ``x_hmm`` is the left end of the text; ``end`` the end of the top
    band it took (S15: the stored payload carries only ``x_hmm``)."""

    text: str
    x_hmm: int
    baseline_y_hmm: int
    size_pt: float
    width_hmm: int
    end: FeedMarkEnd = "top_right"


@dataclass(frozen=True)
class MarginPlacement:
    """Where the margin ID goes. ``depth_hmm`` is the usable size that is drawn (capped).

    ``x_hmm``/``y_hmm`` are the top left corner of the ID's rectangle on the page, ``w_hmm`` its
    length along the band (the QR, the gap and the text) and ``depth_hmm`` its depth across it;
    ``content`` what is drawn: QR, code and type; QR and code; or the text alone (``code``: the
    code first, the type as far as it fits, as 1.7.x).
    """

    band: MarginBand
    corner: MarginCorner
    depth_hmm: int
    qr: bool
    x_hmm: int = 0
    y_hmm: int = 0
    w_hmm: int = 0
    content: MarginContent = "code"

    @property
    def rect(self) -> Rect:
        """The rectangle on the page (a side band's rectangle is ``depth_hmm`` wide)."""
        if self.band in ("top", "bottom"):
            return Rect(self.x_hmm, self.y_hmm, self.w_hmm, self.depth_hmm)
        return Rect(self.x_hmm, self.y_hmm, self.depth_hmm, self.w_hmm)


def margin_bands_hmm(g: Geometry, *, hw: Margins | None, dev: Deviation) -> dict[MarginBand, int]:
    """Room of each margin band after the printer's hardware margin of that side (5 mm if
    unknown), the calibration shift on that axis and 1 mm of safety.

    Every band of an axis loses the full ``|offset|`` of the axis, whatever its sign (ruling
    A-1). Values may be negative; scale and rotation are ignored (OC-51).
    """
    m = (
        hw
        if hw is not None
        else Margins(
            top=DEFAULT_HW_HMM, left=DEFAULT_HW_HMM, bottom=DEFAULT_HW_HMM, right=DEFAULT_HW_HMM
        )
    )
    right, bottom = remaining_margins(g)
    dx = abs(dev.offset_x_hmm)
    dy = abs(dev.offset_y_hmm)
    return {
        "top": g.margin_top - m.top - dy - SAFETY_HMM,
        "bottom": bottom - m.bottom - dy - SAFETY_HMM,
        "right": right - m.right - dx - SAFETY_HMM,
        "left": g.margin_left - m.left - dx - SAFETY_HMM,
    }


def band_length_hmm(g: Geometry, band: MarginBand) -> int:
    """Length of the grid along ``band``: the room a text beside the QR can run."""
    if band in ("top", "bottom"):
        return (g.cols - 1) * g.pitch_x + g.label_w
    return (g.rows - 1) * g.pitch_y + g.label_h


def margin_qr_allowed(usable_hmm: int, payload: str, dpi: int) -> bool:
    """Whether a QR of ``payload`` is drawn next to the margin ID (OC-16, ruling T4-1).

    The one decision the planner and the renderer both use. True only if the band is at least
    9 mm tall and the module ``build_qr`` really produces at ``dpi`` (the symbol is
    ``qr_edge_px`` square, modules a whole number of device pixels, four-module quiet zone
    inside) is at least 0.25 mm. Pixel flooring is part of the answer, not an afterthought.
    """
    if usable_hmm < MIN_MARGIN_QR_HMM or dpi <= 0:
        return False
    # Only the symbol's size matters, and it follows from the QR version alone (``best_fit``,
    # cached): a full build would search eight masks, about 15 ms a page, and the plan runs
    # under the global write lock (R10-2).
    version = qr_version(payload)
    if version is None:
        return False
    edge = qr_edge_px(usable_hmm / 100, usable_hmm / 100, dpi)
    modules = 17 + 4 * version  # the side of the symbol in modules, quiet zone excluded
    module_px = edge // (modules + 2 * _QUIET_MODULES)
    # module_px * 25.4 / dpi >= 0.25 mm, in integers: module_px * 2540 >= 25 * dpi
    return module_px >= 1 and module_px * 2540 >= MIN_QR_MODULE_HMM * dpi


margin_qr_fits = margin_qr_allowed
"""Name the plan uses; the same function."""


def feed_mark_text(language: str | None) -> str:
    """The mark's text for the hub language; any other value gives English (ruling A-5)."""
    return FEED_MARK_TEXTS.get(language or "", FEED_MARK_TEXTS["en"])


def _hw(hw: Margins | None) -> Margins:
    return (
        hw
        if hw is not None
        else Margins(
            top=DEFAULT_HW_HMM, left=DEFAULT_HW_HMM, bottom=DEFAULT_HW_HMM, right=DEFAULT_HW_HMM
        )
    )


def _width_hmm(text: str, size_pt: float) -> int:
    return math.ceil(text_width_mm(text, size_pt) * 100)


def grown_zones(zones: Sequence[Rect], dev: Deviation) -> list[Rect]:
    """The zones as the placement keeps them free: grown by ``SAFETY_HMM`` and the calibration's
    ``|offset|`` on each axis, like the bands (spec 5.2, 5.7)."""
    dx = SAFETY_HMM + abs(dev.offset_x_hmm)
    dy = SAFETY_HMM + abs(dev.offset_y_hmm)
    return [z.grown(dx, dy) for z in zones]


def band_rect(g: Geometry, band: MarginBand, *, hw: Margins | None, dev: Deviation) -> Rect:
    """The room of a margin band on the page, uncapped (it can be empty).

    Across the band it is ``margin_bands_hmm``: from the hardware margin and the calibration's
    ``|offset|`` to ``SAFETY_HMM`` before the grid. Along the band it is the grid's length, cut at
    the printable area (the hardware margin, ``|offset|`` and ``SAFETY_HMM``).
    """
    m = _hw(hw)
    dx, dy = abs(dev.offset_x_hmm), abs(dev.offset_y_hmm)
    right, bottom = remaining_margins(g)
    grid_right, grid_bottom = g.page_w - right, g.page_h - bottom
    x1 = max(g.margin_left, m.left + dx + SAFETY_HMM)
    x2 = min(grid_right, g.page_w - m.right - dx - SAFETY_HMM)
    y1 = max(g.margin_top, m.top + dy + SAFETY_HMM)
    y2 = min(grid_bottom, g.page_h - m.bottom - dy - SAFETY_HMM)
    depth = margin_bands_hmm(g, hw=hw, dev=dev)[band]
    if band == "top":
        return Rect(x1, g.margin_top - SAFETY_HMM - depth, x2 - x1, depth)
    if band == "bottom":
        return Rect(x1, grid_bottom + SAFETY_HMM, x2 - x1, depth)
    if band == "right":
        return Rect(grid_right + SAFETY_HMM, y1, depth, y2 - y1)
    return Rect(g.margin_left - SAFETY_HMM - depth, y1, depth, y2 - y1)


@dataclass(frozen=True)
class _Candidate:
    placement: MarginPlacement
    text_hmm: int  # the length the text gets, for the area order


def _content(
    usable: int,
    room: int,
    *,
    payload: str | None,
    dpi: int,
    text: str | None,
    code_text: str | None,
) -> tuple[MarginContent, int, int]:
    """What the ID draws in a piece ``usable`` deep and ``room`` long: content, length, and the
    text length the order compares.

    The degradation of #246: QR, code and type; QR and code; the text alone. The QR needs 9 mm of
    depth, a module the printer draws and 10 mm of text beside it (OC-48), as in 1.7.x. Without
    ``text`` (no type label known) the text is taken as the 10 mm run. The compared text length
    is measured at the smallest size, so it does not grow with the depth: on full bands the order
    is the depth rule of 1.7.x (ruling A-2), the length only tells a short piece from a long one.
    """
    start = margin_text_start_pt(usable)
    lead = usable + MARGIN_TEXT_GAP_HMM
    qr = (
        payload is not None
        and margin_qr_allowed(usable, payload, dpi)
        and room >= lead + MIN_MARGIN_TEXT_RUN_HMM
    )
    if qr:
        run = room - lead
        if text is None:
            return "qr_code_type", lead + min(run, MIN_MARGIN_TEXT_RUN_HMM), MIN_MARGIN_TEXT_RUN_HMM
        smallest = _width_hmm(text, MARGIN_TEXT_MIN_PT)
        if smallest <= run:
            return "qr_code_type", lead + min(run, _width_hmm(text, start)), smallest
        if code_text is not None and (code := _width_hmm(code_text, MARGIN_TEXT_MIN_PT)) <= run:
            return "qr_code", lead + min(run, _width_hmm(code_text, start)), code
        return "qr_code_type", room, run  # shortened with an ellipsis, as 1.7.x
    if text is None:
        length = min(room, MIN_MARGIN_TEXT_RUN_HMM)
        return "code", length, length
    return (
        "code",
        min(room, _width_hmm(text, start)),
        min(room, _width_hmm(text, MARGIN_TEXT_MIN_PT)),
    )


def _pieces(
    band: MarginBand,
    free: Sequence[Rect],
    *,
    g: Geometry,
    align: Literal["start", "end"],
    offset_hmm: int,
) -> list[tuple[int, int, int, int]]:
    """The free rectangles as ``(along_lo, along_hi, across_lo, across_hi)``, cut at the requested
    start point when an offset or ``align = end`` asks for one (spec 5.2 step 3)."""
    horizontal = band in ("top", "bottom")
    out: list[tuple[int, int, int, int]] = []
    right, bottom = remaining_margins(g)
    grid_lo = g.margin_left if horizontal else g.margin_top
    grid_hi = (g.page_w - right) if horizontal else (g.page_h - bottom)
    point = grid_lo + offset_hmm if align == "start" else grid_hi - offset_hmm
    anchored = align == "end" or offset_hmm != 0
    for r in free:
        lo, hi = (r.x, r.x2) if horizontal else (r.y, r.y2)
        across = (r.y, r.y2) if horizontal else (r.x, r.x2)
        if anchored:
            if align == "start":
                if not lo <= point < hi:
                    continue
                lo = point
            else:
                if not lo < point <= hi:
                    continue
                hi = point
        out.append((lo, hi, *across))
    return out


def margin_placement_with_reason(
    g: Geometry,
    *,
    hw: Margins | None,
    dev: Deviation,
    payload: str | None,
    dpi: int,
    zones: Sequence[Rect] = (),
    band_rule: MarginBandRule = "auto",
    align: Literal["start", "end"] = "start",
    offset_hmm: int = 0,
    taken: Sequence[Rect] = (),
    text: str | None = None,
    code_text: str | None = None,
) -> tuple[MarginPlacement | None, IdPlacementReason | None]:
    """The rectangle the margin ID goes into, or ``None`` with the reason (spec 5.2, D33).

    Each band (``band_rect``) is cut into free rectangles around the zones and ``taken`` (grown
    by ``SAFETY_HMM`` and ``|offset|``); a piece at least 4 mm deep and 10 mm long is a
    candidate. ``band_rule`` other than ``auto`` keeps only that band. Candidates are ordered: one
    with a QR first; then the area the ID uses (its capped depth times the length of its text, a
    side band's depth counted ``VERTICAL_BAND_LEAD_HMM`` less, ruling A-2); then the band order
    of 1.7.x. The ID sits on the grid side of its piece, at the piece's start (``align`` ``end``:
    ending at the requested point).
    """
    blocked = grown_zones(zones, dev) + [
        t.grown(MARGIN_TEXT_GAP_HMM, MARGIN_TEXT_GAP_HMM) for t in taken
    ]
    depths = margin_bands_hmm(g, hw=hw, dev=dev)
    bands = BAND_ORDER if band_rule == "auto" else (band_rule,)
    best: tuple[tuple[int, int, int], _Candidate] | None = None
    seen_deep = seen_shallow = seen_point = room_without_zones = False
    for order, band in enumerate(BAND_ORDER):
        if band not in bands:
            continue
        depth = depths[band]
        if depth <= 0:
            continue
        if depth < MIN_MARGIN_TEXT_HMM:
            seen_shallow = True
            continue
        seen_deep = True
        rect = band_rect(g, band, hw=hw, dev=dev)
        horizontal = band in ("top", "bottom")
        clear = free_rects(rect, [], min_depth=MIN_MARGIN_TEXT_HMM, horizontal=horizontal)
        free = free_rects(rect, blocked, min_depth=MIN_MARGIN_TEXT_HMM, horizontal=horizontal)
        anchored_clear = _pieces(band, clear, g=g, align=align, offset_hmm=offset_hmm)
        if anchored_clear:
            seen_point = True
        if any(hi - lo >= MIN_MARGIN_TEXT_RUN_HMM for lo, hi, _, _ in anchored_clear):
            room_without_zones = True  # without zones this band would have held the ID
        for lo, hi, a_lo, a_hi in _pieces(band, free, g=g, align=align, offset_hmm=offset_hmm):
            room = hi - lo
            if room < MIN_MARGIN_TEXT_RUN_HMM:
                continue
            usable = min(a_hi - a_lo, MAX_MARGIN_ID_HMM)
            content, length, text_len = _content(
                usable, room, payload=payload, dpi=dpi, text=text, code_text=code_text
            )
            start = lo if align == "start" else hi - length
            if band == "top":
                x, y = start, a_hi - usable
            elif band == "bottom":
                x, y = start, a_lo
            elif band == "right":
                x, y = a_lo, start
            else:
                x, y = a_hi - usable, start
            placement = MarginPlacement(
                band, BAND_CORNER[band], usable, content != "code", x, y, length, content
            )
            lead = 0 if horizontal else VERTICAL_BAND_LEAD_HMM - 1
            key = (int(content != "code"), (usable - lead) * text_len, -order)
            if best is None or key > best[0]:
                best = (key, _Candidate(placement, text_len))
    if best is not None:
        return best[1].placement, None
    if not seen_deep:
        return None, "too_small" if seen_shallow else "no_margin_band"
    if not seen_point:
        return None, "outside_printable"
    # ``overlaps_preprint`` only when the zones (or ``taken``) are what blocks: some band would
    # have held the ID without them. Otherwise the pieces are too short anyway (review M-5).
    return None, "overlaps_preprint" if room_without_zones else "too_small"


def choose_margin_placement(
    g: Geometry,
    *,
    hw: Margins | None,
    dev: Deviation,
    payload: str | None,
    dpi: int,
    zones: Sequence[Rect] = (),
    band_rule: MarginBandRule = "auto",
    align: Literal["start", "end"] = "start",
    offset_hmm: int = 0,
    taken: Sequence[Rect] = (),
    text: str | None = None,
    code_text: str | None = None,
) -> MarginPlacement | None:
    """The rectangle the margin ID goes into, or ``None`` (``margin_placement_with_reason``).

    One function for the plan, the preview, the margin check, the per-printer view and the
    probe print (spec 5.2). Decided once, at render dpi.
    """
    return margin_placement_with_reason(
        g,
        hw=hw,
        dev=dev,
        payload=payload,
        dpi=dpi,
        zones=zones,
        band_rule=band_rule,
        align=align,
        offset_hmm=offset_hmm,
        taken=taken,
        text=text,
        code_text=code_text,
    )[0]


def choose_feed_mark(
    g: Geometry,
    *,
    hw: Margins | None,
    dev: Deviation,
    text: str,
    zones: Sequence[Rect] = (),
    taken: Sequence[Rect] = (),
) -> tuple[FeedMarkPlacement | None, FeedMarkReason | None]:
    """Where the feed-direction mark goes: the top band's right end, else its left end (#246).

    Not printed when the top band is below 4 mm; never in another band. 7 pt, shrinking in
    0.5 pt steps to 6 pt; never shortened, so it is dropped when 6 pt does not fit (OC-53/54);
    the reason is then ``None``. An end is free when the mark's box overlaps no zone (grown like
    the ID's) and no rectangle of ``taken`` (the margin ID's, grown by the text gap); with
    neither end free the mark is not printed and the reason is ``no_free_place`` (spec 5.7).
    """
    depth = margin_bands_hmm(g, hw=hw, dev=dev)["top"]
    if depth < MIN_MARGIN_TEXT_HMM:
        return None, None
    usable = min(depth, MAX_MARGIN_ID_HMM)
    m = _hw(hw)
    grid_right = g.page_w - remaining_margins(g)[0]
    right_end = min(grid_right, g.page_w - m.right - abs(dev.offset_x_hmm) - SAFETY_HMM)
    left_end = max(g.margin_left, m.left + abs(dev.offset_x_hmm) + SAFETY_HMM)
    room = right_end - g.margin_left
    size = FEED_MARK_PT
    while round(text_width_mm(text, size) * 100) > room and size > FEED_MARK_MIN_PT:
        size -= 0.5
    width = round(text_width_mm(text, size) * 100)
    if width > room:
        return None, None
    centre = g.margin_top - SAFETY_HMM - usable / 2
    baseline = round(centre + _CAP_EM * size / PT_PER_MM * 100 / 2)
    top = g.margin_top - SAFETY_HMM - usable
    blocked = grown_zones(zones, dev) + [
        t.grown(MARGIN_TEXT_GAP_HMM, MARGIN_TEXT_GAP_HMM) for t in taken
    ]
    ends: tuple[tuple[FeedMarkEnd, int], ...] = (
        ("top_right", right_end - width),
        ("top_left", left_end),
    )
    for end, x in ends:
        if x + width > right_end or x < left_end:
            continue
        box = Rect(x, top, width, usable)
        if not any(box.overlaps(b) for b in blocked):
            return FeedMarkPlacement(text, x, baseline, size, width, end), None
    return None, "no_free_place"


@dataclass(frozen=True)
class MarginDecision:
    """Whether the sheet's margin ID and feed mark are due on a page, and where they go."""

    id_was_due: bool
    margin_id: bool
    placement: MarginPlacement | None
    id_not_printed: bool
    mark_was_due: bool
    feed_mark: FeedMarkPlacement | None
    feed_mark_reason: FeedMarkReason | None = None


FeedMarkMemo = dict[
    tuple[Geometry, tuple[Rect, ...], tuple[Rect, ...]],
    tuple[FeedMarkPlacement | None, FeedMarkReason | None],
]


def decide_margin_and_mark(
    view: SheetView,
    margin: MarginContext | None,
    hw_margins_hmm: Margins | None,
    *,
    id_done: bool = False,
    mark_done: bool = False,
    feed_marks: FeedMarkMemo | None = None,
) -> MarginDecision:
    """The one rule for the margin ID and the feed mark of a page, shared by ``plan`` and by the
    retry of a page job (which re-decides both for the printer it is sent to and stores its own
    decision, spec S2).

    The mark has its own flag and is due for every ID placement (OC-56, OC-58). The **ID is
    placed first** and the mark in what is left (#246, D33; reverses OC-57's "mark first"): the
    ID identifies the sheet, the mark is a convenience. ``margin=None`` means there is no printer
    context: the ID is still due, but no band is chosen.

    ``feed_marks`` memoises the mark for one planning pass by geometry, effective zones and the
    ID's rectangle: the hardware margins, the calibration and the language are the same for all
    of its pages, so fifty new sheets of one type measure the mark's text once (the plan runs
    under the write lock, R10-2), and two types with equal geometry but other zones, or a sheet
    whose ID is not due, never share a mark.
    """
    g = view.geometry
    rule = view.margin_rule
    due = (
        view.id_placement == "margin"
        and not view.id_printed
        and not view.margin_id_in_flight
        and not id_done
    )
    margin_id = due
    placement: MarginPlacement | None = None
    id_not_printed = False
    mark_due = not view.feed_mark_printed and not view.feed_mark_in_flight and not mark_done
    feed_mark: FeedMarkPlacement | None = None
    reason: FeedMarkReason | None = None
    if due and margin is not None:
        code = view.code or view.code_hint
        placement = choose_margin_placement(
            g,
            hw=hw_margins_hmm,
            dev=margin.deviation,
            payload=margin.scan_payload(code) if code else None,
            dpi=margin.render_dpi,
            zones=rule.zones,
            band_rule=rule.band,
            align=rule.align,
            offset_hmm=rule.offset_hmm,
            text=margin_id_text(code, view.type_label) if code and view.type_label else None,
            code_text=code,
        )
        margin_id = placement is not None
        id_not_printed = placement is None
    if mark_due and margin is not None:
        taken = (placement.rect,) if placement is not None else ()
        key = (g, rule.zones, taken)
        if feed_marks is not None and key in feed_marks:
            feed_mark, reason = feed_marks[key]
        else:
            feed_mark, reason = choose_feed_mark(
                g,
                hw=hw_margins_hmm,
                dev=margin.deviation,
                text=feed_mark_text(margin.language),
                zones=rule.zones,
                taken=taken,
            )
            if feed_marks is not None:
                feed_marks[key] = (feed_mark, reason)
    return MarginDecision(due, margin_id, placement, id_not_printed, mark_due, feed_mark, reason)


def _hw_margin_cells(g: Geometry, cells: Sequence[int], hw: Margins | None) -> list[int]:
    """Cells whose rectangle reaches into a hardware-margin band (OC-44: flagged, not refused)."""
    if hw is None:
        return []
    flagged: list[int] = []
    for n in sorted(cells):
        x, y = cell_origin(g, n)
        if (
            y < hw.top
            or x < hw.left
            or y + g.label_h > g.page_h - hw.bottom
            or x + g.label_w > g.page_w - hw.right
        ):
            flagged.append(n)
    return flagged


def _resolve_counts(placements: Sequence[PlacementIn], n_items: int) -> list[int]:
    counts: list[int] = []
    last = len(placements) - 1
    for i, p in enumerate(placements):
        if isinstance(p.cells, list):
            if p.count is not None and p.count != len(p.cells):
                raise InvalidPlacementsError("explicit_count_mismatch")
            counts.append(len(p.cells))
        elif p.count is None:
            if i != last:
                raise InvalidPlacementsError("count_missing_not_last")
            counts.append(-1)  # takes the rest, resolved below
        else:
            if p.count < 0:
                raise InvalidPlacementsError("count_mismatch")
            counts.append(p.count)
    if counts and counts[-1] == -1:
        rest = n_items - sum(counts[:-1])
        if rest < 0:
            raise InvalidPlacementsError("count_mismatch")
        counts[-1] = rest
    if sum(counts) != n_items:
        raise InvalidPlacementsError(
            "explicit_count_mismatch"
            if any(isinstance(p.cells, list) for p in placements)
            else "count_mismatch"
        )
    return counts


@dataclass
class _Draft:
    placement: PlacementIn
    count: int
    id_cell: int | None
    id_missing: bool
    item_cells: list[int]
    margin_id: bool
    available: int
    free_after: int
    margin_placement: MarginPlacement | None = None
    id_not_printed: bool = False
    id_was_due: bool = False
    feed_mark: FeedMarkPlacement | None = None
    mark_was_due: bool = False
    feed_mark_reason: FeedMarkReason | None = None


def _draft(
    p: PlacementIn,
    count: int,
    taken: set[int],
    margin: MarginContext | None,
    id_done: bool,
    mark_done: bool,
    hw_margins_hmm: Margins | None,
    feed_marks: FeedMarkMemo,
) -> _Draft:
    """Plan one placement. ``id_done``: this sheet's ID label already went to an earlier page;
    ``mark_done``: so did its feed-direction mark."""
    view = p.sheet
    if view.state == "discarded":
        raise SheetDiscardedError()
    g = view.geometry
    total = cell_count(g)
    busy = set(view.non_free) | taken
    free = [n for n in range(1, total + 1) if n not in busy]
    skip = set(p.skip)
    needs_id_cell = (
        view.id_placement == "cell"
        and not view.id_printed
        and not view.has_hub_cell
        and not id_done
    )
    explicit = set(p.cells) if isinstance(p.cells, list) else set()

    if isinstance(p.cells, list):
        out = sorted(n for n in p.cells if n < 1 or n > total)
        if out:
            raise CellOutOfRangeError(out, total)
        if len(explicit) != len(p.cells):
            raise InvalidPlacementsError("duplicate_cells")
        not_free = sorted(n for n in p.cells if n in busy)
        if not_free:
            raise CellsTakenError(not_free)

    id_cell: int | None = None
    if needs_id_cell:
        preferred = view.id_cell_no
        if (
            preferred is not None
            and 1 <= preferred <= total
            and preferred not in busy
            and preferred not in skip
            and preferred not in explicit
        ):
            id_cell = preferred  # R-350-7: the type's reserved cell while it is free
        else:
            id_cell = next((n for n in free if n not in skip and n not in explicit), None)
    id_missing = needs_id_cell and id_cell is None

    decision = decide_margin_and_mark(
        view, margin, hw_margins_hmm, id_done=id_done, mark_done=mark_done, feed_marks=feed_marks
    )
    due = decision.id_was_due
    margin_id = decision.margin_id
    placement = decision.placement
    id_not_printed = decision.id_not_printed
    mark_due = decision.mark_was_due
    feed_mark = decision.feed_mark

    if isinstance(p.cells, list):
        # Item i goes to cells[i]: the caller's order is kept.
        items = list(p.cells)
        # 266-01: with no cell left for the ID, the capacity is what remains for items once the
        # ID takes one of the listed cells, as a ``first_free`` placement reports it; that cell
        # is counted here, so ``plan`` adds no second one to the shortfall.
        available = len(items) - 1 if id_missing and items else len(items)
    else:
        pool = [n for n in free if n not in skip and n != id_cell]
        available = len(pool)
        items = pool[:count]
    enough = not id_missing and len(items) == count
    if not enough:
        items = []
    used = set(items) | ({id_cell} if id_cell is not None else set())
    return _Draft(
        placement=p,
        count=count,
        id_cell=id_cell,
        id_missing=id_missing,
        item_cells=items,
        margin_id=margin_id,
        available=available,
        free_after=len(free) - len(used),
        margin_placement=placement,
        id_not_printed=id_not_printed,
        id_was_due=due,
        feed_mark=feed_mark,
        mark_was_due=mark_due,
        feed_mark_reason=decision.feed_mark_reason,
    )


def plan(
    placements: Sequence[PlacementIn],
    n_items: int,
    *,
    hw_margins_hmm: Margins | None,
    margin: MarginContext | None,
) -> Plan:
    """Fill ``placements`` in order with ``n_items`` items.

    ``n_items == 0`` is valid when an ID cell or a margin ID is due (an ID-only job); with
    nothing to print at all the request is ``empty``. ``margin=None`` means there is no printer
    context, so the band is not chosen (the ID is still due). A placement that would print
    nothing is ``empty_placement``.

    Caller contract: one ``SheetView`` object stands for one sheet. Passing the same object in
    several placements shares its cells and its ID label (the ID goes out once, on the sheet's
    first page); two distinct objects are two sheets, even if they carry the same code, and a
    new sheet must therefore get its own object. Explicit cell lists keep the caller's order.
    ``InsufficientCellsError.free_cells`` is the capacity left for items per placement, after
    skip and the ID cell; a missing ID cell adds one to ``shortfall``.
    """
    if not placements:
        raise InvalidPlacementsError("empty")
    counts = _resolve_counts(placements, n_items)

    taken_by_sheet: dict[int, set[int]] = {}
    id_done: set[int] = set()
    mark_done: set[int] = set()
    drafts: list[_Draft] = []
    feed_marks: FeedMarkMemo = {}
    for p, count in zip(placements, counts, strict=True):
        key = id(p.sheet)
        taken = taken_by_sheet.setdefault(key, set())
        d = _draft(
            p, count, taken, margin, key in id_done, key in mark_done, hw_margins_hmm, feed_marks
        )
        drafts.append(d)
        if d.id_missing or len(d.item_cells) != count:
            continue
        taken.update(d.item_cells)
        if d.id_cell is not None:
            taken.add(d.id_cell)
        if d.id_cell is not None or d.id_was_due:
            id_done.add(key)
        if d.mark_was_due:
            mark_done.add(key)

    short = [d for d in drafts if d.id_missing or len(d.item_cells) != d.count]
    if short:
        shortfall = sum(
            max(0, d.count - d.available)
            + int(d.id_missing and not (isinstance(d.placement.cells, list) and d.placement.cells))
            for d in short
        )
        raise InsufficientCellsError(
            [d.available for d in drafts], needed=n_items, shortfall=shortfall
        )

    for d in drafts:
        # With items to print, a page needs a cell: the margin ID and the feed mark are recorded
        # when a cell of the page turns used, so a page of them alone would print both
        # unrecorded and the sheet's next print would draw them again (R10-3). Only the
        # ID-only job (``n_items == 0``) is a page without item cells.
        carries_nothing = not d.item_cells and d.id_cell is None
        if carries_nothing and (n_items > 0 or not d.margin_id):
            # n_items == 0 with nothing due is "empty"; otherwise a zero-count placement.
            raise InvalidPlacementsError("empty" if n_items == 0 else "empty_placement")
    if n_items == 0 and not any(d.id_cell is not None or d.margin_id for d in drafts):
        raise InvalidPlacementsError("empty")

    pages = [
        PagePlan(
            index=i,
            sheet=d.placement.sheet,
            item_cells=d.item_cells,
            id_cell=d.id_cell,
            margin_id=d.margin_id,
            hw_margin_cells=_hw_margin_cells(
                d.placement.sheet.geometry,
                d.item_cells + ([d.id_cell] if d.id_cell is not None else []),
                hw_margins_hmm,
            ),
            margin_placement=d.margin_placement,
            id_not_printed=d.id_not_printed,
            feed_mark=d.feed_mark,
            feed_mark_reason=d.feed_mark_reason,
        )
        for i, d in enumerate(drafts)
    ]
    return Plan(pages=pages, free_after=[d.free_after for d in drafts])
