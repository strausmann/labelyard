# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The probe print and the measurement sheet on plain paper (#241, #246, design 7.3).

A probe page draws a sheet type as it would land on the paper: the outline of every label
(rectangles, rounded rectangles or circles), each cell's number and ``R<row> C<col>``, the
preprint zones hatched and the planned sheet-ID rectangle outlined, the type key and version,
and the run number and time. The tray's **current** calibration is applied, so the operator holds
the printed plain sheet against a real label sheet and sees whether the two line up. No sheet,
reservation or cell is created or touched; the page is a queued page job of kind
``probe_page``, held while the device behind the printer is flagged as swapped.

The **measurement sheet** (``grid``) is the same page plus a millimetre grid over the whole page
(lines every 1 mm, heavier every 5 mm, labelled every 10 mm, origin at the page's top-left
corner), drawn **without** calibration so its coordinates are the page's own. Printed on a real
sheet and photographed, it gives the vendor's preprint in page millimetres.

``build_probe_payload`` is the only place that reads the database; the job and the PDF download
both call ``render_probe_page`` on its payload, so they cannot drift apart. Everything the
renderer needs (geometry, zones, the ID rectangle, the printable area) travels in ``detail``, so
a retry draws the same page.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from app.calibration.layout import PrintableArea, correction_line, printable_area
from app.page_render.pdf import VectorPen, text_width_mm, write_pdf
from app.page_render.transform import page_size_mm
from app.services.page_jobs import PagePayload, snapshot_to_deviation
from app.sheets.free_rects import Rect
from app.sheets.geometry import Geometry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "GRID_FOOTER",
    "PROBE_KIND",
    "build_probe_payload",
    "draw_probe_page",
    "fresh_offline",
    "hatch_segments",
    "header_lines",
    "render_probe_page",
]

PROBE_KIND: Final = "probe_page"
GRID_FOOTER: Final = "Measurement sheet — no correction"
"""The footer of the measurement sheet: its grid is the page's own coordinates."""

OUTLINE_MM: Final = 0.2
"""Line width of a label outline and a zone outline."""
ID_OUTLINE_MM: Final = 0.3
ID_DASH: Final = (1.2, 0.6)
HATCH_STEP_MM: Final = 1.5
"""Distance between two hatch lines of a preprint zone, measured along the x axis."""
HATCH_MM: Final = 0.1
GRID_THIN_MM: Final = 0.05
GRID_HEAVY_MM: Final = 0.15
GRID_LABEL_PT: Final = 3.0
CELL_PT: Final = 5.0
HEADER_PT: Final = 6.0
_LEADING: Final = 1.15
_PT_MM: Final = 25.4 / 72
_GAP_MM: Final = 1.0
"""Space kept between a header line and the label grid."""


# ---------------------------------------------------------------------------- payload


def _rect_dict(r: Rect) -> dict[str, int]:
    return {"x": r.x, "y": r.y, "w": r.w, "h": r.h}


