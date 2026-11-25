# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Storage of the manual calibration per page printer and tray.

The row stores the printer's deviation around the page centre; the hub applies the inverse
when it renders (D17).
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import and_, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.calibration import history
from app.calibration.fit import Reading, fit
from app.calibration.model import (
    NOTE_MAX,
    CalibrationIn,
    CalibrationRead,
    MeasuredOn,
    deviation_to_values,
    to_deviation,
)
from app.models.calibration_run import CalibrationHistory, CalibrationRun
from app.models.printer import Printer, is_page_printer
from app.models.printer_calibration import PrinterCalibration
from app.models.printer_tray import PrinterTray
from app.models.sheet import SheetType, SheetTypeVersion
from app.page_render.transform import Deviation
from app.repositories.write_lock import write_transaction
from app.services.page_jobs import CalibrationSnapshot
from app.sheets import type_service as ts
from app.sheets.errors import (
    InvalidFieldError,
    MediaKindMismatchError,
    PrinterNotFoundError,
    SheetApiError,
    TrayUnknownError,
)


async def _page_printer(session: AsyncSession, printer_id: UUID) -> Printer:
    printer = await session.get(Printer, printer_id)
    if printer is None:
        raise PrinterNotFoundError()
    if not is_page_printer(printer):
        raise MediaKindMismatchError()
    return printer


async def page_printer(session: AsyncSession, printer_id: UUID) -> Printer:
    """The page printer ``printer_id``; ``printer_not_found`` or ``media_kind_mismatch``."""
    return await _page_printer(session, printer_id)


async def _enabled_trays(session: AsyncSession, printer_id: UUID) -> list[PrinterTray]:
    rows = await session.execute(
        select(PrinterTray)
        .where(col(PrinterTray.printer_id) == printer_id, col(PrinterTray.enabled).is_(True))
        .order_by(PrinterTray.tray_key)
    )
    return list(rows.scalars())


async def resolve_tray(session: AsyncSession, printer_id: UUID, tray: str | None) -> str:
    """The tray a sheet print uses: the one named, else the printer's default tray.

    Both must be enabled; otherwise ``tray_unknown``. A printer without an enabled default
    tray has none to fall back to (pre-flight C12).
    """
    enabled = await _enabled_trays(session, printer_id)
    if tray is not None:
        if tray not in {t.tray_key for t in enabled}:
            raise TrayUnknownError([])  # C19: never echo the caller's key
        return tray
    default = next((t.tray_key for t in enabled if t.is_default), None)
    if default is None:
        raise TrayUnknownError([])
    return default


async def _check_tray(session: AsyncSession, printer_id: UUID, tray: str) -> None:
    if tray not in {t.tray_key for t in await _enabled_trays(session, printer_id)}:
        raise TrayUnknownError([])  # C19: never echo the caller's key


async def check_tray(session: AsyncSession, printer_id: UUID, tray: str) -> None:
    """``tray_unknown`` unless ``tray`` is an enabled tray of the printer."""
    await _check_tray(session, printer_id, tray)


async def _measured_on(session: AsyncSession, version_id: UUID | None) -> MeasuredOn | None:
    if version_id is None:
        return None
    found = (
        await session.execute(
            select(col(SheetType.key), col(SheetTypeVersion.version))
            .join(SheetTypeVersion, col(SheetTypeVersion.type_id) == col(SheetType.id))
            .where(col(SheetTypeVersion.id) == version_id)
        )
    ).first()
    return MeasuredOn(type_key=found[0], version=found[1]) if found else None


async def read_row(
    session: AsyncSession, tray: str, row: PrinterCalibration | None
) -> CalibrationRead:
    if row is None:
        return CalibrationRead(tray=tray)
    v = deviation_to_values(to_deviation(row))
    # SQLite hands back naive datetimes; every stored time is UTC.
    updated_at = row.updated_at if row.updated_at.tzinfo else row.updated_at.replace(tzinfo=UTC)
    return CalibrationRead(
        tray=tray,
        method=row.method,
        offset_x_mm=v.offset_x_mm,
        offset_y_mm=v.offset_y_mm,
        scale_x=v.scale_x,
        scale_y=v.scale_y,
        rotation_deg=v.rotation_deg,
        measured_on=await _measured_on(session, row.measured_on_type_version_id),
        updated_at=updated_at,
        updated_by=row.updated_by,
    )


