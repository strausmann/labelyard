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
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.identity_hold import ensure_not_held
from app.api.pagination import next_link
from app.api.routes.sheet_common import actor, mapped
from app.auth.dependencies import AuthContext, check_printer_access
from app.auth.scope_deps import require_admin, require_print, require_read
from app.calibration import history, page, probe, runs, scale, service, suggest
from app.calibration.check_sheet import (
    CheckSheetOpen,
    check_passes,
    first_pass_payload,
    open_check_sheet,
)
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
from app.config import get_settings
from app.db.session import get_session
from app.repositories import printers as printers_repo
from app.schemas.problem import ProblemDetail
from app.services import render_slots
from app.services.page_jobs import PagePayload
from app.settings_store.registry import SETTINGS
from app.sheets import type_service as ts
from app.sheets.errors import PrinterNotFoundError, SheetPrinterDisabledError
from app.sheets.print_service import RenderTimeoutApiError, RenderUnavailableApiError
from app.sheets.type_log import SheetTypeLogRead

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
            "geometry error is already in the correction), `unknown_correction` (the sheet type "
            "the correction was measured on is not known: typed in by hand, or its history entry "
            "or version is gone). Null without `run_id`."
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
                "(`run_version_mismatch`), `run_id` names a check print, whose readings verify the "
                "correction and are never one (`run_is_a_check_print`), or a probe print "
                "(`run_is_a_probe_print`), `readings` came without "
                "`measured_on` "
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


class CheckReadingsRequest(BaseModel):
    run_id: UUID = Field(description="The check print the readings were taken on.")
    readings: list[ReadingIn] = Field(min_length=3, max_length=READINGS_MAX)


@router.post(
    "/{printer_id}/calibration/{tray}/check-readings",
    response_model=SheetTypeLogRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                "`run_id` names a run of another printer or tray (`run_not_for_this_tray`) or a "
                "page that is no check print (`run_not_a_check_print`), the tray is unknown "
                "(`tray_unknown`), the readings lie on one line (`readings_insufficient`), or a "
                "reading names a gap outside the sheet (`invalid_geometry`)."
            )
        }
    },
    summary="Record the readings of a check print in the sheet-type log (the correction stays)",
)
async def record_check_readings(
    printer_id: PrinterPath,
    tray: TrayPath,
    body: CheckReadingsRequest,
    session: SessionDep,
    auth: AdminDep,
) -> SheetTypeLogRead:
    """Write a `check_print` entry to the log of the run's sheet type: the readings, each with its
    residual from their fit, the run and the correction the page was printed with. The tray's
    correction and its history are never changed; a check verifies the correction."""
    check_printer_access(auth, printer_id)
    readings = [Reading(r.col_gap, r.row_gap, r.across_mm, r.down_mm) for r in body.readings]
    with mapped():
        return await service.record_check_readings(
            session, printer_id, tray, body.run_id, readings, actor=actor(auth)
        )