async def build_probe_payload(
    session: AsyncSession,
    printer_id: UUID,
    tray: str,
    *,
    type_key: str,
    version: int | None,
    grid: bool,
    time_zone: str,
) -> PagePayload:
    """Validate printer, tray and type and build the payload of a probe page.

    The tray's current calibration goes into the payload (the page is drawn with it), except for
    the measurement sheet (``grid``), which is drawn without any correction. The ID rectangle is
    the margin placement a new sheet of the type would get on this printer and tray (the one
    placement function, spec 5.2); ``None`` when the type puts its code elsewhere or there is no
    room. Raises the ``SheetApiError``s of the calibration and type services.
    """
    from app.calibration import service
    from app.calibration.page import MAX_CALIBRATION_CELLS, today_iso
    from app.config import get_settings
    from app.models.printer import Printer
    from app.sheets import sheet_service as ss
    from app.sheets import type_service as ts
    from app.sheets.errors import CalibrationGridTooLargeError
    from app.sheets.id_placement import resolve_default_placement
    from app.sheets.naming import sheet_type_label
    from app.sheets.planner import margin_id_text, margin_placement_with_reason
    from app.sheets.preprint_service import margin_rules
    from app.sheets.print_service import scan_payload_builder
    from app.sheets.printer_facts import load_printer_facts

    stored, stored_history = await service.snapshot_with_history(session, printer_id, tray)
    # An uncalibrated (or reset) tray: the page goes out without a correction.
    snapshot = None if stored.method == "none" else stored
    history_id = None if snapshot is None else stored_history
    printer = await session.get(Printer, printer_id)
    assert printer is not None  # snapshot_with_history() found it
    sheet_type = await ts.get_type(session, type_key)
    if version is None:
        v = await ts.current_version(session, sheet_type.id)
    else:
        v = await ts.get_version(session, type_key, version)
    g = ts.version_geometry(v)
    if g.cols * g.rows > MAX_CALIBRATION_CELLS:
        raise CalibrationGridTooLargeError(g.cols * g.rows, MAX_CALIBRATION_CELLS)
    facts = await load_printer_facts(session, printer_id)
    tray_dev = snapshot_to_deviation(snapshot)
    applied = None if grid else tray_dev
    area = printable_area(g, facts.margins_hmm, tray_dev, applied=applied)
    rule = (await margin_rules(session, [sheet_type]))[sheet_type.id]
    code = await ss.peek_next_code(session, sheet_type.key)
    label = sheet_type_label(sheet_type.vendor, sheet_type.product_codes, sheet_type.key)
    placement, _reason = margin_placement_with_reason(
        g,
        hw=facts.margins_hmm,
        dev=tray_dev,
        payload=scan_payload_builder(str(get_settings().public_url))(code),
        dpi=facts.render_dpi,
        zones=rule.zones,
        band_rule=rule.band,
        align=rule.align,
        offset_hmm=rule.offset_hmm,
        text=margin_id_text(code, label),
        code_text=code,
    )
    in_margin = (
        resolve_default_placement(
            sheet_type.id_placement,  # type: ignore[arg-type]
            margin_printable=placement is not None,
        )
        == "margin"
    )
    detail: dict[str, Any] = {
        "type_key": sheet_type.key,
        "version": v.version,
        "printer_name": printer.name,
        "date": today_iso(time_zone),
        "grid": grid,
        "geometry": asdict(g),
        "zones": [_rect_dict(z) for z in rule.zones],
        "id_rect": _rect_dict(placement.rect) if placement is not None and in_margin else None,
        "printable_area": asdict(area),
    }
    title = "Measurement sheet" if grid else "Probe print"
    return PagePayload(
        kind=PROBE_KIND,
        media=g.page_size,
        tray=tray,
        render_dpi=600,
        job_name=f"{title} · {sheet_type.key} v{v.version}",
        calibration=None if grid else snapshot,
        calibration_history_id=None if grid else history_id,
        detail=detail,
    )


# ---------------------------------------------------------------------------- status


async def fresh_offline(
    session: AsyncSession,
    printer_id: UUID,
    *,
    probe_interval_s: float,
    now: datetime | None = None,
) -> bool:
    """Whether the printer's cached status is fresh and says it is offline.

    A stale reading never blocks (design 7.3): a printer that is back must not be refused on an
    old cache row; a printer without any reading is not refused either.
    """
    from app.models.printer_status_cache import PrinterStatusCache
    from app.services.status_freshness import backoff_of, is_stale

    row = await session.get(PrinterStatusCache, printer_id)
    if row is None:
        return False
    parsed = row.parsed or {}
    if parsed.get("online") is not False:
        return False
    return not is_stale(
        row.updated_at, now or datetime.now(UTC), probe_interval_s, backoff_of(parsed, page=True)
    )


# ---------------------------------------------------------------------------- texts


