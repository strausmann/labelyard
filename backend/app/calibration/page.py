# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The printable calibration page (spec 13.2, design 7.2): a test grid the user measures.

The page shows the sheet type's cut lines with a ruler and a millimetre scale at every crossing
of four labels. The user prints it on the real paper, reads how far the ink landed from each
crossing, and enters the readings; ``fit`` turns them into the printer's deviation.

Where everything goes is decided by ``layout.check_page_layout`` inside the printer's printable
area; this module turns a payload into that layout, the texts of the two margin blocks and the
run codes, and draws them. The routes build the payload with ``build_payload`` and then both the
job and the PDF download call ``render_calibration_page`` on it, so they cannot drift apart
(OC-35). No sheet record is created or touched.

A page that belongs to a run (``detail.run_no``, set at submit) carries the run's ``LYC1`` codes:
up to four page codes and a code in every cell that holds one. A downloaded PDF belongs to no run
and carries no codes; its cell numbers and margin blocks are the same.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import UUID

import qrcode
import qrcode.util
from PIL import Image

from app.calibration import codes
from app.calibration.layout import (
    BLOCK_PT,
    CELL_NUMBER_PT,
    CHECKERBOARD_SENTENCE,
    PageLayout,
    PrintableArea,
    bottom_line_count,
    check_page_layout,
    correction_line,
    geometry_line,
    needs_checkerboard,
    printable_area,
    run_line,
    scale_segments,
    type_line,
)
from app.calibration.scale import LINE_MM
from app.page_render.pdf import PageImage, VectorPen, text_width_mm, write_pdf
from app.page_render.transform import IDENTITY
from app.services.page_jobs import PagePayload, snapshot_to_deviation
from app.sheets.errors import CalibrationGridTooLargeError, CalibrationTypeUnsuitableError
from app.sheets.geometry import Geometry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CHECKERBOARD_SENTENCE",
    "MAX_CALIBRATION_CELLS",
    "block_lines",
    "build_payload",
    "check_run_codes",
    "draw_calibration_page",
    "fit_type_line",
    "payload_layout",
    "render_calibration_page",
    "scale_segments",
]

#: Most cells (columns x rows) a calibration page draws; a bigger grid is refused (#358).
#: A second line of defence: every API, import and seed path already caps a type at
#: ``geometry.MAX_CELLS`` (400) in ``validate_geometry``; this bound covers rows stored past it.
MAX_CALIBRATION_CELLS: Final = 2500


def today_iso(time_zone: str = "UTC") -> str:
    """The hub's date in ``time_zone`` (the setting ``general.time_zone``, the same zone as the
    run time); the routes put it into the payload, the renderer never reads it."""
    from app.calibration.runs import hub_zone

    return datetime.now(hub_zone(time_zone)).date().isoformat()


# ---------------------------------------------------------------------------- margin block texts


def _short(text: str, limit: int) -> str:
    if limit >= len(text):
        return text
    return text[: limit - 3] + "..." if limit > 3 else text[: max(limit, 0)]


def fit_type_line(
    type_key: str,
    version: int,
    printer_name: str,
    tray: str,
    *,
    room_mm: float,
    size_pt: float = BLOCK_PT,
) -> str:
    """The second line of the top block, shortened until it fits ``room_mm``.

    Measured with the embedded font. The printer name gives way first (down to nothing), then the
    type key, then the tray; the version never shortens. Raises ``ValueError`` when even the bare
    line is too wide.
    """
    fields = [printer_name, type_key, tray]
    keep = [len(f) for f in fields]

    def build() -> str:
        n, k, t = (_short(f, m) for f, m in zip(fields, keep, strict=True))
        return type_line(k, version, n, t)

    for i in range(3):
        while text_width_mm(build(), size_pt) > room_mm and keep[i] > 0:
            keep[i] = min(keep[i], len(fields[i])) - 1
    line = build()
    if text_width_mm(line, size_pt) > room_mm:
        raise ValueError("margin block: the fixed part alone does not fit the printable area")
    return line


def _fixed(line: str, room_mm: float, size_pt: float) -> str:
    if text_width_mm(line, size_pt) > room_mm:
        raise ValueError("margin block: the fixed part alone does not fit the printable area")
    return line


def bottom_lines_of(payload: PagePayload) -> int:
    """How many lines the payload's bottom block draws (``layout.bottom_line_count``)."""
    d = payload.detail
    g = Geometry(**d["geometry"])
    return bottom_line_count(
        geometry_diff=geometry_line(d.get("geometry_diff") or {}) is not None,
        checkerboard=needs_checkerboard(g),
    )


