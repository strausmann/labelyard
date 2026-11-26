# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Error types of the sheet archive API.

Every error maps to the existing error shape
``HTTPException(status, detail={"error_code", "error_message", "error_detail"})``.
Messages are fixed English text; raw input is never echoed beyond identifiers.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException


class SheetApiError(Exception):
    """Base class: a domain error with an HTTP status and a stable error code."""

    status: int = 400
    error_code: str = "sheet_error"
    message: str = "Sheet request failed."

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.message = message if message is not None else type(self).message
        self.detail = detail
        super().__init__(self.message)

    def to_http(self) -> HTTPException:
        return HTTPException(
            status_code=self.status,
            detail={
                "error_code": self.error_code,
                "error_message": self.message,
                "error_detail": self.detail,
            },
        )


class SheetTypeNotFoundError(SheetApiError):
    status = 404
    error_code = "sheet_type_not_found"
    message = "Sheet type not found."


class CatalogEntryNotFoundError(SheetApiError):
    """No catalog entry (or preset) under this key ships in the image (ADR 0028)."""

    status = 404
    error_code = "catalog_entry_not_found"
    message = "Catalog entry not found."


class SheetTypeVersionNotFoundError(SheetApiError):
    status = 404
    error_code = "sheet_type_version_not_found"
    message = "Sheet type version not found."


class NewerVersionInUseError(SheetApiError):
    """A version is restored only while no sheet is pinned to a newer one (#336 point 4)."""

    status = 409
    error_code = "newer_version_in_use"
    message = "A sheet uses a newer version of this type; the version cannot be restored."


class SheetTypeArchivedError(SheetApiError):
    status = 409
    error_code = "sheet_type_archived"
    message = "Sheet type is archived."


class TypeExistsError(SheetApiError):
    status = 409
    error_code = "type_exists"
    message = "A sheet type with this key already exists."


class TypeKeyReservedError(SheetApiError):
    """The key is a static path segment under ``/api/v1/sheet-types/`` (``facets``, …)."""

    status = 422
    error_code = "type_key_reserved"
    message = "This sheet type key is reserved by the API; choose another key."


class PrefixTakenError(SheetApiError):
    status = 409
    error_code = "prefix_taken"
    message = "This sheet code prefix is already used by another sheet type."


class PrefixLockedError(PrefixTakenError):
    """The prefix cannot change any more because sheets already carry it (OC-5)."""

    def __init__(self) -> None:
        super().__init__(
            "The sheet code prefix cannot change while sheets of this type exist.",
            detail={"reason": "sheets_exist"},
        )


class SheetTypeInUseError(SheetApiError):
    status = 409
    error_code = "type_in_use"
    message = "Sheet type cannot be deleted."

    def __init__(self, reason: str, **extra: Any) -> None:
        super().__init__(detail={"reason": reason, **extra})


class TypeNotUnusedError(SheetApiError):
    """``delete-unused`` named types that are not in state ``unused`` (spec 6.1.5)."""

    status = 409
    error_code = "not_unused"
    message = "Some of these sheet types are not unused; nothing was deleted."

    def __init__(self, keys: list[str]) -> None:
        super().__init__(detail={"keys": keys})


class TypeLocallyEditedError(SheetApiError):
    """The type differs from what the catalog installed; its update is not applied (6.1.3)."""

    status = 409
    error_code = "locally_edited"
    message = "This sheet type was edited here; the catalog update is not applied."


class BackingDesignDiffersError(SheetApiError):
    """The local backing design of the entry's key differs and its state is not a shipped one.

    Only for a design no other type links and that the written type does not prove (an unedited
    catalog type linking a design proves its state); a design linked by a type that cannot follow
    gives ``backing_design_shared`` (ADR 0028).
    """

    status = 409
    error_code = "backing_design_differs"
    message = "The local backing design of this key differs from the catalog's."

    def __init__(self, design: str) -> None:
        super().__init__(detail={"design": design})