class PageRequest(BaseModel):
    type: str = Field(min_length=1, max_length=200, description="Sheet type key.")
    version: int | None = Field(default=None, ge=1, description="Defaults to the current version.")
    corrected: bool = Field(
        default=False,
        description="Print with the tray's stored correction, to check it (not to measure).",
    )
    check_sheet: runs.CheckSheetMode | None = Field(
        default=None,
        description=(
            "Only with `corrected`: print a pass of a check sheet (design 7.2). `new` starts a "
            "sheet with pass 1, the full check page; `next` prints the lowest free pass of the "
            "newest open sheet of this printer, tray and sheet type version (passes 2 and 3: "
            "their crosses and a run line only), else `409 check_sheet_full`. Without it the "
            "check page is the single full page, as before 1.8.0."
        ),
    )

    @model_validator(mode="after")
    def _check_sheet_needs_correction(self) -> PageRequest:
        if self.check_sheet is not None and not self.corrected:
            raise ValueError("check_sheet needs corrected: true")
        return self


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
                "print queue (`printer-unavailable`), the device behind it changed "
                "(`printer-identity-mismatch`), or `check_sheet` `next` found no open check "
                "sheet with a free pass (`check_sheet_full`)."
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
        choice: runs.CheckSheetChoice | None = None
        if body.check_sheet is not None:
            g = ts.version_geometry(version)
            passes = check_passes(g.cols, g.rows)
            choice = runs.CheckSheetChoice(body.check_sheet, version.id, passes)
            if passes:
                payload.detail["passes_total"] = len(passes)

        def page_codes(pass_no: int | None) -> list[dict[str, Any]]:
            if pass_no is not None and pass_no > 1:
                return []  # passes 2 and 3 draw no codes
            drawn = payload
            if choice is not None and pass_no == 1 and choice.passes:
                # Pass 1 is laid out with its crosses, not the full grid: its codes sit there.
                drawn = payload.model_copy(
                    update={
                        "detail": {
                            **payload.detail,
                            "pass_no": 1,
                            "pass_crossings": [list(c) for c in choice.crossings(1) or []],
                            "reserved_crossings": [list(c) for c in choice.reserved(1)],
                        }
                    }
                )
            return page.payload_layout(drawn).page_code_records()

        async def submit(stamped: PagePayload, job_id: UUID) -> UUID:
            # The run number is known now: a page code that no longer fits is refused, never cut.
            page.check_run_codes(stamped)
            if isinstance(stamped.detail.get("pass_no"), int) and stamped.detail["pass_no"] > 1:
                # The run line goes where pass 1 left room, under pass 1's correction (#449).
                first = await first_pass_payload(session, UUID(stamped.detail["check_sheet_id"]))
                box = page.later_run_line_box(stamped, first) if first is not None else None
                if box is not None:
                    stamped.detail["run_line_box"] = box
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
                check_sheet=choice,
                page_codes=page_codes,
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
    check_sheet: Annotated[
        runs.CheckSheetMode | None,
        Query(
            description=(
                "With `check_page`: the page is pass 1 of a check sheet (as `check_sheet` on the "
                "print request); `passes_total` counts the sheet's passes and `run_line_slots` "
                "the slots pass 1 reserves for them. Without it the check page is the single "
                "full page, one pass. Ignored for `calibration_page`."
            )
        ),
    ] = None,
) -> PageLayoutSummary:
    """The layout the page would get now: the printer's hardware margins (5 mm per side when it
    reports none) and the tray's stored offset decide the printable area, as when it is printed.
    The summary describes exactly the page the same print request would draw. Nothing is stored
    and no run is taken."""
    check_printer_access(auth, printer_id)
    with mapped():
        payload = await page.build_payload(
            session, printer_id, tray, type_key=type_key, version=version,
            corrected=kind == "check_page", time_zone=str(_setting(request, "general.time_zone")),
        )  # fmt: skip
        g = ts.version_geometry(
            await ts.get_version(session, payload.detail["type_key"], payload.detail["version"])
        )
        sheet_passes = check_passes(g.cols, g.rows) if kind == "check_page" and check_sheet else []
        passes = max(len(sheet_passes), 1)
        if sheet_passes:
            # Pass 1 as it is stamped: its own crosses, the later ones reserved.
            payload.detail.update(
                passes_total=passes,
                pass_no=1,
                pass_crossings=[list(c) for c in sheet_passes[0]],
                reserved_crossings=[list(c) for p in sheet_passes[1:] for c in p],
            )
        layout = page.payload_layout(payload)
    return PageLayoutSummary(
        page_codes=len(layout.page_codes),
        run_line_slots=len(layout.run_line_slots),
        passes_total=passes,
        margin_text=MarginTextSummary(
            top=layout.margin_text["top"], bottom=layout.margin_text["bottom"]
        ),
    )