def block_lines(payload: PagePayload, layout: PageLayout) -> dict[str, list[str]]:
    """The lines of each margin block the layout holds, fitted to its box and size; a block the
    margin had no room for has none (``layout.margin_text``)."""
    d = payload.detail
    corrected = bool(d["corrected"])
    out: dict[str, list[str]] = {}
    top = layout.block("top")
    if top is not None:
        room, pt = top.box.w / 100, top.size_pt
        run = run_line(d.get("run_no"), d["date"], d.get("time"), corrected=corrected)
        out["top"] = [
            _fixed(run, room, pt),
            fit_type_line(
                d["type_key"], d["version"], d["printer_name"], payload.tray, room_mm=room,
                size_pt=pt,
            ),
        ]  # fmt: skip
    bottom = layout.block("bottom")
    if bottom is not None:
        room, pt = bottom.box.w / 100, bottom.size_pt
        dev = snapshot_to_deviation(payload.calibration) if corrected else None
        lines = [_fixed(correction_line(dev), room, pt)]
        geometry = geometry_line(d.get("geometry_diff") or {})
        if geometry is not None:
            lines.append(_fixed(geometry, room, pt))
        if layout.checkerboard:
            lines.append(_fixed(CHECKERBOARD_SENTENCE, room, pt))
        out["bottom"] = lines
    return out


# ---------------------------------------------------------------------------- layout of a payload


def payload_area(payload: PagePayload) -> PrintableArea:
    """The printable area stored at build time; a payload from before 1.8.0 assumes 5 mm."""
    d = payload.detail
    stored = d.get("printable_area")
    if isinstance(stored, Mapping):
        return PrintableArea(**{k: int(stored[k]) for k in ("left", "top", "right", "bottom")})
    return printable_area(Geometry(**d["geometry"]), None, IDENTITY)


def payload_layout(payload: PagePayload, *, passes: int = 1) -> PageLayout:
    d = payload.detail
    g = Geometry(**d["geometry"])
    if g.cols < 2 or g.rows < 2:
        raise CalibrationTypeUnsuitableError(g.cols, g.rows)
    return check_page_layout(
        g,
        payload_area(payload),
        allow_qr=bool(d.get("allow_qr", True)),
        passes=passes,
        bottom_lines=bottom_lines_of(payload),
    )


# ---------------------------------------------------------------------------- run codes


def _qr_image(payload: str, version: int, border: int) -> Image.Image:
    """One pixel per module, mode ``"1"``; the PDF scales it to the drawn size exactly."""
    qr = qrcode.QRCode(version=version, error_correction=codes.ECC, border=border)
    qr.add_data(qrcode.util.QRData(payload, mode=qrcode.util.MODE_ALPHA_NUM))
    qr.make(fit=False)
    matrix = qr.get_matrix()
    side = len(matrix)
    flat = bytes(0 if dark else 255 for row in matrix for dark in row)
    return Image.frombytes("L", (side, side), flat).convert("1")


def _page_code(payload: PagePayload) -> str | None:
    """The page code of a payload that belongs to a run; ``None`` without a run or prefix."""
    d = payload.detail
    run_no, prefix = d.get("run_no"), d.get("prefix")
    if not isinstance(run_no, int) or not isinstance(prefix, str):
        return None
    corrected = bool(d["corrected"])
    dev = snapshot_to_deviation(payload.calibration) if corrected else IDENTITY
    return codes.page_payload(
        run_no=run_no,
        prefix=prefix,
        version=int(d["version"]),
        dev=dev,
        check=corrected,
        geometry_diff=cast(dict[codes.GeometryField, int], d.get("geometry_diff") or {}),
    )


def check_run_codes(payload: PagePayload) -> None:
    """Refuse a stamped payload whose page code does not fit its symbol (never truncated):
    ``CalibrationQrTooLargeError``, 422 ``calibration_qr_too_large``."""
    page_code = _page_code(payload)
    if page_code is not None:
        codes.page_qr_version(page_code)


def _code_images(payload: PagePayload, layout: PageLayout) -> list[PageImage]:
    run_no = payload.detail.get("run_no")
    if not isinstance(run_no, int):
        return []
    images: list[PageImage] = []
    page_code = _page_code(payload)
    if page_code is not None:
        symbol = _qr_image(
            page_code, codes.page_qr_version(page_code), 0
        )  # the quiet zone lies outside the 10 mm
        for spot in layout.page_codes:
            b = spot.box
            images.append(PageImage(round(b.x), round(b.y), round(b.w), round(b.h), symbol))
    for cell in layout.cell_codes:
        text = codes.cell_payload(run_no, cell.cell)
        cell_version = codes.cell_qr_version(text)
        if cell_version is None:  # a cell code that does not fit is left out, never cut
            continue
        b = cell.box
        images.append(
            PageImage(
                round(b.x), round(b.y), round(b.w), round(b.h),
                _qr_image(text, cell_version, codes.QUIET_MODULES),
            )
        )  # fmt: skip
    return images