class BackingDesignSharedError(SheetApiError):
    """Other types link the backing design the write would change, and they cannot follow.

    Each of them must be an unedited catalog type whose newest catalog entry carries the same
    new design; ``types`` names the ones that are not (ADR 0028).
    """

    status = 409
    error_code = "backing_design_shared"
    message = "Other sheet types use this backing design and cannot take the catalog's change."

    def __init__(self, design: str, types: list[str]) -> None:
        super().__init__(detail={"design": design, "types": types})


class NoCatalogUpdateError(SheetApiError):
    """The catalog has no newer revision for this type, or the type is not from the catalog."""

    status = 409
    error_code = "no_update"
    message = "The catalog has no update for this sheet type."


class InvalidGeometryError(SheetApiError):
    status = 422
    error_code = "invalid_geometry"

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(
            f"Invalid geometry: {field}: {reason}.",
            detail={"field": field, "reason": reason},
        )


class CornerRadiusTooLargeError(InvalidGeometryError):
    """``corner_radius`` is larger than half the shorter label side (#204 note 4): its own code,
    the detail of ``invalid_geometry`` (``field`` ``corner_radius``)."""

    error_code = "corner_radius_too_large"

    def __init__(self, limit: int) -> None:
        super().__init__("corner_radius", f"larger than half the shorter label side ({limit})")