@router.get(
    "/{printer_id}/calibration/{tray}/check-sheets/open",
    response_model=CheckSheetOpen | None,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "The key may not use this printer (`printer_not_allowed`)."
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: _PAGE_UNPROCESSABLE,
    },
    summary="The open check sheet of a printer, tray and sheet type version, if any",
)
async def get_open_check_sheet(
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
) -> CheckSheetOpen | None:
    """The newest check sheet with a free pass, for the print dialog's "Print on the same sheet?
    (pass 2 of 3)"; `null` when there is none (every pass printed, or no sheet started).
    `run_line_slots` are the slots pass 1 reserved in the layout it was printed with (0 once its
    job was cleaned up). Nothing is stored."""
    check_printer_access(auth, printer_id)
    with mapped():
        payload = await page.build_payload(
            session, printer_id, tray, type_key=type_key, version=version, corrected=True,
            time_zone=str(_setting(request, "general.time_zone")),
        )  # fmt: skip
        v = await ts.get_version(session, payload.detail["type_key"], payload.detail["version"])
        sheet = await open_check_sheet(session, printer_id, tray, v.id, run_line_slots=0)
        if sheet is None:
            return None
        # The slots pass 1 reserved, in the layout it was printed with.
        first = await first_pass_payload(session, sheet.check_sheet_id)
        # Each later pass as it would print now, with the tray's current correction: does the
        # slot pass 1 reserved still land inside its printable area (review on #449)?
        printed = {p.pass_no for p in sheet.passes}
        fits: dict[int, bool] = {}
        for pass_no in range(2, sheet.passes_total + 1):
            later = payload.model_copy(
                update={
                    "detail": {**payload.detail, "passes_total": sheet.passes_total,
                               "pass_no": pass_no}
                }
            )  # fmt: skip
            fits[pass_no] = first is not None and page.later_run_line_box(later, first) is not None
        return sheet.model_copy(
            update={
                "run_line_slots": sum(fits.values()),
                "passes_without_run_line": [
                    p for p, ok in fits.items() if not ok and p not in printed
                ],
            }
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
    pdf = await _render_download(
        page.render_calibration_page, payload, label=f"calibration:{printer_id}", auth=auth
    )
    name = re.sub(r"[^A-Za-z0-9._-]", "_", f"calibration-{payload.detail['type_key']}")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{name}-v{payload.detail["version"]}.pdf"'
        },
    )


# ---------------------------------------------------------------------------- probe print


class ProbeRequest(BaseModel):
    type: str = Field(min_length=1, max_length=200, description="Sheet type key.")
    version: int | None = Field(default=None, ge=1, description="Defaults to the current version.")
    grid: bool = Field(
        default=False,
        description=(
            "Print the measurement sheet: the probe page plus a millimetre grid over the whole "
            "page (lines every 1 mm, heavier every 5 mm, labelled every 10 mm, origin at the "
            "page's top-left corner), drawn without any correction."
        ),
    )


class ProbeJobAccepted(BaseModel):
    job_id: UUID
    run_no: int = Field(description="The run number the page carries (per printer and tray).")


_PROBE_DOWNLOAD_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"content": {"application/pdf": {}}, "description": "The probe page."},
    status.HTTP_403_FORBIDDEN: {
        "description": "The key may not use this printer (`printer_not_allowed`)."
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": (
            "The tray is not an enabled tray of the printer (`tray_unknown`), or the grid has "
            "more than 2500 cells (`calibration_grid_too_large`)."
        )
    },
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "description": (
            "A render of the key is stuck past the time limit (`too-many-previews`). The "
            "body has the rate limit's shape (`retry_after_seconds`)."
        )
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": "No render slot became free in time (`render-unavailable`)."
    },
    status.HTTP_504_GATEWAY_TIMEOUT: {
        "description": (
            "The page took longer than `LABELYARD_RENDER_TIMEOUT_S` to draw (`render-timeout`)."
        )
    },
}