async def get_all(session: AsyncSession, printer_id: UUID) -> list[CalibrationRead]:
    """One entry per enabled tray; a tray without a row has ``None`` values (identity)."""
    await _page_printer(session, printer_id)
    rows = {
        r.tray_key: r
        for r in (
            await session.execute(
                select(PrinterCalibration).where(col(PrinterCalibration.printer_id) == printer_id)
            )
        ).scalars()
    }
    return [
        await read_row(session, t.tray_key, rows.get(t.tray_key))
        for t in await _enabled_trays(session, printer_id)
    ]


def row_deviation(row: PrinterCalibration | None) -> Deviation | None:
    """The stored deviation of a tray; ``None`` without a row."""
    return None if row is None else to_deviation(row)


def write_row(
    session: AsyncSession,
    row: PrinterCalibration | None,
    printer_id: UUID,
    tray: str,
    dev: Deviation,
    *,
    version_id: UUID | None,
    actor: str,
) -> PrinterCalibration:
    """Set the tray's row to ``dev`` inside the caller's write transaction (no commit)."""
    if row is None:
        row = PrinterCalibration(
            printer_id=printer_id,
            tray_key=tray,
            method="manual",
            offset_x_hmm=0,
            offset_y_hmm=0,
            scale_x_ppm=1_000_000,
            scale_y_ppm=1_000_000,
            rotation_udeg=0,
        )
        session.add(row)
    row.method = "manual"  # D17: the row is the printer's deviation, not the correction
    row.offset_x_hmm = dev.offset_x_hmm  # D16
    row.offset_y_hmm = dev.offset_y_hmm  # D16
    row.scale_x_ppm = dev.scale_x_ppm
    row.scale_y_ppm = dev.scale_y_ppm
    row.rotation_udeg = dev.rotation_udeg
    row.measured_on_type_version_id = version_id
    row.updated_at = datetime.now(UTC)
    row.updated_by = actor
    return row


# ---------------------------------------------------------------------------- the server fit

CLIENT_FIT_TOLERANCE_HMM: Final = 5
"""A sent value further than this (0.05 mm) from the hub's fit is replaced (design 7.5)."""


def _linear(dev: Deviation, u: float, v: float) -> tuple[float, float]:
    """Scale and rotation of ``dev`` applied to ``(u, v)`` around the page centre (as the fit)."""
    theta = math.radians(dev.rotation_udeg / 1_000_000)
    sx, sy = dev.scale_x_ppm / 1_000_000, dev.scale_y_ppm / 1_000_000
    return (
        math.cos(theta) * sx * u - math.sin(theta) * sy * v,
        math.sin(theta) * sx * u + math.cos(theta) * sy * v,
    )


def displacement_at_far_corner(
    dev_a: Deviation, dev_b: Deviation, page_w_hmm: int, page_h_hmm: int
) -> int:
    """How far apart (hundredths of a mm) the scale and rotation of two deviations put the page
    corners farthest from the centre; the offsets are left out. The largest of the four."""
    hw, hh = page_w_hmm / 200, page_h_hmm / 200
    worst = 0.0
    for u, v in ((hw, hh), (-hw, hh), (hw, -hh), (-hw, -hh)):
        ax, ay = _linear(dev_a, u, v)
        bx, by = _linear(dev_b, u, v)
        worst = max(worst, math.hypot(ax - bx, ay - by))
    return math.floor(worst * 100 + 0.5)  # half up, not to even


def ignored_fields(
    sent: Deviation, fitted: Deviation, page_w_hmm: int, page_h_hmm: int
) -> list[str]:
    """The API names of the sent values further than the tolerance from the fit: offsets
    directly, scale and rotation as the displacement each alone causes at the page corner."""
    out: list[str] = []
    if abs(sent.offset_x_hmm - fitted.offset_x_hmm) > CLIENT_FIT_TOLERANCE_HMM:
        out.append("offset_x_mm")
    if abs(sent.offset_y_hmm - fitted.offset_y_hmm) > CLIENT_FIT_TOLERANCE_HMM:
        out.append("offset_y_mm")
    for name, changed in (
        ("scale_x", dataclasses.replace(fitted, scale_x_ppm=sent.scale_x_ppm)),
        ("scale_y", dataclasses.replace(fitted, scale_y_ppm=sent.scale_y_ppm)),
        ("rotation_deg", dataclasses.replace(fitted, rotation_udeg=sent.rotation_udeg)),
    ):
        moved = displacement_at_far_corner(changed, fitted, page_w_hmm, page_h_hmm)
        if moved > CLIENT_FIT_TOLERANCE_HMM:
            out.append(name)
    return out


