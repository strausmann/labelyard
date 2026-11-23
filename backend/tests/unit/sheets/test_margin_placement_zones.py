# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The margin ID around preprint keep-out zones, and the feed mark after it (#246, spec 5.2, D33).

Geometry of the scenarios: an L4736 sheet (4 x 12 cells of 45.7 x 21.2 mm, top and bottom margin
21.3 mm, left 9.85 mm). With the assumed 5 mm hardware margin its top band is 15.3 mm deep and runs
from x = 9.85 mm to the grid's right end at 200.15 mm.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from functools import cache
from typing import Literal

from app.catalog import loader
from app.page_render.transform import Deviation
from app.printer_backends.ipp.attributes import Margins
from app.sheets.exchange import geometry_from_doc
from app.sheets.free_rects import Rect
from app.sheets.geometry import Geometry, cell_origin, remaining_margins
from app.sheets.planner import (
    SAFETY_HMM,
    MarginContext,
    MarginRule,
    PlacementIn,
    SheetView,
    choose_feed_mark,
    choose_margin_placement,
    decide_margin_and_mark,
    margin_placement_with_reason,
    plan,
)
from hypothesis import given, settings
from hypothesis import strategies as st

L4736 = Geometry("A4", 21000, 29700, 4570, 2120, 4, 12, 4820, 2120, 2130, 985, None, "rectangle")
PAYLOAD = "https://hub.example.invalid/sheet/L4736-0001"
EN_MARK = "▲ Top \u2013 this edge first"
LOGO = Rect(985, 600, 4000, 1000)  # 40 x 10 mm over the top band's left end


def _view(
    rule: MarginRule | None = None,
    *,
    geometry: Geometry = L4736,
    placement: Literal["cell", "margin", "manual"] = "margin",
    code: str = "L4736-0001",
    label: str | None = None,
) -> SheetView:
    return SheetView(
        code=code,
        type_key="acme",
        type_version_id=uuid.UUID(int=1),
        geometry=geometry,
        allow_qr=True,
        non_free={},
        id_placement=placement,
        id_printed=False,
        has_hub_cell=False,
        margin_id_in_flight=False,
        state="active",
        margin_rule=rule or MarginRule(),
        type_label=label,
    )


def _ctx(dev: Deviation | None = None) -> MarginContext:
    return MarginContext(
        deviation=dev or Deviation(),
        scan_payload=lambda c: f"https://hub.example.invalid/sheet/{c}",
        render_dpi=600,
        language="en",
    )


def _choose(rule: MarginRule, *, hw: Margins | None = None, dev: Deviation | None = None, **kw):  # type: ignore[no-untyped-def]
    return choose_margin_placement(
        L4736,
        hw=hw,
        dev=dev or Deviation(),
        payload=PAYLOAD,
        dpi=600,
        zones=rule.zones,
        band_rule=rule.band,
        align=rule.align,
        offset_hmm=rule.offset_hmm,
        **kw,
    )


# ---------------------------------------------------------------------------- property


@cache
def _catalog_geometries() -> tuple[Geometry, ...]:
    return tuple(
        geometry_from_doc(loader.load_entry(e.key).version.geometry) for e in loader.load_index()
    )


def _gap(a_lo: int, a_hi: int, b_lo: int, b_hi: int) -> int:
    """Distance of two intervals; negative when they overlap."""
    return max(b_lo - a_hi, a_lo - b_hi)


_zones = st.lists(
    st.builds(
        Rect,
        x=st.integers(0, 20000),
        y=st.integers(0, 29000),
        w=st.integers(100, 6000),
        h=st.integers(100, 3000),
    ),
    max_size=6,
)


