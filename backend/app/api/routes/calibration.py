# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Calibration API for page printers (spec 15, 18).

Reads and the fit need ``read`` scope, writes need ``admin``; every route also checks the
key's printer restriction (``403 printer_not_allowed`` for another printer's calibration).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.identity_hold import ensure_not_held
from app.api.pagination import next_link
from app.api.routes.sheet_common import actor, mapped
from app.auth.dependencies import AuthContext, check_printer_access
from app.auth.scope_deps import require_admin, require_print, require_read
from app.calibration import history, page, runs, scale, service, suggest
from app.calibration.fit import Reading, fit
from app.calibration.history import CalibrationHistoryRead
from app.calibration.layout import MarginText
from app.calibration.model import (
    READINGS_MAX,
    CalibrationIn,
    CalibrationPut,
    CalibrationRead,
    CalibrationScale,
    ReadingIn,
    RestoreRequest,
    deviation_to_values,
)
from app.calibration.runs import CalibrationRunRead
from app.calibration.suggest import GeometrySuggestion, SuggestionUnavailable
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


class FitRequest(BaseModel):
    type: str = Field(min_length=1, max_length=200, description="Sheet type key.")
    version: int | None = Field(default=None, ge=1, description="Defaults to the current version.")
    readings: list[ReadingIn] = Field(min_length=3, max_length=READINGS_MAX)
    run_id: UUID | None = Field(
        default=None,
        description=(
            "The run (printed page) the readings were taken on; it must belong to this printer "
            "and tray, else `422 run_not_for_this_tray`, and be printed for the fitted version, "
            "else `422 run_version_mismatch`. A check print's run adds the geometry suggestion."
        ),
    )


class FitResponse(BaseModel):
    values: CalibrationIn
    residuals_mm: list[float]
    flagged: list[int] = Field(
        description="0-based indices into `readings` of those further than 0.3 mm from the fit."
    )
    out_of_range: list[str] = Field(description="Names of values outside the allowed ranges.")
    suggestion: GeometrySuggestion | None = Field(
        default=None,
        description=(
            "With the `run_id` of a check print whose correction was measured on another sheet "
            "type: the geometry the remaining readings point at (null fields are unchanged)."
        ),
    )
    suggestion_unavailable: SuggestionUnavailable | None = Field(
        default=None,
        description=(
            "Why a run gives no suggestion: `no_check_run` (not a check print, or printed without "
            "a correction), `same_type` (the correction was measured on this sheet type, so its "
            "geometry error is already in the correction). Null without `run_id`."
        ),
    )


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
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                "A value is out of range (`calibration_out_of_range`), the tray is unknown "
                "(`tray_unknown`), `run_id` names a run of another printer or tray "
                "(`run_not_for_this_tray`) or was printed for another version than `measured_on` "
                "(`run_version_mismatch`), `readings` came without `measured_on` "
                "(`invalid_geometry`, `error_detail.field` is `measured_on`), has fewer than 3 or "
                "more than 200 entries (request validation), the readings lie on one line "
                "(`readings_insufficient`), or a reading names a gap outside the sheet "
                "(`invalid_geometry`, `error_detail.field` names the reading, such as "
                "`readings[0].col_gap`)."
            )
        }
    },
    summary="Set the calibration of a tray (a new history entry)",
)
async def put_calibration(
    printer_id: PrinterPath,
    tray: TrayPath,
    body: CalibrationPut,
    session: SessionDep,
    auth: AdminDep,
) -> CalibrationRead:
    """Store the tray's calibration and add a history entry in the same transaction.

    With `readings` the stored values are the hub's fit of them on `measured_on` (`source`
    `fit`); a sent value further than 0.05 mm from the fit is replaced and named in
    `ignored_fields`, with `client_values_ignored` in `notes`. Without readings the values are
    stored as sent (`source` `manual`).
    """
    check_printer_access(auth, printer_id)
    measured = (body.measured_on.type_key, body.measured_on.version) if body.measured_on else None
    readings = (
        None
        if body.readings is None
        else [Reading(r.col_gap, r.row_gap, r.across_mm, r.down_mm) for r in body.readings]
    )
    with mapped():
        return await service.put(
            session,
            printer_id,
            tray,
            CalibrationIn(**body.model_dump(include=set(CalibrationIn.model_fields))),
            measured_on=measured,
            actor=actor(auth),
            run_id=body.run_id,
            readings=readings,
            note=body.note,
        )