# ---------------------------------------------------------------------------- drawing


def draw_calibration_page(
    pen: VectorPen, layout: PageLayout, *, lines: Mapping[str, Sequence[str]]
) -> None:
    """Draw ``layout`` (everything in black, 0.1 mm lines) with the lines of its blocks, each on
    the block's lowest baselines."""
    for mark in layout.marks:
        for x1, y1, x2, y2 in mark.segs:
            pen.line(x1, y1, x2, y2, width_mm=LINE_MM, dash=mark.dash)
    for t in layout.labels:
        pen.text(t.x, t.y, t.text, size_pt=t.size_pt, anchor=t.anchor)
    for n in layout.cell_numbers:
        pen.text(n.x, n.y, str(n.cell), size_pt=CELL_NUMBER_PT, anchor=n.anchor)
    for block in layout.margin_blocks:
        shown = lines.get(block.edge, ())
        cx = (block.box.x + block.box.w / 2) / 100
        for y, line in zip(block.baselines_for(len(shown)), shown, strict=True):
            pen.text(cx, y, line, size_pt=block.size_pt, anchor="middle")


def render_calibration_page(payload: PagePayload, *, compress: bool = True) -> bytes:
    """The PDF of a ``calibration_page`` payload. Used by the job and by the download alike."""
    corrected = bool(payload.detail["corrected"])
    layout = payload_layout(payload)
    images = _code_images(payload, layout)  # first: a code too large is refused as such
    lines = block_lines(payload, layout)
    return write_pdf(
        payload.media,
        images=images,
        vector=lambda pen: draw_calibration_page(pen, layout, lines=lines),
        correction=snapshot_to_deviation(payload.calibration if corrected else None),
        compress=compress,
        title="Calibration page",
    )


# ---------------------------------------------------------------------------- payload


async def build_payload(
    session: AsyncSession,
    printer_id: UUID,
    tray: str,
    *,
    type_key: str,
    version: int | None,
    corrected: bool,
    time_zone: str = "UTC",
) -> PagePayload:
    """Validate printer, tray and type and build the payload; the only place that does.

    The printable area (the printer's hardware margins, ``PrinterFacts.margins_hmm``, and the
    tray's stored offset) is computed here and travels in ``detail``, so a retry draws the same
    page. Raises the ``SheetApiError``s of the calibration service and the type service, and
    ``CalibrationTypeUnsuitableError`` for a type without crossings and
    ``CalibrationGridTooLargeError`` for more than ``MAX_CALIBRATION_CELLS`` cells.
    """
    from app.calibration import service
    from app.models.printer import Printer
    from app.sheets import type_service as ts
    from app.sheets.printer_facts import load_printer_facts

    # Kind and tray are checked; the entry goes with the snapshot (design 7.5, S7).
    snapshot, history_id = await service.snapshot_with_history(session, printer_id, tray)
    printer = await session.get(Printer, printer_id)
    assert printer is not None  # snapshot() found it
    sheet_type = await ts.get_type(session, type_key)
    if version is None:
        v = await ts.current_version(session, sheet_type.id)
    else:
        v = await ts.get_version(session, type_key, version)
    g = ts.version_geometry(v)
    if g.cols < 2 or g.rows < 2:
        raise CalibrationTypeUnsuitableError(g.cols, g.rows)
    if g.cols * g.rows > MAX_CALIBRATION_CELLS:
        raise CalibrationGridTooLargeError(g.cols * g.rows, MAX_CALIBRATION_CELLS)
    facts = await load_printer_facts(session, printer_id)
    # The tray's offset moves the ink whether or not the page applies the correction.
    tray_dev = snapshot_to_deviation(snapshot)
    area = printable_area(g, facts.margins_hmm, tray_dev, applied=tray_dev if corrected else None)
    detail: dict[str, Any] = {
        "type_key": sheet_type.key,
        "version": v.version,
        "printer_name": printer.name,
        "date": today_iso(time_zone),
        "corrected": corrected,
        "geometry": asdict(g),
        "prefix": sheet_type.prefix,
        "allow_qr": bool(sheet_type.allow_qr),
        "geometry_diff": await codes.catalog_geometry_diff(session, v),
        "printable_area": asdict(area),
    }
    return PagePayload(
        kind="calibration_page",
        media=g.page_size,
        tray=tray,
        render_dpi=600,
        job_name=f"Calibration page · {sheet_type.key} v{v.version}",
        calibration=snapshot if corrected else None,
        calibration_history_id=history_id if corrected else None,
        detail=detail,
    )