class RunNotForThisTrayError(SheetApiError):
    status = 422
    error_code = "run_not_for_this_tray"
    message = "The run does not belong to this printer and tray."


class RunVersionMismatchError(SheetApiError):
    """The run's page was printed for another sheet type version than ``measured_on`` names: the
    entry would name a run and a version that disagree (task review F7)."""

    status = 422
    error_code = "run_version_mismatch"
    message = "The run was printed for another sheet type version than measured_on."


async def check_run(
    session: AsyncSession,
    run_id: UUID,
    printer_id: UUID,
    tray: str,
    version: SheetTypeVersion | None,
) -> CalibrationRun:
    """The run ``run_id`` of this printer and tray, printed for ``version`` when one is named; the
    save and the fit both check a run this way (``run_not_for_this_tray``,
    ``run_version_mismatch``). Read fresh: a run from ``submit_with_run`` is detached."""
    run = await session.get(CalibrationRun, run_id, populate_existing=True)
    if run is None or run.printer_id != printer_id or run.tray != tray:
        raise RunNotForThisTrayError()
    if version is not None and run.type_version_id != version.id:
        raise RunVersionMismatchError()
    return run


async def put(
    session: AsyncSession,
    printer_id: UUID,
    tray: str,
    values: CalibrationIn,
    *,
    measured_on: tuple[str, int] | None,
    actor: str,
    run_id: UUID | None = None,
    readings: Sequence[Reading] | None = None,
    note: str | None = None,
) -> CalibrationRead:
    """Store (replace) the calibration of a tray and write its history entry, in one transaction.

    With ``readings`` the stored values are the hub's fit of them on the ``measured_on`` version
    (design 7.5); a sent value outside ``CLIENT_FIT_TOLERANCE_HMM`` of the fit is named in
    ``ignored_fields``. Without readings the values are stored as sent. Nothing is stored on any
    error.
    """
    await _page_printer(session, printer_id)
    # Path before body, the order fit and page use: tray, the measured_on type, then the ranges.
    await _check_tray(session, printer_id, tray)
    version = await ts.get_version(session, measured_on[0], measured_on[1]) if measured_on else None
    if run_id is not None:
        await check_run(session, run_id, printer_id, tray, version)
    dev = to_deviation(values)  # raises calibration_out_of_range before any write
    clean_note = None if note is None else ts.clean_text("note", note, NOTE_MAX) or None
    source: Literal["manual", "fit"] = "manual"
    stored = dev
    ignored: list[str] = []
    stored_readings: list[dict[str, Any]] | None = None
    residuals: list[float] | None = None
    if readings is not None:
        if version is None:
            raise InvalidFieldError("measured_on", "required with readings")
        geometry = ts.version_geometry(version)
        result = fit(geometry, geometry.page_size, list(readings))
        stored = to_deviation(deviation_to_values(result.dev))  # the fit's ranges are checked too
        ignored = ignored_fields(dev, stored, geometry.page_w, geometry.page_h)
        source = "fit"
        stored_readings = [dataclasses.asdict(r) for r in readings]
        residuals = [round(r, 4) for r in result.residuals_mm]
    async with write_transaction(session):
        await _check_tray(session, printer_id, tray)  # inside the lock: no race with a tray edit
        row = await session.get(PrinterCalibration, (printer_id, tray), populate_existing=True)
        previous = row_deviation(row)
        row = write_row(
            session,
            row,
            printer_id,
            tray,
            stored,
            version_id=version.id if version else None,
            actor=actor,
        )
        await history.record(
            session,
            printer_id=printer_id,
            tray=tray,
            action="save",
            new=stored,
            previous=previous,
            method=row.method,
            measured_on_version_id=version.id if version else None,
            run_id=run_id,
            readings=stored_readings,
            residuals=residuals,
            reverted_from_id=None,
            actor=actor,
            note=clean_note,
            source=source,
        )
        await session.commit()
    read = await read_row(session, tray, row)
    if ignored:
        return read.model_copy(
            update={"notes": ["client_values_ignored"], "ignored_fields": ignored}
        )
    return read


