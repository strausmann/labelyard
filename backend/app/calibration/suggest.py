# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A sheet geometry suggested from check-print readings (#240 comment, design 7.4).

On a calibration page a pitch error and a margin error cannot be told apart from the printer's
scale and offset: both are linear in the gap index, and the affine fit absorbs them. A **check**
print is drawn through the stored correction; when that correction was measured on another sheet
type, what is left in the readings is the geometry of this type. A reading is ink minus the
crossing on the real sheet, so along the S gaps ``across = (ml - ml') + (i - 0.5)(px - px')``
(the stored geometry unprimed, the real one primed) and along the Z gaps the same for ``down``.
A least-squares line per axis gives the real pitch (from the slope) and margin (from the
intercept); what the line does not explain is reported as ``uneven``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.calibration.fit import RESIDUAL_FLAG_MM, Reading
from app.models.calibration_run import CalibrationHistory, CalibrationRun
from app.models.job import Job
from app.models.sheet import SheetTypeVersion
from app.repositories.write_lock import write_transaction
from app.sheets import type_service as ts
from app.sheets.errors import SheetApiError
from app.sheets.geometry import Geometry, normalise_shape, validate_geometry

#: A proposed value closer than this to the stored one is no change (hundredths of a mm).
MIN_CHANGE_HMM = 5

SuggestionUnavailable = Literal["same_type", "no_check_run", "unknown_correction"]


class GeometrySuggestion(BaseModel):
    """Proposed geometry values in hundredths of a millimetre; ``null`` means "keep it": a change
    under 0.05 mm (unless the other value of its axis needs it to keep the grid on the page),
    too few gaps on that axis, or an axis the geometry rules would refuse, such as a pitch below
    the label size (then pitch and margin of that axis are both ``null``). The proposal as a
    whole is always a geometry a new version may have."""

    pitch_x_hmm: int | None = Field(description="From the trend across the S gaps.")
    pitch_y_hmm: int | None = Field(description="From the trend across the Z gaps.")
    margin_left_hmm: int | None = Field(description="From the constant part across the S gaps.")
    margin_top_hmm: int | None = Field(description="From the constant part across the Z gaps.")
    uneven: bool = Field(
        description=(
            "A reading lies more than 0.3 mm off the trend: the pattern is not a pitch or margin "
            "error, check the sheet rather than the printer."
        )
    )


def _line(t: list[float], p: list[float]) -> tuple[float, float] | None:
    """Least-squares ``p = c0 + c1 * t``; ``None`` with fewer than two distinct ``t``."""
    n = len(t)
    if len(set(t)) < 2:
        return None
    mt, mp = sum(t) / n, sum(p) / n
    stt = sum((x - mt) ** 2 for x in t)
    c1 = sum((x - mt) * (y - mp) for x, y in zip(t, p, strict=True)) / stt
    return mp - c1 * mt, c1


Axis = tuple[int | None, int | None]
"""(pitch, margin) of one axis in hundredths of a millimetre; ``None`` keeps the stored value."""


def _raw_axis(line: tuple[float, float] | None, pitch: int, margin: int, label: int) -> Axis | None:
    """The rounded (pitch, margin) the trend of one axis points at, each ``None`` where it equals
    the stored value; ``None`` without a trend or when the pitch would fall below the label size
    (a gapless sheet read as smaller), which no version may have."""
    if line is None:
        return None
    c0, c1 = line
    new_pitch = round(pitch - c1 * 100)
    if new_pitch < label:
        return None
    new_margin = round(margin - (c0 + 0.5 * c1) * 100)
    return (
        None if new_pitch == pitch else new_pitch,
        None if new_margin == margin else new_margin,
    )


def _options(raw: Axis | None, pitch: int, margin: int) -> list[Axis]:
    """What one axis may propose, best first: the changes of at least ``MIN_CHANGE_HMM``, then
    both changes together (a small change the other one needs to stay on the page), then none."""
    if raw is None:
        return [(None, None)]
    thresholded: Axis = (
        raw[0] if raw[0] is not None and abs(raw[0] - pitch) >= MIN_CHANGE_HMM else None,
        raw[1] if raw[1] is not None and abs(raw[1] - margin) >= MIN_CHANGE_HMM else None,
    )
    out = [thresholded]
    if thresholded != (None, None) and raw != thresholded:
        out.append(raw)
    if (None, None) not in out:
        out.append((None, None))
    return out


