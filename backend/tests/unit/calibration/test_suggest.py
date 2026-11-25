# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The geometry suggestion from check-print readings (#240 comment, design 7.4).

The readings are simulated end to end: the printer misplaces ink by ``PRINTER``, the hub draws
through the stored correction (the inverse of ``CORRECTION``), and the reader measures the ink
against the crossings of the sheet as it really is (``TRUE``).
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from app.calibration.fit import Reading, crossing_point
from app.calibration.suggest import suggest_geometry
from app.page_render.transform import Deviation, apply_deviation, correction_mm
from app.sheets.geometry import Geometry

# L4737REV-like: 5 x 13 labels on A4, the stored geometry.
G = Geometry("A4", 21000, 29700, 3810, 2120, 5, 13, 4820, 2120, 1080, 990, None, "rectangle")
PRINTER = Deviation(120, -40, 1_001_000, 999_000, 300_000)
# Pass 1 of a check sheet (outer corners and centre) plus a few more crossings.
CROSSINGS = [(1, 1), (4, 1), (1, 12), (4, 12), (2, 6), (3, 9), (2, 3)]
CORRECTION_VERSION = uuid4()
RUN_VERSION = uuid4()


def _readings(
    true: Geometry, *, printer: Deviation = PRINTER, correction: Deviation | None = PRINTER
) -> list[Reading]:
    out = []
    for col_gap, row_gap in CROSSINGS:
        drawn = crossing_point(G, col_gap, row_gap)  # the hub draws the stored geometry
        if correction is not None:
            drawn = correction_mm(correction, "A4").apply(drawn)
        ink = apply_deviation(printer, "A4", drawn)
        paper = crossing_point(true, col_gap, row_gap)  # the crossing on the real sheet
        out.append(Reading(col_gap, row_gap, ink[0] - paper[0], ink[1] - paper[1]))
    return out


def _suggest(readings: list[Reading]):  # type: ignore[no-untyped-def]
    return suggest_geometry(
        G,
        readings,
        correction_type_version_id=CORRECTION_VERSION,
        run_type_version_id=RUN_VERSION,
    )


def test_synthetic_pitch_error_recovered_within_0_05_mm() -> None:
    true = replace(G, pitch_x=4755, pitch_y=2131, margin_left=1030, margin_top=1050)
    s = _suggest(_readings(true))
    assert s is not None
    assert s.pitch_x_hmm == pytest.approx(4755, abs=5)  # 48.20 -> 47.55 mm, within 0.05 mm
    assert s.pitch_y_hmm == pytest.approx(2131, abs=5)
    assert s.margin_left_hmm == pytest.approx(1030, abs=5)
    assert s.margin_top_hmm == pytest.approx(1050, abs=5)
    assert s.uneven is False


def test_compensated_printer_offset_and_scale_suggest_no_change() -> None:
    s = _suggest(_readings(G))
    assert s is not None
    assert (s.pitch_x_hmm, s.pitch_y_hmm, s.margin_left_hmm, s.margin_top_hmm) == (
        None,
        None,
        None,
        None,
    )
    assert s.uneven is False


def test_changes_below_5_hmm_are_reported_as_none() -> None:
    true = replace(G, pitch_x=G.pitch_x + 1, margin_top=G.margin_top - 3)
    s = _suggest(_readings(true))
    assert s is not None
    assert s.pitch_x_hmm is None and s.margin_top_hmm is None


def test_same_type_correction_gives_no_suggestion() -> None:
    same = uuid4()
    true = replace(G, pitch_x=4755)
    assert (
        suggest_geometry(
            G, _readings(true), correction_type_version_id=same, run_type_version_id=same
        )
        is None
    )


def test_uneven_pattern_reported() -> None:
    readings = _readings(G)
    bad = readings[4]  # (2, 6): the centre, off on its own
    readings[4] = Reading(bad.col_gap, bad.row_gap, bad.across_mm + 0.8, bad.down_mm)
    s = _suggest(readings)
    assert s is not None
    assert s.uneven is True


def test_one_column_gap_gives_no_pitch_x() -> None:
    readings = [r for r in _readings(replace(G, pitch_x=4755)) if r.col_gap == 1]
    s = _suggest(readings)
    assert s is not None
    assert s.pitch_x_hmm is None and s.margin_left_hmm is None


def test_a_pitch_below_the_label_size_is_not_offered() -> None:
    """A gapless type (pitch_y == label_h) cannot take a smaller pitch: the axis is left out so
    an apply never fails with invalid_geometry (review M1)."""
    assert G.pitch_y == G.label_h
    true = replace(G, pitch_y=G.pitch_y - 7, margin_top=G.margin_top + 20, pitch_x=4755)
    s = _suggest(_readings(true))
    assert s is not None
    assert s.pitch_y_hmm is None and s.margin_top_hmm is None
    assert s.pitch_x_hmm == pytest.approx(4755, abs=5)  # the other axis stays