def header_lines(payload: PagePayload) -> list[str]:
    """The page's text lines: what it is, the run, and the correction it is printed with."""
    d = payload.detail
    grid = bool(d.get("grid"))
    title = "Measurement sheet" if grid else "Probe print on plain paper"
    run_no, time = d.get("run_no"), d.get("time")
    run = f"Run #{run_no}" if isinstance(run_no, int) else "No run (downloaded PDF)"
    when = f"{d['date']} {time}" if time else str(d["date"])
    footer = (
        GRID_FOOTER if grid else correction_line(
            snapshot_to_deviation(payload.calibration) if payload.calibration else None
        )
    )  # fmt: skip
    return [
        f"{title} · {d['type_key']} v{d['version']}",
        f"{run} · {when} · {d['printer_name']} · tray {payload.tray}",
        footer,
    ]


# ---------------------------------------------------------------------------- drawing


def _area(payload: PagePayload, g: Geometry) -> PrintableArea:
    stored = payload.detail.get("printable_area")
    if isinstance(stored, Mapping):
        return PrintableArea(**{k: int(stored[k]) for k in ("left", "top", "right", "bottom")})
    return printable_area(g, None, snapshot_to_deviation(payload.calibration))


def _rects(raw: Any) -> list[Rect]:
    if not isinstance(raw, Sequence):
        return []
    return [Rect(int(z["x"]), int(z["y"]), int(z["w"]), int(z["h"])) for z in raw]


def hatch_segments(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, ...]]:
    """45° hatch lines (down to the right) inside the rectangle, every ``HATCH_STEP_MM`` (mm)."""
    segs: list[tuple[float, ...]] = []
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return segs
    # Lines x - y = k, from k = -h to k = w.
    k = -h + HATCH_STEP_MM / 2
    while k < w:
        # Entry: on the top edge (y = 0) if k >= 0, else on the left edge (x = 0).
        sx, sy = (k, 0.0) if k >= 0 else (0.0, -k)
        # Exit: on the right edge (x = w) if w - k <= h, else on the bottom edge (y = h).
        ex, ey = (w, w - k) if w - k <= h else (k + h, h)
        segs.append((x0 + sx, y0 + sy, x0 + ex, y0 + ey))
        k += HATCH_STEP_MM
    return segs


def _draw_grid(pen: VectorPen, page: str, area: PrintableArea) -> None:
    w_mm, h_mm = page_size_mm(page)
    for x in range(int(w_mm) + 1):
        heavy = x % 5 == 0
        pen.line(x, 0, x, h_mm, width_mm=GRID_HEAVY_MM if heavy else GRID_THIN_MM)
    for y in range(int(h_mm) + 1):
        heavy = y % 5 == 0
        pen.line(0, y, w_mm, y, width_mm=GRID_HEAVY_MM if heavy else GRID_THIN_MM)
    left, top, right, bottom = area.rect_mm()
    for x in range(10, int(w_mm), 10):
        if left <= x <= right:
            pen.text(x + 0.3, top + 1.2, str(x), size_pt=GRID_LABEL_PT)
    for y in range(10, int(h_mm), 10):
        if top <= y <= bottom:
            pen.text(left + 0.3, y - 0.3, str(y), size_pt=GRID_LABEL_PT)


def _draw_cells(pen: VectorPen, g: Geometry) -> None:
    lw, lh = g.label_w / 100, g.label_h / 100
    radius = (g.corner_radius or 0) / 100
    line = CELL_PT * _PT_MM * _LEADING
    for r in range(g.rows):
        for c in range(g.cols):
            x = (g.margin_left + c * g.pitch_x) / 100
            y = (g.margin_top + r * g.pitch_y) / 100
            if g.shape == "round":
                pen.circle(x + lw / 2, y + lh / 2, lw / 2, width_mm=OUTLINE_MM)
            elif radius > 0:
                pen.round_rect(x, y, lw, lh, radius, width_mm=OUTLINE_MM)
            else:
                pen.rect(x, y, lw, lh, width_mm=OUTLINE_MM)
            number, where = str(r * g.cols + c + 1), f"R{r + 1} C{c + 1}"
            cx, cy = x + lw / 2, y + lh / 2
            room = lw * (0.7 if g.shape == "round" else 0.9)
            if lh >= 2 * line + 1 and text_width_mm(where, CELL_PT) <= room:
                pen.text(cx, cy - 0.1, number, size_pt=CELL_PT, anchor="middle")
                pen.text(cx, cy - 0.1 + line, where, size_pt=CELL_PT, anchor="middle")
            elif lh >= line + 0.5 and text_width_mm(number, CELL_PT) <= room:
                pen.text(cx, cy + line / 3, number, size_pt=CELL_PT, anchor="middle")


