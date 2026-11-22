# SPDX-FileCopyrightText: 2026 Bj\u00f6rn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""``corner_radius`` is at most half the shorter label side (#204 note 4, 1.8.0)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from app.sheets.cell_render import BoxHmm, content_box
from app.sheets.errors import CornerRadiusTooLargeError
from app.sheets.geometry import Geometry, max_corner_radius, validate_geometry

# 25.4 x 10.0 mm labels: the shorter side is 10.0 mm, half of it 5.0 mm (500 hmm).
G = Geometry("A4", 21000, 29700, 2540, 1000, 7, 27, 2790, 1000, 1350, 860, 150, "rectangle")


def test_the_bound_is_half_the_shorter_side() -> None:
    assert max_corner_radius(G) == 500
    assert max_corner_radius(replace(G, label_w=900, pitch_x=2790)) == 450
    assert max_corner_radius(replace(G, label_h=1001, pitch_y=1001)) == 500  # integer half


def test_radius_up_to_half_is_valid() -> None:
    assert validate_geometry(replace(G, corner_radius=500)) is not None
    assert validate_geometry(replace(G, corner_radius=0)) is not None


def test_radius_over_half_is_refused_with_its_own_code() -> None:
    with pytest.raises(CornerRadiusTooLargeError) as raised:
        validate_geometry(replace(G, corner_radius=501))
    assert raised.value.status == 422
    assert raised.value.error_code == "corner_radius_too_large"
    assert raised.value.detail is not None and raised.value.detail["field"] == "corner_radius"


def test_a_round_label_has_no_radius_to_check() -> None:
    round_g = Geometry("A4", 21000, 29700, 3000, 3000, 5, 7, 3500, 3500, 1000, 1000, None, "round")
    assert validate_geometry(round_g) is not None


def test_stored_oversized_radius_is_clamped_at_render() -> None:
    """A type stored before the bound existed keeps drawing: the inset uses at most half the
    shorter side, so the content box of a 10 mm label is not eaten by a 100 mm radius."""
    huge = replace(G, corner_radius=10_000)
    at_half = replace(G, corner_radius=500)
    assert content_box(huge, 1) == content_box(at_half, 1)
    assert content_box(huge, 1) == BoxHmm(860 + 250, 1350 + 250, 2540 - 500, 1000 - 500)