def _fits(g: Geometry, x: Axis, y: Axis) -> bool:
    candidate = replace(
        g,
        pitch_x=x[0] if x[0] is not None else g.pitch_x,
        margin_left=x[1] if x[1] is not None else g.margin_left,
        pitch_y=y[0] if y[0] is not None else g.pitch_y,
        margin_top=y[1] if y[1] is not None else g.margin_top,
    )
    try:
        validate_geometry(normalise_shape(candidate), on_write=True)
    except SheetApiError:
        return False
    return True


def suggest_geometry(
    g: Geometry,
    readings: Sequence[Reading],
    *,
    correction_type_version_id: UUID | None,
    run_type_version_id: UUID,
) -> GeometrySuggestion | None:
    """The geometry the check readings point at, or ``None`` when the correction was measured on
    the very version the check page was printed for (its geometry error is already in it)."""
    if correction_type_version_id == run_type_version_id:
        return None
    cols = [float(r.col_gap) for r in readings]
    rows = [float(r.row_gap) for r in readings]
    across = [r.across_mm for r in readings]
    down = [r.down_mm for r in readings]
    x_line, y_line = _line(cols, across), _line(rows, down)

    def detrended(line: tuple[float, float] | None, t: float, p: float) -> float:
        return 0.0 if line is None else p - (line[0] + line[1] * t)

    uneven = any(
        math.hypot(detrended(x_line, c, a), detrended(y_line, r, d)) > RESIDUAL_FLAG_MM
        for c, r, a, d in zip(cols, rows, across, down, strict=True)
    )
    # Every proposal is a geometry a version may have, checked as a whole: an axis is filtered
    # for small changes, kept with both changes when one needs the other, or left out.
    x_options = _options(_raw_axis(x_line, g.pitch_x, g.margin_left, g.label_w),
                         g.pitch_x, g.margin_left)  # fmt: skip
    y_options = _options(_raw_axis(y_line, g.pitch_y, g.margin_top, g.label_h),
                         g.pitch_y, g.margin_top)  # fmt: skip
    (pitch_x, margin_left), (pitch_y, margin_top) = next(
        ((x, y) for x in x_options for y in y_options if _fits(g, x, y)),
        ((None, None), (None, None)),
    )
    return GeometrySuggestion(
        pitch_x_hmm=pitch_x,
        pitch_y_hmm=pitch_y,
        margin_left_hmm=margin_left,
        margin_top_hmm=margin_top,
        uneven=uneven,
    )