def _draw_zones(pen: VectorPen, zones: Sequence[Rect]) -> None:
    for z in zones:
        x0, y0, x1, y1 = z.x / 100, z.y / 100, z.x2 / 100, z.y2 / 100
        pen.rect(x0, y0, x1 - x0, y1 - y0, width_mm=OUTLINE_MM)
        for sx, sy, ex, ey in hatch_segments(x0, y0, x1, y1):
            pen.line(sx, sy, ex, ey, width_mm=HATCH_MM)


def _draw_id_rect(pen: VectorPen, r: Rect) -> None:
    x0, y0, x1, y1 = r.x / 100, r.y / 100, r.x2 / 100, r.y2 / 100
    for seg in ((x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)):
        pen.line(*seg, width_mm=ID_OUTLINE_MM, dash=ID_DASH)


def _text_band(g: Geometry, area: PrintableArea, count: int) -> list[float]:
    """Baselines for ``count`` header lines: the top band when it holds them all, else the
    bottom band, else as many as the deeper band holds. Never across the labels."""
    _left, top, _right, bottom = area.rect_mm()
    grid_top = g.margin_top / 100
    grid_bottom = (g.margin_top + (g.rows - 1) * g.pitch_y + g.label_h) / 100
    step = HEADER_PT * _PT_MM * _LEADING
    bands = [(top, grid_top - _GAP_MM), (grid_bottom + _GAP_MM, bottom)]
    fits = [max(0, int((b - a) // step)) for a, b in bands]
    pick = 0 if fits[0] >= count else 1 if fits[1] >= count else (0 if fits[0] >= fits[1] else 1)
    a, _ = bands[pick]
    shown = min(count, fits[pick])
    return [a + step * (i + 1) - 0.4 for i in range(shown)]


def draw_probe_page(pen: VectorPen, payload: PagePayload) -> None:
    """Draw the probe page (black only): the grid first, then labels, zones, the ID and texts."""
    d = payload.detail
    g = Geometry(**d["geometry"])
    area = _area(payload, g)
    if d.get("grid"):
        _draw_grid(pen, payload.media, area)
    _draw_cells(pen, g)
    _draw_zones(pen, _rects(d.get("zones")))
    id_rect = d.get("id_rect")
    if isinstance(id_rect, Mapping):
        _draw_id_rect(pen, _rects([id_rect])[0])
    lines = header_lines(payload)
    cx = (area.left + area.right) / 200
    room = (area.right - area.left) / 100
    for y, text in zip(_text_band(g, area, len(lines)), lines, strict=False):
        if text_width_mm(text, HEADER_PT) <= room:
            pen.text(cx, y, text, size_pt=HEADER_PT, anchor="middle")


def render_probe_page(payload: PagePayload, *, compress: bool = True) -> bytes:
    """The PDF of a ``probe_page`` payload, drawn with the payload's calibration (none for the
    measurement sheet). Used by the job and by the download alike."""
    return write_pdf(
        payload.media,
        vector=lambda pen: draw_probe_page(pen, payload),
        correction=snapshot_to_deviation(payload.calibration),
        compress=compress,
        title="Measurement sheet" if payload.detail.get("grid") else "Probe print",
    )