@router.delete(
    "/{printer_id}/calibration/{tray}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Reset a tray to no calibration (a `reset` history entry)",
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
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                "`run_id` names a run of another printer or tray (`run_not_for_this_tray`) or "
                "was printed for another version (`run_version_mismatch`), the readings lie on "
                "one line (`readings_insufficient`), or a reading names a gap outside the sheet "
                "(`invalid_geometry`)."
            )
        }
    },
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
        run = (
            None
            if body.run_id is None
            else await service.check_run(session, body.run_id, printer_id, tray, version)
        )
        geometry = ts.version_geometry(version)
        readings = [Reading(r.col_gap, r.row_gap, r.across_mm, r.down_mm) for r in body.readings]
        result = fit(geometry, geometry.page_size, readings)
        suggestion, unavailable = (
            (None, None)
            if run is None
            else await suggest.suggestion_for_run(session, run, geometry, readings)
        )
    return FitResponse(
        values=deviation_to_values(result.dev),
        residuals_mm=result.residuals_mm,
        flagged=result.flagged,
        out_of_range=result.out_of_range,
        suggestion=suggestion,
        suggestion_unavailable=unavailable,
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
_PRINT_UNPROCESSABLE = {
    "description": (
        _PAGE_UNPROCESSABLE["description"]
        + " The run code would not fit its QR symbol at 0.25 mm modules "
        "(`calibration_qr_too_large`, with `length`, `version_needed` and `max_version`); it is "
        "never shortened."
    )
}
_DOWNLOAD_RETRY_AFTER_S = 1


@router.post(
    "/{printer_id}/calibration/{tray}/page",
    response_model=PageJobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: _PRINT_UNPROCESSABLE,
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
        time_zone = str(_setting(request, "general.time_zone"))
        payload = await page.build_payload(
            session, printer_id, tray, type_key=body.type, version=body.version,
            corrected=body.corrected, time_zone=time_zone,
        )  # fmt: skip
        queue = getattr(request.app.state, "print_queue", None)
        if queue is None:
            raise _printer_unavailable(printer_id)
        version = await ts.get_version(
            session, payload.detail["type_key"], payload.detail["version"]
        )

        async def submit(stamped: PagePayload, job_id: UUID) -> UUID:
            # The run number is known now: a page code that no longer fits is refused, never cut.
            page.check_run_codes(stamped)
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
                time_zone=time_zone,
                retention_days=_job_retention_days(request),
                page_codes=page.payload_layout(payload).page_code_records(),
            )
        except KeyError:
            raise _printer_unavailable(printer_id) from None
    return PageJobAccepted(job_id=job_id)


class MarginTextSummary(BaseModel):
    """Per margin block: `full` (6 pt), `reduced` (smaller, at least 4.5 pt) or `omitted`."""

    top: MarginText = Field(description="The run, its date and time, the sheet type and tray.")
    bottom: MarginText = Field(
        description="The correction, the values not as shipped, the checkerboard hint."
    )


class PageLayoutSummary(BaseModel):
    """What the print dialog says before a calibration or check page is printed (design 7.2)."""

    page_codes: int = Field(
        ge=0,
        le=4,
        description=(
            "How many of the four page codes the page carries on this printer and tray; fewer "
            'than 4: "This sheet type leaves room for 2 of the 4 page codes on this printer".'
        ),
    )
    run_line_slots: int = Field(
        ge=0,
        le=2,
        description=(
            "Run-line slots reserved for the later passes of a check sheet; fewer than "
            "`passes_total - 1`: the later passes print without their run line."
        ),
    )
    passes_total: int = Field(ge=1, description="Passes the sheet takes.")
    margin_text: MarginTextSummary = Field(
        description=(
            "How the run information fits the margins above and below the grid. It never goes "
            "across the labels: a block that does not fit at 6 pt is printed smaller (down to "
            "4.5 pt), and one that does not fit even then is left out, so the dialog can say "
            "before printing that the page prints without that margin text. The page codes "
            "carry the run, the sheet type, its version and the correction either way."
        )
    )


PageKindQuery = Literal["calibration_page", "check_page"]


@router.get(
    "/{printer_id}/calibration/{tray}/page/layout",
    response_model=PageLayoutSummary,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "The key may not use this printer (`printer_not_allowed`)."
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: _PAGE_UNPROCESSABLE,
    },
    summary="How a calibration or check page fits the printable area of a printer and tray",
)
async def get_calibration_page_layout(
    request: Request,
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
    kind: Annotated[
        PageKindQuery,
        Query(description="`check_page` is printed with the tray's stored correction."),
    ] = "calibration_page",
) -> PageLayoutSummary:
    """The layout the page would get now: the printer's hardware margins (5 mm per side when it
    reports none) and the tray's stored offset decide the printable area, as when it is printed.
    Nothing is stored and no run is taken."""
    check_printer_access(auth, printer_id)
    with mapped():
        payload = await page.build_payload(
            session, printer_id, tray, type_key=type_key, version=version,
            corrected=kind == "check_page", time_zone=str(_setting(request, "general.time_zone")),
        )  # fmt: skip
        passes = 1
        layout = page.payload_layout(payload, passes=passes)
    return PageLayoutSummary(
        page_codes=len(layout.page_codes),
        run_line_slots=len(layout.run_line_slots),
        passes_total=passes,
        margin_text=MarginTextSummary(
            top=layout.margin_text["top"], bottom=layout.margin_text["bottom"]
        ),
    )


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