@router.post(
    "/{printer_id}/calibration/{tray}/probe",
    response_model=ProbeJobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "The key lacks the print scope (`insufficient_scope`) or may not use this "
                "printer (`printer_not_allowed`)."
            )
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "No such printer (`printer_not_found`) or sheet type."
        },
        status.HTTP_409_CONFLICT: {
            "description": (
                "The printer is disabled (`printer_disabled`), is a tape printer "
                "(`media_kind_mismatch`), its fresh status says it is offline "
                "(`printer-offline`; a stale status never refuses), it is not known to the print "
                "queue (`printer-unavailable`), or the device behind it changed "
                "(`printer-identity-mismatch`)."
            )
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                "The tray is not an enabled tray of the printer (`tray_unknown`), or the grid has "
                "more than 2500 cells (`calibration_grid_too_large`)."
            )
        },
    },
    summary="Print the probe page or the measurement sheet on plain paper (no sheet is created)",
)
async def print_probe_page(
    request: Request,
    printer_id: PrinterPath,
    tray: TrayPath,
    body: ProbeRequest,
    session: SessionDep,
    auth: PrintDep,
) -> ProbeJobAccepted:
    """Queue a probe page: every label outline of the sheet type with its number and row and
    column, the preprint zones hatched and the planned sheet-ID rectangle dashed, drawn with the
    tray's current correction (the measurement sheet, `grid`, without any). No sheet, reservation
    or cell changes; the page takes the tray's next run number and can be retried from Jobs."""
    check_printer_access(auth, printer_id)
    with mapped():
        printer = await service.page_printer(session, printer_id)
        if not printer.enabled:
            raise SheetPrinterDisabledError()
    queue = getattr(request.app.state, "print_queue", None)
    ensure_not_held(queue, printer_id)  # #231
    if await probe.fresh_offline(
        session, printer_id, probe_interval_s=get_settings().sse_probe_interval_s
    ):
        raise _printer_offline(printer_id)
    with mapped():
        time_zone = str(_setting(request, "general.time_zone"))
        payload = await probe.build_probe_payload(
            session, printer_id, tray, type_key=body.type, version=body.version, grid=body.grid,
            time_zone=time_zone,
        )  # fmt: skip
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

        # #336: the run is committed before the job; a failed submit deletes it again (ADR 0029).
        try:
            run, job_id = await runs.submit_with_run(
                session,
                payload,
                printer_id=printer_id,
                kind="probe_page",
                type_version_id=version.id,
                actor=actor(auth),
                submit=submit,
                time_zone=time_zone,
                retention_days=_job_retention_days(request),
            )
        except KeyError:
            raise _printer_unavailable(printer_id) from None
    # ``run`` is a detached snapshot (``submit_with_run``); only its number is read.
    return ProbeJobAccepted(job_id=job_id, run_no=run.run_no)


@router.get(
    "/{printer_id}/calibration/{tray}/probe/pdf",
    response_class=Response,
    responses=_PROBE_DOWNLOAD_RESPONSES,
    summary="Download the probe page or the measurement sheet as a PDF",
)
async def download_probe_page(
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
    grid: Annotated[
        bool, Query(description="The measurement sheet: with the millimetre grid, uncorrected.")
    ] = False,
) -> Response:
    """The page the job would print, without a run (no run number and time)."""
    check_printer_access(auth, printer_id)
    with mapped():
        payload = await probe.build_probe_payload(
            session, printer_id, tray, type_key=type_key, version=version, grid=grid,
            time_zone=str(_setting(request, "general.time_zone")),
        )  # fmt: skip
    # No read transaction stays open while the page draws (#405).
    await session.commit()
    pdf = await _render_download(
        probe.render_probe_page, payload, label=f"probe:{printer_id}", auth=auth
    )
    what = "measurement-sheet" if grid else "probe"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", f"{what}-{payload.detail['type_key']}")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{name}-v{payload.detail["version"]}.pdf"'
        },
    )


async def _render_download(
    render: Any, payload: PagePayload, *, label: str, auth: AuthContext
) -> bytes:
    """Draw ``payload`` under the render slots (#358) and map their refusals to the answers the
    download routes document (504, 503, 429)."""
    try:
        pdf: bytes = await render_slots.render_bounded(
            render,
            payload,
            label=label,
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
    return pdf


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


def _printer_offline(printer_id: UUID) -> HTTPException:
    problem = ProblemDetail(
        type="printer-offline",
        title="The printer is offline",
        status=status.HTTP_409_CONFLICT,
        detail=f"The last status of printer {printer_id} says it is offline",
    )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail=problem.model_dump(exclude_none=True)
    )