@settings(max_examples=250, deadline=None)
@given(
    index=st.integers(0, 10_000),
    hw_mm=st.sampled_from([3, 5, 8]),
    dx=st.integers(-200, 200),
    dy=st.integers(-200, 200),
    zones=_zones,
    band=st.sampled_from(["auto", "top", "bottom", "left", "right"]),
    align=st.sampled_from(["start", "end"]),
    offset=st.integers(0, 3000),
)
def test_chosen_rectangle_keeps_its_clearance(
    index: int,
    hw_mm: int,
    dx: int,
    dy: int,
    zones: list[Rect],
    band: Literal["auto", "top", "bottom", "left", "right"],
    align: Literal["start", "end"],
    offset: int,
) -> None:
    """The clearance rule (ADR 0023, D33): from every zone 1 mm and ``|offset|`` on at least one
    axis; along the band 1 mm and ``|offset|`` from the printable edge; across the band only what
    1.7.x kept, ``|offset|`` from the hardware margin on the paper side and 1 mm before the grid,
    so placements without zones are unchanged. Never over a cell."""
    geometries = _catalog_geometries()
    g = geometries[index % len(geometries)]
    hw = Margins(top=hw_mm * 100, left=hw_mm * 100, bottom=hw_mm * 100, right=hw_mm * 100)
    dev = Deviation(offset_x_hmm=dx, offset_y_hmm=dy)
    got = choose_margin_placement(
        g,
        hw=hw,
        dev=dev,
        payload=PAYLOAD,
        dpi=600,
        zones=zones,
        band_rule=band,
        align=align,
        offset_hmm=offset,
        text="L4736-0001 · Avery Zweckform L4736REV",
        code_text="L4736-0001",
    )
    if got is None:
        return
    r = got.rect
    if band != "auto":
        assert got.band == band
    # every zone: the full clearance on at least one axis
    for z in zones:
        gap_x = _gap(r.x, r.x2, z.x, z.x2)
        gap_y = _gap(r.y, r.y2, z.y, z.y2)
        assert gap_x >= SAFETY_HMM + abs(dx) or gap_y >= SAFETY_HMM + abs(dy), (r, z)
    # the unprintable area: along the band 1 mm and |offset|; across it the hardware margin and
    # |offset| on the paper side, 1 mm before the grid on the other (the bands of 1.7.x)
    right, bottom = remaining_margins(g)
    if got.band in ("top", "bottom"):
        assert r.x >= hw.left + abs(dx) + SAFETY_HMM
        assert r.x2 <= g.page_w - hw.right - abs(dx) - SAFETY_HMM
        if got.band == "top":
            assert r.y >= hw.top + abs(dy) and r.y2 <= g.margin_top - SAFETY_HMM
        else:
            assert r.y >= g.page_h - bottom + SAFETY_HMM
            assert r.y2 <= g.page_h - hw.bottom - abs(dy)
    else:
        assert r.y >= hw.top + abs(dy) + SAFETY_HMM
        assert r.y2 <= g.page_h - hw.bottom - abs(dy) - SAFETY_HMM
        if got.band == "left":
            assert r.x >= hw.left + abs(dx) and r.x2 <= g.margin_left - SAFETY_HMM
        else:
            assert r.x >= g.page_w - right + SAFETY_HMM
            assert r.x2 <= g.page_w - hw.right - abs(dx)
    # never over a cell
    for n in range(1, g.cols * g.rows + 1):
        x, y = cell_origin(g, n)
        assert not r.overlaps(Rect(x, y, g.label_w, g.label_h)), (r, n)


# ---------------------------------------------------------------------------- the ID


def test_zone_over_the_logo_moves_the_id_to_the_free_corner() -> None:
    plain = _choose(MarginRule())
    assert plain is not None and (plain.band, plain.x_hmm) == ("top", 985)

    got = _choose(MarginRule(zones=(LOGO,)))

    assert got is not None and got.band == "top" and got.qr is True
    assert got.x_hmm == LOGO.x2 + SAFETY_HMM  # right of the logo, 1 mm clear
    assert got.y_hmm == 2130 - SAFETY_HMM - got.depth_hmm  # still on the grid side


def test_zone_over_the_whole_top_band_moves_the_id_to_another_band() -> None:
    top = Rect(0, 0, 21000, 2130)

    got = _choose(MarginRule(zones=(top,)))

    assert got is not None and got.band == "bottom" and got.qr is True


def test_all_bands_blocked_degrades_to_code_then_none() -> None:
    text = "L4736-0001 · Avery Zweckform L4736REV"
    others = (Rect(0, 27570, 21000, 2130), Rect(0, 0, 985, 29700), Rect(20015, 0, 985, 29700))

    def content(*zones: Rect) -> str | None:
        got = _choose(MarginRule(zones=(*others, *zones)), text=text, code_text="L4736-0001")
        return None if got is None else got.content

    assert content() == "qr_code_type"
    # a piece 1.73 + 2.5 cm long: the QR and the code fit, the type does not
    short = (Rect(985 + 1730 + 2500 + 100, 0, 21000, 2130),)
    assert content(*short) == "qr_code"
    # 5 mm deep along the grid: too shallow for a QR, the text alone
    shallow = (Rect(0, 0, 21000, 2030 - 500 - SAFETY_HMM),)
    assert content(*shallow) == "code"
    assert content(Rect(0, 0, 21000, 2130)) is None

    decision = decide_margin_and_mark(
        _view(MarginRule(zones=(*others, Rect(0, 0, 21000, 2130)))), _ctx(), None
    )
    assert decision.margin_id is False and decision.id_not_printed is True


