# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The tolerance a sent value has against the hub's fit (#336, design 7.5)."""

from __future__ import annotations

from app.calibration.service import (
    CLIENT_FIT_TOLERANCE_HMM,
    displacement_at_far_corner,
    ignored_fields,
)
from app.page_render.transform import Deviation

A4_W, A4_H = 21000, 29700
FIT = Deviation(120, -40, 1_001_000, 999_000, 300_000)


def test_tolerance_is_five_hundredths() -> None:
    assert CLIENT_FIT_TOLERANCE_HMM == 5


def test_equal_deviations_move_nothing() -> None:
    assert displacement_at_far_corner(FIT, FIT, A4_W, A4_H) == 0


def test_offsets_do_not_count_as_displacement() -> None:
    moved = Deviation(500, 500, FIT.scale_x_ppm, FIT.scale_y_ppm, FIT.rotation_udeg)
    assert displacement_at_far_corner(moved, FIT, A4_W, A4_H) == 0


def test_scale_moves_the_corner_by_half_the_page_width() -> None:
    # 0.002 on scale x moves the corner 105 mm from the centre by 0.21 mm.
    a = Deviation(0, 0, 1_002_000, 1_000_000, 0)
    b = Deviation(0, 0, 1_000_000, 1_000_000, 0)
    assert displacement_at_far_corner(a, b, A4_W, A4_H) == 21


def test_rotation_moves_the_corner_along_the_half_diagonal() -> None:
    # 0.01 degrees on a half diagonal of 181.9 mm is 0.0317 mm.
    a = Deviation(0, 0, 1_000_000, 1_000_000, 10_000)
    b = Deviation(0, 0, 1_000_000, 1_000_000, 0)
    assert displacement_at_far_corner(a, b, A4_W, A4_H) == 3


def test_ignored_fields_names_each_value_beyond_the_tolerance() -> None:
    sent = Deviation(126, -40, 1_001_000, 999_000, 300_000)
    assert ignored_fields(sent, FIT, A4_W, A4_H) == ["offset_x_mm"]
    sent = Deviation(125, -46, 1_001_000, 999_000, 330_000)
    assert ignored_fields(sent, FIT, A4_W, A4_H) == ["offset_y_mm", "rotation_deg"]
    sent = Deviation(120, -40, 1_001_000, 1_000_000, 300_000)
    assert ignored_fields(sent, FIT, A4_W, A4_H) == ["scale_y"]
