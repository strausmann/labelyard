# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The waiting signal (#403, spec 5.6): the planner tells apart a sheet code or feed mark that
waits for another job from one that has no room.

Geometry: an L4736 sheet (4 x 12 cells of 45.7 x 21.2 mm, top and bottom margin 21.3 mm, left
9.85 mm), as in ``test_margin_placement_zones``.
"""

from __future__ import annotations

import uuid
from typing import Literal

from app.page_render.transform import Deviation
from app.sheets.free_rects import Rect
from app.sheets.geometry import Geometry
from app.sheets.planner import (
    MarginContext,
    MarginRule,
    PlacementIn,
    SheetView,
    decide_margin_and_mark,
    plan,
)

L4736 = Geometry("A4", 21000, 29700, 4570, 2120, 4, 12, 4820, 2120, 2130, 985, None, "rectangle")
# Both ends of the top band are preprinted: the feed mark finds no place.
BOTH_ENDS = (Rect(985, 500, 3000, 1530), Rect(17000, 500, 3015, 1530))


def _view(
    placement: Literal["cell", "margin", "manual", "back_side"] = "margin",
    *,
    rule: MarginRule | None = None,
    **kw: object,
) -> SheetView:
    fields: dict[str, object] = {
        "code": "L4736-0001",
        "type_key": "acme",
        "type_version_id": uuid.UUID(int=1),
        "geometry": L4736,
        "allow_qr": True,
        "non_free": {},
        "id_placement": placement,
        "id_printed": False,
        "has_hub_cell": False,
        "margin_id_in_flight": False,
        "state": "active",
        "margin_rule": rule or MarginRule(),
    }
    fields.update(kw)
    return SheetView(**fields)  # type: ignore[arg-type]


def _ctx() -> MarginContext:
    return MarginContext(
        deviation=Deviation(),
        scan_payload=lambda c: f"https://hub.example.invalid/sheet/{c}",
        render_dpi=600,
        language="en",
    )


def test_margin_id_in_flight_sets_id_pending_not_id_not_printed() -> None:
    decision = decide_margin_and_mark(_view(margin_id_in_flight=True), _ctx(), None)

    assert decision.margin_id is False and decision.placement is None
    assert decision.id_pending is True
    assert decision.id_not_printed is False


def test_cell_sheet_with_reserved_id_cell_sets_id_pending() -> None:
    for state in ("reserved", "unconfirmed"):
        view = _view(
            "cell", non_free={1: state}, has_hub_cell=True, hub_cell_state=state, id_cell_no=1
        )

        decision = decide_margin_and_mark(view, _ctx(), None)

        assert decision.id_pending is True, state
        assert decision.id_not_printed is False and decision.margin_id is False
        # The planner places no second ID cell on the page (needs_id_cell is false).
        (page,) = plan([PlacementIn(view, "first_free", count=1)], 1, hw_margins_hmm=None,
                       margin=_ctx()).pages  # fmt: skip
        assert page.id_cell is None and page.id_pending is True
    used = _view("cell", non_free={1: "used"}, has_hub_cell=True, hub_cell_state="used")
    assert decide_margin_and_mark(used, _ctx(), None).id_pending is False
    for other in ("back_side", "manual"):
        assert decide_margin_and_mark(_view(other), _ctx(), None).id_pending is False  # type: ignore[arg-type]


def test_no_room_sets_id_not_printed_not_id_pending() -> None:
    rule = MarginRule(band="right")  # the right band of L4736 is too shallow for a code

    decision = decide_margin_and_mark(_view(rule=rule), _ctx(), None)

    assert decision.id_not_printed is True
    assert decision.id_pending is False


def test_mark_in_flight_reason_in_flight() -> None:
    decision = decide_margin_and_mark(_view("manual", feed_mark_in_flight=True), _ctx(), None)

    assert decision.feed_mark is None and decision.mark_was_due is False
    assert decision.feed_mark_pending is True
    assert decision.feed_mark_reason == "in_flight"
    # Without a printer context the mark is still known to be on its way.
    blind = decide_margin_and_mark(_view("manual", feed_mark_in_flight=True), None, None)
    assert blind.feed_mark_pending is True and blind.feed_mark_reason == "in_flight"
    printed = decide_margin_and_mark(_view("manual", feed_mark_printed=True), _ctx(), None)
    assert printed.feed_mark_pending is False and printed.feed_mark_reason is None


def test_mark_without_place_reason_no_free_place_and_stays_due() -> None:
    view = _view("manual", rule=MarginRule(zones=BOTH_ENDS))

    decision = decide_margin_and_mark(view, _ctx(), None)

    assert decision.feed_mark is None
    assert decision.feed_mark_pending is True
    assert decision.feed_mark_reason == "no_free_place"
    assert decision.mark_was_due is True  # due on this page, not printed
    (page,) = plan([PlacementIn(view, "first_free", count=1)], 1, hw_margins_hmm=None,
                   margin=_ctx()).pages  # fmt: skip
    assert page.feed_mark is None and page.feed_mark_pending is True
    assert page.feed_mark_reason == "no_free_place"
    # Not recorded: the sheet's flag stays unset, so the next page decides again.
    assert view.feed_mark_printed is False
    again = decide_margin_and_mark(view, _ctx(), None)
    assert again.mark_was_due is True and again.feed_mark_reason == "no_free_place"
    placed = decide_margin_and_mark(_view("manual"), _ctx(), None)
    assert placed.feed_mark is not None
    assert placed.feed_mark_pending is False and placed.feed_mark_reason is None