def test_forced_band_without_room_gives_id_not_printed() -> None:
    rule = MarginRule(band="right")  # the right band of L4736 is 3.85 mm deep with 5 mm hw
    placement, reason = margin_placement_with_reason(
        L4736, hw=None, dev=Deviation(), payload=PAYLOAD, dpi=600, band_rule="right"
    )
    assert placement is None and reason == "too_small"

    decision = decide_margin_and_mark(_view(rule), _ctx(), None)

    assert decision.placement is None and decision.id_not_printed is True
    assert _choose(MarginRule()) is not None  # the top band would have fitted


def test_align_end_with_offset_starts_at_the_requested_point() -> None:
    grid_right = 21000 - remaining_margins(L4736)[0]

    got = _choose(MarginRule(band="top", align="end", offset_hmm=3000))

    assert got is not None and got.band == "top"
    assert got.x_hmm + got.w_hmm == grid_right - 3000
    start = _choose(MarginRule(band="top", offset_hmm=2000))
    assert start is not None and start.x_hmm == 985 + 2000
    left = _choose(
        MarginRule(band="left", offset_hmm=500),
        hw=Margins(top=100, left=100, bottom=100, right=100),
    )
    assert left is not None and left.band == "left" and left.y_hmm == 2130 + 500


def test_calibration_offset_grows_the_zones() -> None:
    still = _choose(MarginRule(zones=(LOGO,)))
    shifted = _choose(MarginRule(zones=(LOGO,)), dev=Deviation(offset_x_hmm=-150))

    assert still is not None and shifted is not None
    assert still.x_hmm == LOGO.x2 + SAFETY_HMM
    assert shifted.x_hmm == LOGO.x2 + SAFETY_HMM + 150


# ---------------------------------------------------------------------------- the mark after it


def test_id_first_then_mark_takes_the_free_end() -> None:
    rule = MarginRule(band="top", align="end")  # the ID takes the top band's right end

    decision = decide_margin_and_mark(_view(rule), _ctx(), None)

    assert decision.placement is not None and decision.placement.band == "top"
    grid_right = 21000 - remaining_margins(L4736)[0]
    assert decision.placement.x_hmm + decision.placement.w_hmm == grid_right
    mark = decision.feed_mark
    assert mark is not None and mark.end == "top_left" and mark.x_hmm == 985
    assert decision.feed_mark_reason is None
    alone, _ = choose_feed_mark(L4736, hw=None, dev=Deviation(), text=EN_MARK)
    assert alone is not None and alone.end == "top_right"


def test_mark_without_free_place_reports_no_free_place() -> None:
    ends = (Rect(985, 500, 3000, 1530), Rect(17000, 500, 3015, 1530))

    mark, reason = choose_feed_mark(L4736, hw=None, dev=Deviation(), text=EN_MARK, zones=ends)

    assert mark is None and reason == "no_free_place"
    decision = decide_margin_and_mark(
        _view(MarginRule(zones=ends), placement="manual"), _ctx(), None
    )
    assert decision.mark_was_due is True and decision.feed_mark is None
    assert decision.feed_mark_reason == "no_free_place"
    thin = Margins(top=1631, left=500, bottom=500, right=500)
    assert choose_feed_mark(L4736, hw=thin, dev=Deviation(), text=EN_MARK) == (None, None)


def _pages(*views: SheetView) -> list[object]:
    placements = [PlacementIn(sheet=v, cells="first_free", count=1) for v in views]
    return list(plan(placements, len(views), hw_margins_hmm=None, margin=_ctx()).pages)


def test_two_types_with_equal_geometry_and_different_zones_get_their_own_mark() -> None:
    right_end = Rect(15000, 500, 6000, 1530)
    a = _view(MarginRule(zones=(right_end,)), placement="manual")
    b = _view(MarginRule(), placement="manual", code="L4736-0002")

    first, second = _pages(a, b)

    assert first.feed_mark is not None and first.feed_mark.end == "top_left"  # type: ignore[attr-defined]
    assert second.feed_mark is not None and second.feed_mark.end == "top_right"  # type: ignore[attr-defined]


def test_mark_of_a_sheet_without_due_id_ignores_the_other_sheets_id() -> None:
    rule = MarginRule(band="top", align="end")
    with_id = _view(rule)
    without = replace(_view(rule, code="L4736-0002"), id_placement="manual")

    first, second = _pages(with_id, without)

    assert first.margin_id is True and first.feed_mark.end == "top_left"  # type: ignore[attr-defined]
    assert second.margin_id is False  # type: ignore[attr-defined]
    assert second.feed_mark is not None and second.feed_mark.end == "top_right"  # type: ignore[attr-defined]
