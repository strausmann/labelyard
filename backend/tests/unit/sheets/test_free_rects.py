# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""``free_rects``: a band cut into rectangles around keep-out zones (#246, spec 5.2).

The property test is the contract: any cutting is correct as long as every rectangle lies in the
band, touches no zone and is at least ``min_depth`` deep, and a band without zones comes back
whole.
"""

from __future__ import annotations

from app.sheets.free_rects import Rect, free_rects
from hypothesis import given, settings
from hypothesis import strategies as st

BAND = Rect(985, 500, 19030, 1530)  # the top band of an L4736 sheet, hmm


def _overlaps(a: Rect, b: Rect) -> bool:
    return a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h


def _inside(a: Rect, outer: Rect) -> bool:
    return (
        a.x >= outer.x
        and a.y >= outer.y
        and a.x + a.w <= outer.x + outer.w
        and a.y + a.h <= outer.y + outer.h
    )


_zone = st.builds(
    Rect,
    x=st.integers(0, 21000),
    y=st.integers(0, 3000),
    w=st.integers(1, 6000),
    h=st.integers(1, 3000),
)


@settings(max_examples=300, deadline=None)
@given(zones=st.lists(_zone, max_size=16), min_depth=st.integers(100, 1500))
def test_free_rects_never_overlap_a_zone(zones: list[Rect], min_depth: int) -> None:
    found = free_rects(BAND, zones, min_depth=min_depth)
    for r in found:
        assert r.w > 0 and r.h >= min_depth
        assert _inside(r, BAND)
        assert not any(_overlaps(r, z) for z in zones), (r, zones)


@settings(max_examples=100, deadline=None)
@given(zones=st.lists(_zone, max_size=16))
def test_free_rects_of_a_side_band_never_overlap_a_zone(zones: list[Rect]) -> None:
    side = Rect(20115, 2130, 400, 25440)
    for r in free_rects(side, zones, min_depth=400):
        assert r.h > 0 and r.w >= 400
        assert _inside(r, side)
        assert not any(_overlaps(r, z) for z in zones)


def test_a_band_without_zones_is_one_rectangle() -> None:
    assert free_rects(BAND, [], min_depth=400) == [BAND]


def test_a_zone_over_the_left_part_leaves_the_rest() -> None:
    logo = Rect(985, 500, 3000, 1530)
    assert free_rects(BAND, [logo], min_depth=400) == [Rect(3985, 500, 16030, 1530)]


def test_a_shallow_zone_keeps_a_long_strip_and_a_deep_piece() -> None:
    # The zone takes the outer 1000 of the band over 3000 hmm: a long, shallow strip along the grid
    # and the deep pieces beside the zone are all free.
    zone = Rect(5000, 500, 3000, 1000)
    found = set(free_rects(BAND, [zone], min_depth=400))
    assert Rect(985, 1500, 19030, 530) in found
    assert Rect(985, 500, 4015, 1530) in found
    assert Rect(8000, 500, 12015, 1530) in found


def test_a_piece_below_the_minimal_depth_is_dropped() -> None:
    zone = Rect(985, 500, 19030, 1200)  # leaves 330 hmm along the grid
    assert free_rects(BAND, [zone], min_depth=400) == []


def test_overlapping_free_intervals_join_on_their_shared_part() -> None:
    """Codex P2 on #439: two slabs whose free intervals overlap without one containing the other
    still give the rectangle over both slabs on the shared part."""
    band = Rect(1000, 500, 1800, 1600)
    zones = [Rect(1000, 500, 900, 400), Rect(1900, 1700, 900, 400)]

    found = free_rects(band, zones, min_depth=400, horizontal=True)

    assert Rect(1000, 900, 1800, 800) in found


def _covers(outer: Rect, inner: Rect) -> bool:
    return _inside(inner, outer)


_band_zone = st.builds(
    Rect,
    x=st.integers(985, 20015),
    y=st.integers(300, 2100),
    w=st.integers(100, 8000),
    h=st.integers(100, 1200),
)


def _brute_force(band: Rect, zones: list[Rect], min_depth: int) -> list[Rect]:
    """Every free rectangle with edges on band or zone edges (a superset of the maximal ones)."""
    xs = sorted({band.x, band.x + band.w} | {e for z in zones for e in (z.x, z.x + z.w)})
    ys = sorted({band.y, band.y + band.h} | {e for z in zones for e in (z.y, z.y + z.h)})
    xs = [v for v in xs if band.x <= v <= band.x + band.w]
    ys = [v for v in ys if band.y <= v <= band.y + band.h]
    out = []
    for i, x1 in enumerate(xs):
        for x2 in xs[i + 1 :]:
            for j, y1 in enumerate(ys):
                for y2 in ys[j + 1 :]:
                    r = Rect(x1, y1, x2 - x1, y2 - y1)
                    if r.h >= min_depth and not any(_overlaps(r, z) for z in zones):
                        out.append(r)
    return out


@settings(max_examples=200, deadline=None)
@given(zones=st.lists(_band_zone, max_size=5))
def test_every_free_rectangle_of_min_depth_is_covered(zones: list[Rect]) -> None:
    """Completeness: every rectangle of the band at least ``min_depth`` deep that touches no zone
    lies inside some returned rectangle, so the largest free area is always among them."""
    found = free_rects(BAND, zones, min_depth=400)
    for probe in _brute_force(BAND, zones, 400):
        assert any(_covers(r, probe) for r in found), (probe, zones)