async def delete(session: AsyncSession, printer_id: UUID, tray: str, *, actor: str) -> None:
    """Reset a tray to the identity; a stored row is removed even if its tray is gone.

    A removed row writes a ``reset`` history entry; a tray without a row has nothing to reset.
    """
    await _page_printer(session, printer_id)
    async with write_transaction(session):
        row = await session.get(PrinterCalibration, (printer_id, tray), populate_existing=True)
        if row is None:
            # Nothing to clear: an unknown tray is still an error, a known one is a no-op.
            await _check_tray(session, printer_id, tray)
        else:
            # A stored row is always clearable, even when its tray was disabled or removed since.
            previous = row_deviation(row)
            await session.delete(row)
            await history.record(
                session,
                printer_id=printer_id,
                tray=tray,
                action="reset",
                new=None,
                previous=previous,
                method=None,
                measured_on_version_id=None,
                run_id=None,
                readings=None,
                residuals=None,
                reverted_from_id=None,
                actor=actor,
                note=None,
                source=None,
            )
        await session.commit()


async def snapshot(session: AsyncSession, printer_id: UUID, tray: str) -> CalibrationSnapshot:
    """The calibration a page job records; a tray without one is the identity."""
    await _page_printer(session, printer_id)
    await _check_tray(session, printer_id, tray)
    row = await session.get(PrinterCalibration, (printer_id, tray))
    if row is None:
        return CalibrationSnapshot(method="none")
    return CalibrationSnapshot(
        method=row.method,
        dx_hmm=row.offset_x_hmm,
        dy_hmm=row.offset_y_hmm,
        sx_ppm=row.scale_x_ppm,
        sy_ppm=row.scale_y_ppm,
        rotation_udeg=row.rotation_udeg,
    )


async def snapshot_with_history(
    session: AsyncSession, printer_id: UUID, tray: str
) -> tuple[CalibrationSnapshot, UUID | None]:
    """The snapshot and the history entry it stands for (``PagePayload.calibration_history_id``,
    design 7.5, S7); ``None`` for a tray without any entry.

    One SELECT reads both, so a save committed meanwhile cannot pair the old values with the new
    entry's id (SQLite answers a statement from one snapshot; review on #442).
    """
    await _page_printer(session, printer_id)
    await _check_tray(session, printer_id, tray)
    latest = (
        select(col(CalibrationHistory.id))
        .where(
            col(CalibrationHistory.printer_id) == printer_id, col(CalibrationHistory.tray) == tray
        )
        .order_by(col(CalibrationHistory.seq).desc())
        .limit(1)
        .scalar_subquery()
    )
    one = select(literal(1).label("one")).subquery()
    found = (
        await session.execute(
            select(
                latest.label("history_id"),
                col(PrinterCalibration.method),
                col(PrinterCalibration.offset_x_hmm),
                col(PrinterCalibration.offset_y_hmm),
                col(PrinterCalibration.scale_x_ppm),
                col(PrinterCalibration.scale_y_ppm),
                col(PrinterCalibration.rotation_udeg),
            )
            .select_from(one)
            .outerjoin(
                PrinterCalibration,
                and_(
                    col(PrinterCalibration.printer_id) == printer_id,
                    col(PrinterCalibration.tray_key) == tray,
                ),
            )
        )
    ).one()
    history_id: UUID | None = found[0]
    method: str | None = found[1]
    dx, dy, sx, sy, rot = found[2:]
    if method is None:  # no row: the outer join gave nulls
        return CalibrationSnapshot(method="none"), history_id
    snap = CalibrationSnapshot(
        method=method, dx_hmm=dx, dy_hmm=dy, sx_ppm=sx, sy_ppm=sy, rotation_udeg=rot
    )
    return snap, history_id
