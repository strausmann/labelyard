# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Check sheets: one sheet of labels printed in up to three passes (#240, design 7.2, ADR 0029).

``check_passes`` decides which crossings each pass prints. Passes are disjoint and never touch:
no crossing of one pass is next to a crossing of another, diagonals included, so the crosses of
a later pass never land on an earlier one's, also when the correction moved between them.

* Pass 1 is the four outer crossings and the centre.
* Pass ``k`` moves the four corners ``k - 1`` columns and ``2(k - 1)`` rows inwards ("shifted
  by one column and two rows"), and adds one crossing near the centre.
* A crossing outside the grid, or one that touches a crossing of an earlier pass, is left out.
  A pass needs three crossings that are not on one line, as the fit does; the first pass
  without them ends the list. The centre of each later pass is the one of a short list of
  positions around the centre that leaves the most room for the passes after it, so the result
  does not depend on luck; it is deterministic.

A grid with fewer than five crossings (or with all of them on one line) has no passes: its check
print is the single full check page, as before 1.8.0.

``open_check_sheet`` reads the newest open sheet of a printer, tray and sheet type version for
the print dialog; the pass of a print is taken in ``runs.allocate_run``'s write transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import product
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.models.calibration_run import (
    CalibrationCheckSheet,
    CalibrationCheckSheetPass,
    CalibrationRun,
)

__all__ = [
    "MAX_PASSES",
    "CheckSheetOpen",
    "CheckSheetPassRead",
    "Crossing",
    "check_passes",
    "newest_open_sheet",
    "open_check_sheet",
]

MAX_PASSES: Final = 3
"""Passes one sheet takes at most (design 7.2)."""

Crossing = tuple[int, int]
"""A crossing between four labels: ``(col_gap, row_gap)``, both from 1 (``S`` and ``Z``)."""

_CENTRE_OFFSETS: Final[tuple[tuple[int, int], ...]] = (
    (1, 2), (-1, -2), (1, -2), (-1, 2), (1, 0), (-1, 0), (0, 2), (0, -2),
)  # fmt: skip
"""Where a later pass puts its centre crossing, per step of the shift, in this order."""


def _touches(a: Crossing, b: Crossing) -> bool:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1])) < 2


def _on_one_line(points: Sequence[Crossing]) -> bool:
    (x0, y0) = points[0]
    return all(
        (x1 - x0) * (y2 - y0) == (x2 - x0) * (y1 - y0) for x1, y1 in points for x2, y2 in points
    )


def _build(cols: int, rows: int, centres: Sequence[int]) -> list[list[Crossing]]:
    """The passes for one choice of centre offsets (one per later pass)."""
    nx, ny = cols - 1, rows - 1  # crossings per row and per column
    cx, cy = (nx + 1) // 2, (ny + 1) // 2
    out: list[list[Crossing]] = []
    taken: list[Crossing] = []
    for k in range(MAX_PASSES):
        dx, dy = k, 2 * k
        corners = [(1 + dx, 1 + dy), (nx - dx, 1 + dy), (1 + dx, ny - dy), (nx - dx, ny - dy)]
        if k == 0:
            centre = (cx, cy)
        else:
            ox, oy = _CENTRE_OFFSETS[centres[k - 1]]
            centre = (cx + ox * k, cy + oy * k)
        crossings: list[Crossing] = []
        for c in [*corners, centre]:
            inside = 1 <= c[0] <= nx and 1 <= c[1] <= ny
            if inside and c not in crossings and not any(_touches(c, t) for t in taken):
                crossings.append(c)
        if len(crossings) < 3 or _on_one_line(crossings):
            break
        out.append(crossings)
        taken += crossings
    return out


def check_passes(cols: int, rows: int) -> list[list[Crossing]]:
    """The crossings of each pass of a check sheet of ``cols`` x ``rows`` labels (at most 3).

    Pure and deterministic; ``[]`` below five crossings or when they all lie on one line.
    """
    if cols < 2 or rows < 2 or (cols - 1) * (rows - 1) < 5:
        return []
    best: list[list[Crossing]] = []
    best_key = (-1, -1)
    for choice in product(range(len(_CENTRE_OFFSETS)), repeat=MAX_PASSES - 1):
        passes = _build(cols, rows, choice)
        key = (len(passes), sum(map(len, passes)))
        if key > best_key:  # the first choice wins a tie: the order of ``_CENTRE_OFFSETS``
            best, best_key = passes, key
    return best


