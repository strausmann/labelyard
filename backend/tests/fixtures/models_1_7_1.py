# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The job payload models of Labelyard 1.7.1, frozen (ADR 0034).

Copied from commit ``40bb6566`` (``backend/app/services/page_jobs.py``,
``backend/app/sheets/sheet_page.py``, ``backend/app/sheets/back_side.py``,
``backend/app/sheets/geometry.py``, ``backend/app/sheets/planner.py``,
``backend/app/schemas/content_type.py``) with every imported type inlined. This module imports
nothing from ``app``: a later change to the live code cannot widen what "1.7.1 can read" means.

What 1.7.1 does with a stored job after a downgrade is parse its payload with these models; a
payload that does not parse fails the job. ``test_payloads_readable_by_1_7_1.py`` builds every
payload kind with the production builders and parses it here.

Only the validation is copied; methods that render (``MarginIdPayload.draws_qr``) are left out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

# ---------------------------------------------------------------- app/schemas/content_type.py
ContentType = Literal[
    "qr_only",
    "qr_one_line",
    "qr_two_lines",
    "qr_three_lines",
    "text_one_line",
    "text_two_lines",
    "qr_with_listing",
    "spool_swatch",
]

# ---------------------------------------------------------------- app/sheets/planner.py
MarginBand = Literal["top", "bottom", "right", "left"]
MarginCorner = Literal["top_left", "top_right", "bottom_left"]


# ---------------------------------------------------------------- app/sheets/geometry.py
@dataclass(frozen=True)
class Geometry:
    page_size: Literal["A4", "Letter"]
    page_w: int
    page_h: int
    label_w: int
    label_h: int
    cols: int
    rows: int
    pitch_x: int
    pitch_y: int
    margin_top: int
    margin_left: int
    corner_radius: int | None
    shape: Literal["rectangle", "round"]


def cell_count(g: Geometry) -> int:
    return g.cols * g.rows


# ---------------------------------------------------------------- app/services/page_jobs.py
PageKind = Literal[
    "sheet_page", "calibration_page", "test_page", "sheet_back_page", "tray_loading_page"
]

TAPE_RERENDER_KEYS: frozenset[str] = frozenset({"content_type", "label_data", "rendered_tape_mm"})
"""``print_queue._REQUIRED_RERENDER_KEYS`` of 1.7.1: what a stored tape job (a payload without
``kind``) needs to be drawn again."""


class CalibrationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str
    dx_hmm: int = 0
    dy_hmm: int = 0
    sx_ppm: int = 1_000_000
    sy_ppm: int = 1_000_000
    rotation_udeg: int = 0


class PagePayload(BaseModel):
    kind: PageKind
    media: Literal["A4", "Letter"]
    tray: str
    render_dpi: int
    job_name: str
    calibration: CalibrationSnapshot | None = None
    detail: dict[str, Any] = {}
    retry_of: UUID | None = None


# ---------------------------------------------------------------- app/sheets/sheet_page.py
class SheetCellPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cell: int
    content_type: ContentType
    label_data: dict[str, Any]
    mapped: bool
    source: str | None = None
    purpose: str | None = None
    ref: str | None = None


class MarginIdPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    qr_payload: str | None = None
    usable_hmm: int
    band: MarginBand = "top"
    corner: MarginCorner = "top_left"
    qr_allowed: bool | None = None
    text_end_hmm: int | None = None


class FeedMarkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    x_hmm: int
    baseline_y_hmm: int
    size_pt: float
    band: Literal["top"] = "top"
    anchor: Literal["top_right"] = "top_right"


class SheetPageDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sheet_id: UUID
    sheet_code: str | None
    type_key: str
    type_version_id: UUID
    geometry: dict[str, Any]
    allow_qr: bool
    cells: list[SheetCellPayload]
    id_cell: SheetCellPayload | None = None
    margin_id: MarginIdPayload | None = None
    feed_mark: FeedMarkPayload | None = None
    job_summary: dict[str, Any]

    @model_validator(mode="after")
    def _cells_valid(self) -> SheetPageDetail:
        total = cell_count(Geometry(**self.geometry))
        numbers = [c.cell for c in self.cells] + ([self.id_cell.cell] if self.id_cell else [])
        if len(set(numbers)) != len(numbers):
            raise ValueError("cell numbers must be unique on a page")
        if any(not 1 <= n <= total for n in numbers):
            raise ValueError(f"cell numbers must lie in 1..{total} of the pinned geometry")
        return self


# ---------------------------------------------------------------- app/sheets/back_side.py
@dataclass(frozen=True)
class StripHmm:
    x: int
    y: int
    w: int
    h: int


class SheetBackDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sheet_id: UUID
    sheet_code: str
    type_text: str
    date: str
    scan_payload: str
    strip: StripHmm
    rotate_180: bool
