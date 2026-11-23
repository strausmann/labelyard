# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Free rectangles of a margin band around keep-out zones (#246, spec 5.2, ADR 0023 D33).

A band is cut into the rectangles where nothing is preprinted. The cutting is not unique; what
``free_rects`` promises is: every rectangle lies inside the band, overlaps no zone and is at
least ``min_depth`` deep, and a band without zones comes back as itself. Rectangles may overlap
each other: the caller picks one.

All lengths are hundredths of a millimetre (D16), in page coordinates (origin top left, ``y``
down).
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["Rect", "free_rects"]


@dataclass(frozen=True, slots=True, order=True)
class Rect:
    """An axis-parallel rectangle in hmm; ``x``/``y`` its top left corner."""

    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    def overlaps(self, other: Rect) -> bool:
        """Whether the two share an area (touching edges do not count)."""
        return self.x < other.x2 and other.x < self.x2 and self.y < other.y2 and other.y < self.y2

    def grown(self, dx: int, dy: int) -> Rect:
        """The rectangle grown by ``dx`` on the left and right and ``dy`` on the top and bottom."""
        return Rect(self.x - dx, self.y - dy, self.w + 2 * dx, self.h + 2 * dy)


def _subtract(lo: int, hi: int, cuts: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """``[lo, hi]`` without the open intervals ``cuts``."""
    out: list[tuple[int, int]] = []
    pos = lo
    for a, b in sorted(cuts):
        if a > pos:
            out.append((pos, min(a, hi)))
        pos = max(pos, b)
        if pos >= hi:
            break
    if pos < hi:
        out.append((pos, hi))
    return [(a, b) for a, b in out if b > a]


def _cut_along_x(band: Rect, zones: Sequence[Rect], min_depth: int) -> list[Rect]:
    """The band as a strip along ``x`` (depth ``h``)."""
    hits = [z for z in zones if z.overlaps(band)]
    edges = sorted(
        {band.x, band.x2} | {e for z in hits for e in (z.x, z.x2) if band.x < e < band.x2}
    )
    slabs: list[list[tuple[int, int]]] = []
    for left, right in itertools.pairwise(edges):
        cuts = [(max(z.y, band.y), min(z.y2, band.y2)) for z in hits if z.x < right and left < z.x2]
        slabs.append(_subtract(band.y, band.y2, cuts))

    def holds(i: int, a: int, b: int) -> bool:
        return any(c <= a and b <= d for c, d in slabs[i])

    found: set[Rect] = set()
    for i, free in enumerate(slabs):
        for a, b in free:
            if b - a < min_depth:
                continue
            lo = i
            while lo > 0 and holds(lo - 1, a, b):
                lo -= 1
            hi = i
            while hi + 1 < len(slabs) and holds(hi + 1, a, b):
                hi += 1
            found.add(Rect(edges[lo], a, edges[hi + 1] - edges[lo], b - a))
    return sorted(found)


def free_rects(
    band: Rect, zones: Sequence[Rect], *, min_depth: int, horizontal: bool | None = None
) -> list[Rect]:
    """The rectangles of ``band`` that overlap no zone and are at least ``min_depth`` deep.

    ``horizontal`` says whether the band runs along ``x`` (top and bottom: the depth is the
    height) or along ``y`` (right and left: the depth is the width); left out, the longer side is
    the band's length.
    """
    if band.w <= 0 or band.h <= 0:
        return []
    along_x = band.w >= band.h if horizontal is None else horizontal
    if along_x:
        return _cut_along_x(band, zones, min_depth)
    turned = _cut_along_x(
        Rect(band.y, band.x, band.h, band.w), [Rect(z.y, z.x, z.h, z.w) for z in zones], min_depth
    )
    return sorted(Rect(r.y, r.x, r.h, r.w) for r in turned)