class InvalidFieldError(InvalidGeometryError):
    """A non-geometry field has a bad value; same code and status as invalid geometry."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(field, reason)
        self.message = f"Invalid value for {field}: {reason}."
        self.args = (self.message,)


class TextTooLongError(InvalidFieldError):
    """A text field is longer than its limit after cleaning (264-12): its own code, not
    ``invalid_geometry``; status and detail stay as before."""

    error_code = "label_too_long"

    def __init__(self, field: str) -> None:
        super().__init__(field, "too long")


class SheetNotFoundError(SheetApiError):
    status = 404
    error_code = "sheet_not_found"
    message = "Sheet not found."


class SheetDiscardedError(SheetApiError):
    status = 409
    error_code = "sheet_discarded"
    message = "Sheet is discarded."


class SheetBusyError(SheetApiError):
    status = 409
    error_code = "sheet_busy"
    message = "Sheet is in use by a running job."


class SheetLimitReachedError(SheetApiError):
    """A new sheet would exceed ``sheets.max_active`` (``hub``) or ``sheets.max_active_per_type``
    (``type``); only active sheets count, so discarding one frees room (#204)."""

    status = 409
    error_code = "sheet_limit_reached"
    message = "The limit of active sheets is reached. Discard a sheet to make room."

    def __init__(self, cap: int, scope: str) -> None:
        super().__init__(detail={"cap": cap, "scope": scope})


class SheetPurgedError(SheetApiError):
    """A job names a sheet that was purged (#204): the job keeps its payload, the sheet is gone."""

    status = 409
    error_code = "sheet_purged"
    message = "The sheet of this job was purged."

    def __init__(self, sheet_code: str) -> None:
        super().__init__(detail={"sheet_code": sheet_code})


class CellsTakenError(SheetApiError):
    status = 409
    error_code = "cells_taken"
    message = "Some cells are not free."

    def __init__(self, cells: list[int]) -> None:
        super().__init__(detail={"cells": cells})


class CellsNotMarkedByHandError(SheetApiError):
    """Only cells marked by hand can be un-marked; a printed cell carries a label (spec 8)."""

    status = 409
    error_code = "cells_not_marked_by_hand"
    message = "Some cells were not marked by hand."

    def __init__(self, cells: list[int]) -> None:
        super().__init__(detail={"cells": cells})


class CellsNotUnconfirmedError(SheetApiError):
    status = 409
    error_code = "cells_not_unconfirmed"
    message = "Some cells are not waiting for confirmation."

    def __init__(self, cells: list[int]) -> None:
        super().__init__(detail={"cells": cells})


class NoUnconfirmedCellsError(SheetApiError):
    status = 409
    error_code = "no_unconfirmed_cells"
    message = "The sheet has no cells waiting for confirmation."


class UnconfirmedCountChangedError(SheetApiError):
    status = 409
    error_code = "unconfirmed_count_changed"
    message = "The cells waiting for confirmation have changed."

    def __init__(self, unconfirmed: int) -> None:
        super().__init__(detail={"unconfirmed": unconfirmed})


class CellOutOfRangeError(SheetApiError):
    status = 422
    error_code = "cell_out_of_range"
    message = "Cell number is outside the sheet."

    def __init__(self, cells: list[int], max_cell: int) -> None:
        super().__init__(detail={"cells": cells, "max": max_cell})


class InsufficientCellsError(SheetApiError):
    """The free cells of the chosen sheets do not hold all items (spec 8)."""

    status = 409
    error_code = "insufficient_cells"
    message = "Not enough free cells for these items."

    def __init__(self, free_cells: list[int], needed: int, shortfall: int) -> None:
        super().__init__(
            detail={"free_cells": free_cells, "needed": needed, "shortfall": shortfall}
        )


class PlanChangedError(SheetApiError):
    """The fresh plan differs from the one the client confirmed (OC-8); nothing was written."""

    status = 409
    error_code = "plan_changed"
    message = "The cell plan changed since the preview."

    def __init__(self, plan: list[dict[str, Any]]) -> None:
        super().__init__(detail={"plan": plan})


class SheetTypeMismatchError(SheetApiError):
    """A placement names a sheet of another type than the request needs."""

    status = 409
    error_code = "sheet_type_mismatch"
    message = "The sheet is of another type than this print needs."

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(detail={"expected": expected, "actual": actual})


class InvalidPlacementsError(SheetApiError):
    """The placements contradict each other or the item count; ``reason`` names the rule."""

    status = 422
    error_code = "invalid_placements"
    message = "The placements are not valid."

    def __init__(self, reason: str) -> None:
        super().__init__(detail={"reason": reason})


class ImportTooLargeError(SheetApiError):
    status = 413
    error_code = "import_too_large"
    message = "Import document is too large."


class InvalidImportError(SheetApiError):
    status = 422
    error_code = "invalid_import"
    message = "Import document is invalid."

    def __init__(self, errors: list[str]) -> None:
        super().__init__(detail={"errors": errors})


# ---------------------------------------------------------------------------- page printers
# Messages are fixed text: an error never names the IPP URI or repeats the printer's answer
# beyond the IPP status name (spec 18).


class MediaKindMismatchError(SheetApiError):
    """A tape route was called for a page printer (or the other way round)."""

    status = 409
    error_code = "media_kind_mismatch"
    message = "This printer does not print this kind of media."


class PageJobNotRetryableError(SheetApiError):
    """A page job that cannot be retried: a sheet page that is ``done`` (its labels came out)."""

    status = 409
    error_code = "job_not_retryable"
    message = "This page job cannot be retried."


class PrinterModelUnknownError(SheetApiError):
    status = 422
    error_code = "printer_model_unknown"
    message = "No driver is registered for this model."

    def __init__(self, known: list[str]) -> None:
        super().__init__(detail={"known": known})


class TrayUnknownError(SheetApiError):
    status = 422
    error_code = "tray_unknown"
    message = "The printer does not report some of these trays."

    def __init__(self, trays: list[str]) -> None:
        super().__init__(detail={"trays": trays})


class IppUnreachableApiError(SheetApiError):
    """409 when saving a printer (nothing was saved), 502 for the live probe."""

    status = 409
    error_code = "ipp_unreachable"
    message = "The printer could not be reached."

    def __init__(self, status: int = 409) -> None:
        super().__init__()
        self.status = status


class IppPdfUnsupportedError(SheetApiError):
    """OC-18: the hub sends nothing but PDF, so a printer without PDF cannot be saved."""

    status = 409
    error_code = "ipp_pdf_unsupported"
    message = "The printer does not accept PDF documents."


class StoredConnectionInvalidError(SheetApiError):
    """A saved IPP connection no longer validates (e.g. a hand-edited or old row)."""

    status = 409
    error_code = "stored_connection_invalid"
    message = "The stored connection of this printer is invalid."

    def __init__(self, printer_id: UUID) -> None:
        super().__init__(
            f"The stored connection of printer {printer_id} is invalid; "
            "send a new connection to replace it."
        )


class IppRejectedError(SheetApiError):
    status = 502
    error_code = "ipp_rejected"
    message = "The printer refused the request."

    def __init__(self, ipp_status: str) -> None:
        super().__init__(detail={"ipp_status": ipp_status})


class IppBadResponseApiError(SheetApiError):
    status = 502
    error_code = "ipp_bad_response"
    message = "The printer's answer could not be used."


# ---------------------------------------------------------------------------- calibration


class PrinterNotFoundError(SheetApiError):
    status = 404
    error_code = "printer_not_found"
    message = "Printer not found."


class CalibrationOutOfRangeError(SheetApiError):
    """A calibration value is outside its allowed range or has too many decimals."""

    status = 422
    error_code = "calibration_out_of_range"
    message = "A calibration value is outside the allowed range."

    def __init__(
        self,
        field: str,
        *,
        minimum: float,
        maximum: float,
        reason: str = "out_of_range",
    ) -> None:
        super().__init__(
            f"Calibration value {field} is not acceptable ({reason}).",
            detail={"field": field, "min": minimum, "max": maximum, "reason": reason},
        )


class ReadingsInsufficientError(SheetApiError):
    """Fewer than three readings, or all on one line: no deviation can be fitted."""

    status = 422
    error_code = "readings_insufficient"
    message = "The readings are not enough to fit a calibration."

    def __init__(self, reason: str) -> None:
        super().__init__(detail={"reason": reason})


class CalibrationTypeUnsuitableError(SheetApiError):
    """A calibration page needs crossings: at least two columns and two rows."""

    status = 422
    error_code = "calibration_type_unsuitable"
    message = (
        "This sheet type has no crossings to measure: it needs at least two columns and two rows."
    )

    def __init__(self, cols: int, rows: int) -> None:
        super().__init__(detail={"cols": cols, "rows": rows})


class CalibrationGridTooLargeError(SheetApiError):
    """A calibration page of this type would draw more cells than the render allows (#358)."""

    status = 422
    error_code = "calibration_grid_too_large"
    message = "This sheet type has too many cells for a calibration page."

    def __init__(self, cells: int, max_cells: int) -> None:
        super().__init__(detail={"cells": cells, "max_cells": max_cells})


class CalibrationQrTooLargeError(SheetApiError):
    """A page code needs a QR version whose modules would be below 0.25 mm (design 7.2).

    The payload is never shortened to fit: the page job is refused instead. ``version_needed`` is
    ``None`` when no QR version (1 to 40) holds the payload.
    """

    status = 422
    error_code = "calibration_qr_too_large"
    message = "The run code of this calibration page does not fit its QR symbol."

    def __init__(self, length: int, version_needed: int | None, max_version: int) -> None:
        super().__init__(
            detail={"length": length, "version_needed": version_needed, "max_version": max_version}
        )


class CheckSheetFullError(SheetApiError):
    """``check_sheet: next`` found no open check sheet of the printer, tray and sheet type
    version with a free pass (design 7.2): every pass is printed, or no sheet was started."""

    status = 409
    error_code = "check_sheet_full"
    message = "There is no check sheet with a free pass. Start a new sheet."


# ---------------------------------------------------------------------------- sheet printing


class CellsUnconfirmedError(SheetApiError):
    """A page job whose cells wait for confirmation cannot be retried (OC-42): the labels may
    have come out, so the person decides first (resolve as printed or not printed)."""

    status = 409
    error_code = "cells_unconfirmed"
    message = "Some cells of this job are waiting for confirmation."

    def __init__(self, cells: list[int]) -> None:
        super().__init__(detail={"cells": cells})


class SheetPrinterDisabledError(SheetApiError):
    """The printer is switched off; sheet prints are refused at the route (pre-flight C18)."""

    status = 409
    error_code = "printer_disabled"
    message = "This printer is disabled."


class PrinterIdentityMismatchError(SheetApiError):
    """The device behind the printer changed; sheet jobs are held (#231).

    The detail names the printer id only, never an identity value."""

    status = 409
    error_code = "printer-identity-mismatch"
    message = "The device behind this printer changed; sheet and calibration jobs are held."

    def __init__(self, printer_id: UUID) -> None:
        super().__init__(detail={"printer_id": str(printer_id)})


class ItemLookupFailedError(SheetApiError):
    """An item's integration lookup failed. It keeps the status and the code of the error the
    lookup raised (pre-flight C10) and names the item; the message is fixed text, because the
    lookup's own text can carry URLs and upstream detail."""

    def __init__(self, *, status: int, code: str, message: str, item_index: int) -> None:
        super().__init__(message, detail={"item_index": item_index})
        self.status = status
        self.error_code = code


class ItemContentMismatchError(SheetApiError):
    """An item lacks what its (mapped) content type needs; nothing is reserved (spec 12.5)."""

    status = 422
    error_code = "content_type_data_mismatch"
    message = "An item lacks the data its content type needs."

    def __init__(self, item_index: int, missing_fields: tuple[str, ...]) -> None:
        super().__init__(detail={"item_index": item_index, "missing_fields": list(missing_fields)})


class IdCellContentMismatchError(SheetApiError):
    """The sheet ID cell of a new ``cell`` sheet cannot show its label: its content box holds no
    line, or the short-URL QR does not fit. Nothing is created or reserved."""

    status = 422
    error_code = "content_type_data_mismatch"
    message = "The sheet ID cell cannot show the ID label on this sheet type."

    def __init__(self, missing_fields: tuple[str, ...]) -> None:
        super().__init__(detail={"cell": "id", "missing_fields": list(missing_fields)})


class PreviewTooLargeError(SheetApiError):
    """The plan has more pages than a preview renders (a load bound, spec 18)."""

    status = 422
    error_code = "preview_too_large"
    message = "The plan has too many pages for a preview."

    def __init__(self, pages: int, maximum: int) -> None:
        super().__init__(detail={"pages": pages, "max": maximum})


class PrinterUnavailableError(SheetApiError):
    """The print queue does not know the printer (for example a page printer added after the
    start); nothing was printed and the reserved cells are free again (pre-flight C24)."""

    status = 409
    error_code = "printer-unavailable"
    message = "Printer is not available to the print queue."


class SheetIdJobFailedError(SheetApiError):
    """Something unexpected stopped the ID-only job of a new ``cell`` sheet after the sheet was
    committed. The sheet stays without a reservation; the detail names it. The cause is logged,
    never returned."""

    status = 500
    error_code = "sheet_id_job_failed"
    message = "The sheet was created, but its ID label could not be queued."

    def __init__(self, sheet_code: str) -> None:
        super().__init__(detail={"sheet_code": sheet_code})


class PrintInterruptedError(SheetApiError):
    """A sheet print failed after some of its pages were queued (pre-flight ruling T7-2).

    The pages already queued keep printing; the detail names the batch and their jobs so the
    client can follow or cancel them instead of retrying the whole print."""

    status = 502
    error_code = "print_interrupted"
    message = "The print stopped after some pages were queued."

    def __init__(self, batch_id: str, job_ids: list[str]) -> None:
        super().__init__(detail={"batch_id": batch_id, "job_ids": job_ids})


# ---------------------------------------------------------------- sheet code placement (#350)


class IdCellOutOfRangeError(SheetApiError):
    """A sheet type's preferred ID cell is outside its current geometry (R-350-7)."""

    status = 422
    error_code = "id_cell_out_of_range"
    message = "The ID cell is outside the sheet."

    def __init__(self, cell: int, total: int) -> None:
        super().__init__(detail={"id_cell": cell, "total": total})


class BackingDesignNotFoundError(SheetApiError):
    status = 404
    error_code = "backing_design_not_found"
    message = "Backing design not found."


class BackingDesignExistsError(SheetApiError):
    status = 409
    error_code = "backing_design_exists"
    message = "A backing design with this key already exists."


class BackingDesignInUseError(SheetApiError):
    """A design some sheet type still points to is not deleted (R-350-4)."""

    status = 409
    error_code = "backing_design_in_use"
    message = "The backing design is used by a sheet type."

    def __init__(self, types_in_use: int) -> None:
        super().__init__(detail={"types_in_use": types_in_use})


class BackingDesignPageSizeMismatchError(SheetApiError):
    """The design's page size differs from the sheet type's current version (R-350-4)."""

    status = 422
    error_code = "backing_design_page_size_mismatch"
    message = "The backing design is for another page size."


class PreprintPresetNotFoundError(SheetApiError):
    status = 404
    error_code = "preprint_preset_not_found"
    message = "Preprint preset not found."


class PreprintPresetExistsError(SheetApiError):
    status = 409
    error_code = "preprint_preset_exists"
    message = "A preprint preset with this key already exists."


class PreprintPresetInUseError(SheetApiError):
    """A preset some sheet type still links is not deleted (#246)."""

    status = 409
    error_code = "preset_in_use"
    message = "The preprint preset is used by a sheet type."

    def __init__(self, types_in_use: int) -> None:
        super().__init__(detail={"types_in_use": types_in_use})


class PreprintPresetPageSizeMismatchError(SheetApiError):
    """The preset's page size differs from the sheet type's current version (#246)."""

    status = 422
    error_code = "preprint_preset_page_size_mismatch"
    message = "The preprint preset is for another page size."


class PreprintZoneOffPageError(SheetApiError):
    """A keep-out zone reaches outside the page (#246); ``field`` names it (``zones.0``)."""

    status = 422
    error_code = "preprint_zone_off_page"
    message = "A preprinted area reaches outside the page."

    def __init__(self, field: str) -> None:
        super().__init__(detail={"field": field})


class PreprintZoneOverlapsCellError(SheetApiError):
    """A keep-out zone covers a label cell (#246): a label there would print on the preprint."""

    status = 422
    error_code = "preprint_zone_overlaps_cell"
    message = "A preprinted area covers a label cell."

    def __init__(self, field: str) -> None:
        super().__init__(detail={"field": field})


class IdPositionOffPageError(SheetApiError):
    """The sheet code's minimal box (4 x 10 mm) at the chosen position leaves the page (#246)."""

    status = 422
    error_code = "id_position_off_page"
    message = "The sheet code position lies outside the page."


class IdPositionOverlapsCellError(SheetApiError):
    """The sheet code's minimal box (4 x 10 mm) at the chosen position covers a label cell."""

    status = 422
    error_code = "id_position_overlaps_cell"
    message = "The sheet code position covers a label cell."


class BackStripDoesNotFitError(SheetApiError):
    """No free strip of the backing design takes the 22 x 62 mm code strip (R-350-3)."""

    status = 422
    error_code = "back_strip_does_not_fit"
    message = "The sheet code does not fit any free strip on the back of this sheet."


class MarginIdDoesNotFitError(SheetApiError):
    """No margin band of the sheet is deep enough for the margin ID on this printer (OC-46)."""

    status = 422
    error_code = "margin_id_does_not_fit"
    message = "The sheet code does not fit the margin of this sheet on this printer."


class IdPlacementMismatchError(SheetApiError):
    """The sheet's code does not go on the back side (R-350-6, R-350-11)."""

    status = 409
    error_code = "id_placement_mismatch"
    message = "The sheet code of this sheet does not go on the back side."


class TrayLoadingAlreadySetError(SheetApiError):
    """A print-scope key may set a tray's loading only while it is unset (R-350-5)."""

    status = 409
    error_code = "tray_loading_already_set"
    message = "How this tray takes paper is already set; an admin can change it."