async def _correction_entry(
    session: AsyncSession, run: CalibrationRun
) -> CalibrationHistory | None:
    """The history entry the check page was printed with.

    The job's payload names it (``calibration_history_id``, design 7.5); once the job was cleaned
    up, the tray's newest entry written before the run stands in, if its values are the run's.
    """
    job = await session.get(Job, run.job_id)
    raw = job.payload.get("calibration_history_id") if job is not None else None
    if isinstance(raw, str):
        try:
            entry = await session.get(CalibrationHistory, UUID(raw))
        except ValueError:
            entry = None
        if entry is not None:
            return entry
    entry = (
        await session.execute(
            select(CalibrationHistory)
            .where(
                col(CalibrationHistory.printer_id) == run.printer_id,
                col(CalibrationHistory.tray) == run.tray,
                col(CalibrationHistory.created_at) <= run.created_at,
            )
            .order_by(col(CalibrationHistory.seq).desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    # Only an entry with the values the page was printed with: a save between the page's
    # snapshot and its run, or a pruned entry, leaves the origin unknown.
    return entry if entry is not None and _same_values(entry, run.correction or {}) else None


def _same_values(entry: CalibrationHistory, snap: dict[str, object]) -> bool:
    return (
        entry.offset_x_hmm,
        entry.offset_y_hmm,
        entry.scale_x_ppm,
        entry.scale_y_ppm,
        entry.rotation_udeg,
    ) == (
        snap.get("dx_hmm"),
        snap.get("dy_hmm"),
        snap.get("sx_ppm"),
        snap.get("sy_ppm"),
        snap.get("rotation_udeg"),
    )


async def _type_id(session: AsyncSession, version_id: UUID | None) -> UUID | None:
    if version_id is None:
        return None
    version = await session.get(SheetTypeVersion, version_id)
    return version.type_id if version is not None else None


async def unavailable_reason(
    session: AsyncSession, run: CalibrationRun
) -> tuple[SuggestionUnavailable | None, UUID | None]:
    """Why ``run`` can give no suggestion (``None`` when it can), and the sheet type version its
    correction was measured on: ``no_check_run`` for a calibration or probe page and for a check
    page printed without any correction (nothing was compensated), ``same_type`` when the
    correction was measured on the run's sheet type, in any version, ``unknown_correction`` when
    the sheet type it was measured on is not known (typed in by hand, its history entry or
    version gone since, or a save between the page's snapshot and its run)."""
    correction = run.correction or {}
    if run.kind != "check_page" or correction.get("method", "none") == "none":
        return "no_check_run", None
    entry = await _correction_entry(session, run)
    measured_on = entry.measured_on_type_version_id if entry is not None else None
    measured_type = await _type_id(session, measured_on)
    if measured_type is None:
        # Without a known origin the correction may hold this type's own geometry error.
        return "unknown_correction", None
    if measured_type == await _type_id(session, run.type_version_id):
        return "same_type", measured_on
    return None, measured_on


async def suggestion_for_run(
    session: AsyncSession, run: CalibrationRun, g: Geometry, readings: Sequence[Reading]
) -> tuple[GeometrySuggestion | None, SuggestionUnavailable | None]:
    """The suggestion for the readings of ``run`` (printed on geometry ``g``), or why there is
    none (``unavailable_reason``)."""
    reason, measured_on = await unavailable_reason(session, run)
    if reason is not None:
        return None, reason
    # Another sheet type, so another version: never None here.
    return (
        suggest_geometry(
            g, readings, correction_type_version_id=measured_on,
            run_type_version_id=run.type_version_id,
        ),
        None,
    )  # fmt: skip


# ---------------------------------------------------------------------------- apply


class RunNotForThisTypeError(SheetApiError):
    status = 422
    error_code = "run_not_for_this_type"
    message = "The run was not printed for this sheet type."


class RunNotACheckPrintError(SheetApiError):
    status = 422
    error_code = "run_not_a_check_print"
    message = "A geometry suggestion comes from a check print; this run is another page."


class SuggestionUnavailableError(SheetApiError):
    """The run gives no suggestion, so its values cannot be applied as one (review M3);
    ``error_detail.reason`` is the fit's ``suggestion_unavailable``."""

    status = 422
    error_code = "suggestion_unavailable"
    message = "This check print gives no geometry suggestion."

    def __init__(self, reason: str) -> None:
        super().__init__(detail={"reason": reason})


class RunVersionNotCurrentError(SheetApiError):
    """The suggestion is relative to the run's version; on top of a newer one it would undo that
    version's changes."""

    status = 409
    error_code = "run_version_not_current"
    message = "The run was printed for an older version of this sheet type."


#: The fields "apply as new version" may change, by the name of the ``Geometry`` field.
SUGGESTION_FIELDS = ("pitch_x", "pitch_y", "margin_left", "margin_top")


async def apply_suggestion(
    session: AsyncSession,
    key: str,
    run_id: UUID,
    values_hmm: dict[str, int],
    *,
    actor: str,
) -> SheetTypeVersion:
    """Add a version of ``key`` with the run's version geometry and ``values_hmm`` in place.

    The run must be a check print of this type's newest version that can give a suggestion
    (``unavailable_reason``). The change note names the run
    by its number only: a version note travels into exports and the feedback issue, a printer or
    tray name must not.
    """
    async with write_transaction(session):
        t = await ts.get_type(session, key)
        run = await session.get(CalibrationRun, run_id, populate_existing=True)
        version = (
            await session.get(SheetTypeVersion, run.type_version_id) if run is not None else None
        )
        if run is None or version is None or version.type_id != t.id:
            raise RunNotForThisTypeError()
        if run.kind != "check_page":
            raise RunNotACheckPrintError()
        if (await ts.current_version(session, t.id)).id != version.id:
            raise RunVersionNotCurrentError()
        reason, _ = await unavailable_reason(session, run)
        if reason is not None:
            raise SuggestionUnavailableError(reason)
        base = ts.version_geometry(version)
        geometry = replace(
            base,
            pitch_x=values_hmm.get("pitch_x", base.pitch_x),
            pitch_y=values_hmm.get("pitch_y", base.pitch_y),
            margin_left=values_hmm.get("margin_left", base.margin_left),
            margin_top=values_hmm.get("margin_top", base.margin_top),
        )
        v = await ts.add_version_locked(
            session,
            key,
            geometry,
            actor=actor,
            change_note=f"Pitch and margins from the check print of run #{run.run_no}",
        )
        await session.commit()
    return v
