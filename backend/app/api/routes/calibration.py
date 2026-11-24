# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Calibration API for page printers (spec 15, 18).

Reads and the fit need ``read`` scope, writes need ``admin``; every route also checks the
key's printer restriction (``403 printer_not_allowed`` for another printer's calibration).
"""

from __future__ import annotations

import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.identity_hold import ensure_not_held
from app.api.routes.sheet_common import actor, mapped
from app.auth.dependencies import AuthContext, check_printer_access
from app.auth.scope_deps import require_admin, require_print, require_read
from app.calibration import page, runs, scale, service
from app.calibration.fit import Reading, fit
from app.calibration.model import (
    CalibrationIn,
    CalibrationPut,
    CalibrationRead,
    CalibrationScale,
    deviation_to_values,
)
from app.calibration.runs import CalibrationRunRead
from app.db.session import get_session
from app.repositories import printers as printers_repo
from app.schemas.problem import ProblemDetail
from app.services import render_slots
from app.services.page_jobs import PagePayload
from app.settings_store.registry import SETTINGS
from app.sheets import type_service as ts
from app.sheets.errors import PrinterNotFoundError
from app.sheets.print_service import RenderTimeoutApiError, RenderUnavailableApiError

router = APIRouter(prefix="/api/v1/printers", tags=["calibration"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ReadDep = Annotated[AuthContext, Depends(require_read)]
AdminDep = Annotated[AuthContext, Depends(require_admin)]
PrintDep = Annotated[AuthContext, Depends(require_print)]
PrinterPath = Annotated[UUID, Path(description="Printer id (a page printer).")]
TrayPath = Annotated[str, Path(min_length=1, max_length=100, description="Tray key.")]


class ReadingIn(BaseModel):
    col_gap: int = Field(ge=1, le=1000, description="Vertical gap S1, S2, ... counted from 1.")
    row_gap: int = Field(ge=1, le=1000, description="Horizontal gap Z1, Z2, ... counted from 1.")
    across_mm: float = Field(
        ge=-20, le=20, allow_inf_nan=False, description="Ink offset to the right, mm."
    )
    down_mm: float = Field(
        ge=-20, le=20, allow_inf_nan=False, description="Ink offset downwards, mm."
    )


class FitRequest(BaseModel):
    type: str = Field(min_length=1, max_length=200, description="Sheet type key.")
    version: int | None = Field(default=None, ge=1, description="Defaults to the current version.")
    readings: list[ReadingIn] = Field(min_length=3, max_length=200)


class FitResponse(BaseModel):
    values: CalibrationIn
    residuals_mm: list[float]
    flagged: list[int] = Field(
        description="0-based indices into `readings` of those further than 0.3 mm from the fit."
    )
    out_of_range: list[str] = Field(description="Names of values outside the allowed ranges.")


@router.get(
    "/calibration/scale",
    response_model=CalibrationScale,
    summary="The mini scale drawn at every crossing of the calibration page",
)
async def get_calibration_scale(_auth: ReadDep) -> CalibrationScale:
    return CalibrationScale(
        half_mm=scale.HALF_MM,
        step_mm=scale.STEP_MM,
        line_mm=scale.LINE_MM,
        tick_half_mm=scale.TICK_HALF_MM,
        tick_whole_mm=scale.TICK_WHOLE_MM,
        tick_centre_mm=scale.TICK_CENTRE_MM,
    )


@router.get(
    "/{printer_id}/calibration",
    response_model=list[CalibrationRead],
    summary="Calibration of every enabled tray",
)
async def get_calibration(
    printer_id: PrinterPath, session: SessionDep, auth: ReadDep
) -> list[CalibrationRead]:
    check_printer_access(auth, printer_id)
    with mapped():
        return await service.get_all(session, printer_id)


@router.put(
    "/{printer_id}/calibration/{tray}",
    response_model=CalibrationRead,
    summary="Set the manual calibration of a tray",
)
async def put_calibration(
    printer_id: PrinterPath,
    tray: TrayPath,
    body: CalibrationPut,
    session: SessionDep,
    auth: AdminDep,
) -> CalibrationRead:
    check_printer_access(auth, printer_id)
    measured = (body.measured_on.type_key, body.measured_on.version) if body.measured_on else None
    with mapped():
        return await service.put(
            session,
            printer_id,
            tray,
            CalibrationIn(**body.model_dump(exclude={"measured_on"})),
            measured_on=measured,
            actor=actor(auth),
        )


@router.delete(
    "/{printer_id}/calibration/{tray}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Reset a tray to no calibration",
)
async def delete_calibration(
    printer_id: PrinterPath, tray: TrayPath, session: SessionDep, auth: AdminDep
) -> Response:
    # No-op here: deleting needs the admin scope, and an admin key is never restricted (#283).
    check_printer_access(auth, printer_id)
    with mapped():
        await service.delete(session, printer_id, tray, actor=actor(auth))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{printer_id}/calibration/{tray}/fit",
    response_model=FitResponse,
    summary="Fit calibration values from measured readings (nothing is saved)",
)
async def fit_calibration(
    printer_id: PrinterPath,
    tray: TrayPath,
    body: FitRequest,
    session: SessionDep,
    auth: ReadDep,
) -> FitResponse:
    check_printer_access(auth, printer_id)
    with mapped():
        await service.snapshot(session, printer_id, tray)  # printer kind and tray must be valid
        if body.version is None:
            version = await ts.current_version(session, (await ts.get_type(session, body.type)).id)
        else:
            version = await ts.get_version(session, body.type, body.version)
        geometry = ts.version_geometry(version)
        result = fit(
            geometry,
            geometry.page_size,
            [Reading(r.col_gap, r.row_gap, r.across_mm, r.down_mm) for r in body.readings],
        )
    return FitResponse(
        values=deviation_to_values(result.dev),
        residuals_mm=result.residuals_mm,
        flagged=result.flagged,
        out_of_range=result.out_of_range,
    )


class PageRequest(BaseModel):
    type: str = Field(min_length=1, max_length=200, description="Sheet type key.")
    version: int | None = Field(default=None, ge=1, description="Defaults to the current version.")
    corrected: bool = Field(
        default=False,
        description="Print with the tray's stored correction, to check it (not to measure).",
    )


class PageJobAccepted(BaseModel):
    job_id: UUID


_PAGE_UNPROCESSABLE = {
    "description": (
        "The sheet type has no crossings to measure (`calibration_type_unsuitable`), or its "
        "grid has more than 2500 cells (`calibration_grid_too_large`, with `cells` and "
        "`max_cells`). Other errors of the type, printer and tray lookups carry their own codes."
    )
}
_DOWNLOAD_RETRY_AFTER_S = 1


@router.post(
    "/{printer_id}/calibration/{tray}/page",
    response_model=PageJobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: _PAGE_UNPROCESSABLE,
        status.HTTP_409_CONFLICT: {
            "description": (
                "The printer is a tape printer (`media_kind_mismatch`), is not known to the "
                "print queue (`printer-unavailable`), or the device behind it changed "
                "(`printer-identity-mismatch`)."
            )
        },
    },
    summary="Print the calibration page (no sheet is created)",
)
async def print_calibration_page(
    request: Request,
    printer_id: PrinterPath,
    tray: TrayPath,
    body: PageRequest,
    session: SessionDep,
    auth: PrintDep,
) -> PageJobAccepted:
    check_printer_access(auth, printer_id)
    ensure_not_held(getattr(request.app.state, "print_queue", None), printer_id)  # #231
    with mapped():
        payload = await page.build_payload(
            session, printer_id, tray, type_key=body.type, version=body.version,
            corrected=body.corrected,
        )  # fmt: skip
        queue = getattr(request.app.state, "print_queue", None)
        if queue is None:
            raise _printer_unavailable(printer_id)
        version = await ts.get_version(
            session, payload.detail["type_key"], payload.detail["version"]
        )

        async def submit(stamped: PagePayload, job_id: UUID) -> UUID:
            new_id: UUID = await queue.submit_page_job(
                printer_id, stamped, api_key_id=auth.api_key_id, source_ip=auth.ip, job_id=job_id
            )
            return new_id

        # #336: the run (and its number) is committed before the job; a failed submit deletes
        # it again and leaves a gap in the numbers (ADR 0029).
        try:
            _run, job_id = await runs.submit_with_run(
                session,
                payload,
                printer_id=printer_id,
                kind="check_page" if body.corrected else "calibration_page",
                type_version_id=version.id,
                actor=actor(auth),
                submit=submit,
                time_zone=str(_setting(request, "general.time_zone")),
            )
        except KeyError:
            raise _printer_unavailable(printer_id) from None
    return PageJobAccepted(job_id=job_id)


@router.get(
    "/{printer_id}/calibration/{tray}/runs",
    response_model=list[CalibrationRunRead],
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "The key may not use this printer (`printer_not_allowed`)."
        },
        status.HTTP_404_NOT_FOUND: {"description": "No such printer (`printer_not_found`)."},
    },
    summary="Calibration, check and probe pages printed on a tray, newest first",
)
async def list_calibration_runs(
    request: Request,
    printer_id: PrinterPath,
    tray: TrayPath,
    session: SessionDep,
    auth: ReadDep,
) -> list[CalibrationRunRead]:
    """Every run of the tray (at most 1000 are kept) and every retry of a run's job.

    The tray is not checked against the printer's current trays: a tray the printer renamed keeps
    its runs under the old key. ``actual_media_source`` is the printer's own text, bounded.
    """
    check_printer_access(auth, printer_id)
    if await printers_repo.get(session, printer_id) is None:
        raise PrinterNotFoundError().to_http()
    return await runs.list_runs(
        session, printer_id, tray, retention_days=_job_retention_days(request)
    )


def _setting(request: Request, key: str) -> object:
    store = getattr(request.app.state, "settings_store", None)
    return store.get(key) if store is not None else SETTINGS[key].default


def _job_retention_days(request: Request) -> int:
    return int(str(_setting(request, "jobs.retention_days")))


@router.get(
    "/{printer_id}/calibration/{tray}/page/pdf",
    response_class=Response,
    responses={
        200: {"content": {"application/pdf": {}}, "description": "The calibration page."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: _PAGE_UNPROCESSABLE,
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": (
                "A render of the key is stuck past the time limit (`too-many-previews`). The "
                "body has the rate limit's shape (`retry_after_seconds`)."
            )
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": (
                "No render slot became free in time (`render-unavailable`): every slot is held "
                "by a stuck render, or the wait for the caller's turn reached "
                "`LABELYARD_RENDER_TIMEOUT_S`."
            )
        },
        status.HTTP_504_GATEWAY_TIMEOUT: {
            "description": (
                "The page took longer than `LABELYARD_RENDER_TIMEOUT_S` to draw "
                "(`render-timeout`). The drawing goes on: the key's next download answers 429 "
                "until it has ended."
            )
        },
    },
    summary="Download the calibration page as a PDF",
)
async def download_calibration_page(
    printer_id: PrinterPath,
    tray: TrayPath,
    session: SessionDep,
    auth: ReadDep,
    type_key: Annotated[
        str, Query(alias="type", min_length=1, max_length=200, description="Sheet type key.")
    ],
    version: Annotated[
        int | None, Query(ge=1, description="Defaults to the current version.")
    ] = None,
    corrected: bool = False,
) -> Response:
    check_printer_access(auth, printer_id)
    with mapped():
        payload = await page.build_payload(
            session, printer_id, tray, type_key=type_key, version=version, corrected=corrected
        )
    # No read transaction stays open while the page draws (#405), as in the other bounded renders.
    await session.commit()
    # What the job renders, under the process-wide render slots like the sheet preview (#358).
    try:
        pdf = await render_slots.render_bounded(
            page.render_calibration_page,
            payload,
            label=f"calibration:{printer_id}",
            owner=render_slots.owner_of(auth.api_key_id, auth.session_id),
        )
    except render_slots.RenderTimeoutError:
        raise RenderTimeoutApiError().to_http() from None
    except render_slots.RenderUnavailableError:
        raise RenderUnavailableApiError().to_http() from None
    except render_slots.OwnerRenderStuckError:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error_code": "too-many-previews",
                "error_message": (
                    "A render of this key is in progress. Retry after "
                    f"{_DOWNLOAD_RETRY_AFTER_S} seconds."
                ),
                "retry_after_seconds": _DOWNLOAD_RETRY_AFTER_S,
            },
            headers={"Retry-After": str(_DOWNLOAD_RETRY_AFTER_S)},
        ) from None
    name = re.sub(r"[^A-Za-z0-9._-]", "_", f"calibration-{payload.detail['type_key']}")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{name}-v{payload.detail["version"]}.pdf"'
        },
    )


def _printer_unavailable(printer_id: UUID) -> HTTPException:
    problem = ProblemDetail(
        type="printer-unavailable",
        title="Printer is not available to the print queue",
        status=status.HTTP_409_CONFLICT,
        detail=f"Printer {printer_id} is not known to the print queue",
    )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail=problem.model_dump(exclude_none=True)
    )