_HISTORY_ACCESS: dict[int | str, dict[str, Any]] = {
    status.HTTP_403_FORBIDDEN: {
        "description": "The key may not use this printer (`printer_not_allowed`)."
    },
    status.HTTP_404_NOT_FOUND: {"description": "No such printer (`printer_not_found`)."},
}


@router.get(
    "/{printer_id}/calibration/{tray}/history",
    response_model=list[CalibrationHistoryRead],
    responses=_HISTORY_ACCESS,
    summary="Saved calibrations of a tray, newest first",
)
async def list_calibration_history(
    request: Request,
    response: Response,
    printer_id: PrinterPath,
    tray: TrayPath,
    session: SessionDep,
    auth: ReadDep,
    limit: Annotated[int, Query(ge=1, le=history.LIST_LIMIT_MAX)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CalibrationHistoryRead]:
    """Every save, reset, revert and import of the tray (the newest 500 are kept), each with the
    change to the values before it and the number of jobs printed with it. A `Link` header names
    the next page. The tray is not checked against the printer's current trays."""
    check_printer_access(auth, printer_id)
    if await printers_repo.get(session, printer_id) is None:
        raise PrinterNotFoundError().to_http()
    entries, has_more = await history.list_entries(
        session, printer_id, tray, limit=limit, offset=offset
    )
    response.headers.update(next_link(request, limit=limit, offset=offset, has_more=has_more))
    return entries


@router.post(
    "/{printer_id}/calibration/{tray}/history/{entry_id}/restore",
    response_model=CalibrationRead,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "The key lacks the admin scope (`insufficient_scope`) or may not use this printer "
                "(`printer_not_allowed`)."
            )
        },
        status.HTTP_404_NOT_FOUND: {
            "description": (
                "No such printer, or no such entry on this printer and tray "
                "(`calibration_history_not_found`)."
            )
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "The tray is not an enabled tray of the printer (`tray_unknown`)."
        },
    },
    summary="Use an earlier calibration of the tray again (a `revert` history entry)",
)
async def restore_calibration(
    printer_id: PrinterPath,
    tray: TrayPath,
    entry_id: Annotated[UUID, Path(description="History entry id.")],
    session: SessionDep,
    auth: AdminDep,
    body: Annotated[RestoreRequest | None, Body()] = None,
) -> CalibrationRead:
    """Write a new `revert` entry with the entry's values; nothing is deleted. A `reset` entry
    clears the tray. When the entry's sheet type version was deleted since, the values are
    restored without it and `notes` carries `measured_on_deleted`. The optional body's `note` is
    kept on the new entry."""
    check_printer_access(auth, printer_id)
    with mapped():
        return await history.restore(
            session,
            printer_id,
            tray,
            entry_id,
            actor=actor(auth),
            note=body.note if body is not None else None,
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
    request: Request,
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
            session, printer_id, tray, type_key=type_key, version=version, corrected=corrected,
            time_zone=str(_setting(request, "general.time_zone")),
        )  # fmt: skip
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