# ---------------------------------------------------------------------------- the open sheet


class CheckSheetPassRead(BaseModel):
    pass_no: int = Field(ge=1, le=MAX_PASSES)
    run_no: int = Field(description="The run of the pass (per printer and tray).")
    run_id: UUID = Field(description="The run id; readings of this pass are saved against it.")


class CheckSheetOpen(BaseModel):
    """A check sheet that still has a free pass (design 7.2)."""

    check_sheet_id: UUID
    passes_total: int = Field(ge=1, le=MAX_PASSES, description="Passes the sheet takes.")
    passes_used: int = Field(ge=0, le=MAX_PASSES)
    next_pass: int = Field(ge=1, le=MAX_PASSES, description="The pass a print on it takes.")
    run_line_slots: int = Field(
        ge=0,
        le=2,
        description=(
            "Run-line slots for passes 2 and 3 on this printer and tray; fewer than "
            "`passes_total - 1`: the later passes print without their run line."
        ),
    )
    passes: list[CheckSheetPassRead] = Field(description="The printed passes, by pass number.")


async def first_pass_correction(
    session: AsyncSession, sheet_id: UUID
) -> tuple[bool, dict[str, Any] | None]:
    """Whether pass 1 of the sheet still has its run, and the correction it was printed with
    (``calibration_runs.correction``): a later pass puts its run line where pass 1 left room."""
    row = (
        await session.execute(
            select(col(CalibrationRun.correction))
            .join(
                CalibrationCheckSheetPass,
                col(CalibrationCheckSheetPass.run_id) == col(CalibrationRun.id),
            )
            .where(
                col(CalibrationCheckSheetPass.check_sheet_id) == sheet_id,
                col(CalibrationCheckSheetPass.pass_no) == 1,
            )
        )
    ).first()
    return (row is not None, row[0] if row is not None else None)


async def newest_open_sheet(
    session: AsyncSession, printer_id: UUID, tray: str, type_version_id: UUID
) -> CalibrationCheckSheet | None:
    """The newest check sheet of the printer, tray and version that is not closed."""
    return (
        await session.execute(
            select(CalibrationCheckSheet)
            .where(
                col(CalibrationCheckSheet.printer_id) == printer_id,
                col(CalibrationCheckSheet.tray) == tray,
                col(CalibrationCheckSheet.type_version_id) == type_version_id,
                col(CalibrationCheckSheet.closed_at).is_(None),
            )
            .order_by(col(CalibrationCheckSheet.created_at).desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def open_check_sheet(
    session: AsyncSession,
    printer_id: UUID,
    tray: str,
    type_version_id: UUID,
    *,
    run_line_slots: int,
) -> CheckSheetOpen | None:
    """The newest open sheet with its passes; ``None`` when there is none or it is full."""
    sheet = await newest_open_sheet(session, printer_id, tray, type_version_id)
    if sheet is None:
        return None
    rows = (
        await session.execute(
            select(col(CalibrationCheckSheetPass.pass_no), CalibrationRun)
            .join(CalibrationRun, col(CalibrationRun.id) == col(CalibrationCheckSheetPass.run_id))
            .where(col(CalibrationCheckSheetPass.check_sheet_id) == sheet.id)
            .order_by(col(CalibrationCheckSheetPass.pass_no))
        )
    ).all()
    used = {pass_no for pass_no, _ in rows}
    free = [n for n in range(1, sheet.passes_total + 1) if n not in used]
    if not free:
        return None
    return CheckSheetOpen(
        check_sheet_id=sheet.id,
        passes_total=sheet.passes_total,
        passes_used=len(rows),
        next_pass=free[0],
        run_line_slots=run_line_slots,
        passes=[
            CheckSheetPassRead(pass_no=pass_no, run_no=run.run_no, run_id=run.id)
            for pass_no, run in rows
        ],
    )
