# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The 1.8.0 calibration page read back from the PDF (#240, design 7.2): every element inside the
printable area, the page codes as the layout says, the run in the margin blocks."""

from __future__ import annotations

from dataclasses import asdict
from functools import cache
from typing import Any

import pytest
from app.calibration import codes
from app.calibration.layout import PrintableArea, check_page_layout, printable_area
from app.calibration.page import check_run_codes, payload_layout, render_calibration_page
from app.catalog import loader
from app.page_render.pdf import PT_PER_MM, text_width_mm
from app.page_render.transform import IDENTITY, Deviation
from app.printer_backends.ipp.attributes import Margins
from app.services.page_jobs import CalibrationSnapshot, PagePayload
from app.sheets.errors import CalibrationQrTooLargeError
from app.sheets.exchange import geometry_from_doc
from app.sheets.geometry import Geometry

from tests.helpers import pdf_inspect as pi

HW_HMM = (300, 500, 800)
_TOL = 0.01  # the PDF writes coordinates to a few decimals


@cache
def _catalog() -> tuple[tuple[str, str, Geometry, bool], ...]:
    out = []
    for entry in loader.load_index():
        doc = loader.load_entry(entry.key)
        g = geometry_from_doc(doc.version.geometry)
        if g.cols >= 2 and g.rows >= 2:
            out.append((entry.key, doc.type.prefix, g, doc.type.allow_qr))
    return tuple(out)


def _cases() -> list[tuple[str, int]]:
    return [(key, hw) for key, _, _, _ in _catalog() for hw in HW_HMM]


def _payload(
    key: str,
    area: PrintableArea,
    *,
    run_no: int | None = 7,
    snapshot: CalibrationSnapshot | None = None,
    **extra: Any,
) -> PagePayload:
    [(prefix, g, qr)] = [(p, g, qr) for k, p, g, qr in _catalog() if k == key]
    detail: dict[str, Any] = {
        "type_key": key,
        "version": 1,
        "printer_name": "Office",
        "date": "2026-11-25",
        "corrected": snapshot is not None,
        "geometry": asdict(g),
        "prefix": prefix,
        "allow_qr": qr,
        "geometry_diff": {},
        "printable_area": asdict(area),
        **extra,
    }
    if run_no is not None:
        detail |= {
            "run_no": run_no,
            "time": "14:05",
            "run_id": "00000000-0000-0000-0000-0000000000aa",
        }
    return PagePayload(
        kind="calibration_page",
        media=g.page_size,
        tray="tray-1",
        render_dpi=600,
        job_name="Calibration page",
        calibration=snapshot,
        detail=detail,
    )


def _geometry(key: str) -> Geometry:
    return next(g for k, _, g, _ in _catalog() if k == key)


def _hw(hmm: int) -> Margins:
    return Margins(top=hmm, left=hmm, bottom=hmm, right=hmm)


# ---------------------------------------------------------------------------- reading the PDF


def _drawn(pdf: bytes, page_h_mm: float) -> list[tuple[float, float, float, float]]:
    """Every drawn element as a box in page mm (y down) *before* the page's correction ``cm``:
    strokes (with their width), texts (an em above the baseline, a quarter below) and images."""
    boxes: list[tuple[float, float, float, float]] = []
    width = 0.0
    pts: list[tuple[float, float]] = []
    size = 0.0
    sizes: list[float] = []
    for op, a in pi.operations(pdf):
        if op == "w":
            width = a[0] / PT_PER_MM
        elif op in ("m", "l"):
            pts.append((a[0] / PT_PER_MM, page_h_mm - a[1] / PT_PER_MM))
        elif op == "S":
            h = width / 2
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            boxes.append((min(xs) - h, min(ys) - h, max(xs) + h, max(ys) + h))
            pts = []
        elif op == "Tf":
            size = a[-1]
        elif op == "Tj":
            sizes.append(size)
    cm = _correction(pdf)
    texts, positions = pi.texts(pdf), pi.text_positions(pdf)
    for text, (x_pt, y_pt), pt in zip(texts, positions, sizes, strict=True):
        if cm:  # text_positions applied the correction; undo it to compare with the layout
            x_pt, y_pt = _apply(_invert(cm), x_pt, y_pt)
        x, y = x_pt / PT_PER_MM, page_h_mm - y_pt / PT_PER_MM
        em = pt * 25.4 / 72
        boxes.append((x, y - em, x + text_width_mm(text, pt), y + em * 0.25))
    for img in pi.placed_images(pdf):
        x0, y0 = img.x_pt, img.y_pt
        x1, y1 = x0 + img.w_pt, y0 + img.h_pt
        if cm:
            x0, y0 = _apply(_invert(cm), x0, y0)
            x1, y1 = _apply(_invert(cm), x1, y1)
        boxes.append((x0 / PT_PER_MM, page_h_mm - y1 / PT_PER_MM, x1 / PT_PER_MM,
                      page_h_mm - y0 / PT_PER_MM))  # fmt: skip
    return boxes


def _correction(pdf: bytes) -> tuple[float, ...] | None:
    """The page's correction ``cm``: the one outside every ``q``/``Q`` (an image's placement
    ``cm`` lies inside one)."""
    depth = 0
    for op, a in pi.operations(pdf):
        if op == "q":
            depth += 1
        elif op == "Q":
            depth -= 1
        elif op == "cm" and depth == 0:
            return tuple(a)
    return None


def _apply(m: tuple[float, ...], x: float, y: float) -> tuple[float, float]:
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def _invert(m: tuple[float, ...]) -> tuple[float, ...]:
    a, b, c, d, e, f = m
    det = a * d - b * c
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return ia, ib, ic, id_, -(e * ia + f * ic), -(e * ib + f * id_)


def _strokes_on_paper(pdf: bytes, page_h_mm: float) -> list[tuple[float, float]]:
    """Every stroke end point after the page's ``cm`` (where the ink goes before the printer's
    own deviation), in page mm."""
    cm = _correction(pdf) or (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    out = []
    for op, a in pi.operations(pdf):
        if op in ("m", "l"):
            x, y = _apply(cm, a[0], a[1])
            out.append((x / PT_PER_MM, page_h_mm - y / PT_PER_MM))
    return out


def _inside(box: tuple[float, ...], area: tuple[float, ...]) -> bool:
    return (
        box[0] >= area[0] - _TOL
        and box[1] >= area[1] - _TOL
        and box[2] <= area[2] + _TOL
        and box[3] <= area[3] + _TOL
    )


# ---------------------------------------------------------------------------- the area


@pytest.mark.parametrize(("key", "hw"), _cases())
def test_every_drawn_element_lies_inside_the_printable_area(key: str, hw: int) -> None:
    g = _geometry(key)
    area = printable_area(g, _hw(hw), IDENTITY)
    pdf = render_calibration_page(_payload(key, area), compress=False)
    outside = [b for b in _drawn(pdf, g.page_h / 100) if not _inside(b, area.rect_mm())]
    assert outside == [], (key, hw, outside[:5])


@pytest.mark.parametrize("key", ["avery-zweckform-6121", "avery-5160", "herma-4103"])
def test_a_corrected_page_lies_inside_the_area_and_lands_inside_the_hardware_margin(
    key: str,
) -> None:
    g = _geometry(key)
    snap = CalibrationSnapshot(method="manual", dx_hmm=180, dy_hmm=-240, sx_ppm=1_000_500)
    dev = Deviation(180, -240, 1_000_500, 1_000_000, 0)
    area = printable_area(g, _hw(500), dev)
    pdf = render_calibration_page(_payload(key, area, snapshot=snap), compress=False)
    assert _correction(pdf) is not None  # the correction is applied
    # In layout coordinates (before the cm) everything is inside the area ...
    outside = [b for b in _drawn(pdf, g.page_h / 100) if not _inside(b, area.rect_mm())]
    assert outside == []
    # ... and the correction moves it by at most |offset| plus the scale: inside the page less
    # the hardware margin.
    page = (5.0, 5.0, g.page_w / 100 - 5.0, g.page_h / 100 - 5.0)
    off = [p for p in _strokes_on_paper(pdf, g.page_h / 100) if not _inside((*p, *p), page)]
    assert off == []


# ---------------------------------------------------------------------------- the codes


def _sized(pdf: bytes, mm: float) -> list[pi.PlacedImage]:
    return [i for i in pi.placed_images(pdf) if abs(i.w_pt / PT_PER_MM - mm) < 0.02]


@pytest.mark.parametrize(("key", "hw"), _cases())
def test_page_code_count_equals_the_layout_summary(key: str, hw: int) -> None:
    g = _geometry(key)
    payload = _payload(key, printable_area(g, _hw(hw), IDENTITY))
    layout = payload_layout(payload)
    pdf = render_calibration_page(payload, compress=False)
    assert len(_sized(pdf, 10.0)) == len(layout.page_codes)
    assert len(_sized(pdf, 8.0)) == len(layout.cell_codes)


def test_the_page_code_is_version_5_with_modules_of_at_least_0_25_mm() -> None:
    key = "avery-zweckform-6121"
    g = _geometry(key)
    pdf = render_calibration_page(
        _payload(key, printable_area(g, _hw(500), IDENTITY)), compress=False
    )
    page_codes = _sized(pdf, 10.0)
    assert len(page_codes) == 4
    for img in page_codes:
        assert img.image.width == 17 + 4 * codes.PAGE_MIN_VERSION == 37  # no quiet zone inside
        assert img.mm_per_pixel >= codes.MIN_MODULE_HMM / 100
    cell_codes = _sized(pdf, 8.0)
    assert len(cell_codes) == 65
    assert all(img.image.width == 21 + 2 * codes.QUIET_MODULES for img in cell_codes)
    assert all(img.mm_per_pixel >= codes.MIN_MODULE_HMM / 100 for img in cell_codes)


def test_a_downloaded_pdf_carries_no_codes_but_every_cell_number() -> None:
    key = "avery-zweckform-6121"
    g = _geometry(key)
    pdf = render_calibration_page(
        _payload(key, printable_area(g, _hw(500), IDENTITY), run_no=None), compress=False
    )
    assert pi.placed_images(pdf) == []
    texts = pi.texts(pdf)
    assert {str(n) for n in range(1, 66)} <= set(texts)
    assert any(t.startswith("No run (downloaded PDF) · 2026-11-25") for t in texts)


def test_the_run_and_its_time_open_the_top_block_and_the_correction_closes_the_page() -> None:
    key = "avery-5160"
    g = _geometry(key)
    snap = CalibrationSnapshot(method="manual", dx_hmm=120, dy_hmm=-40, sx_ppm=1_001_000)
    pdf = render_calibration_page(
        _payload(key, printable_area(g, _hw(500), IDENTITY), snapshot=snap,
                 geometry_diff={"PX": 6700}),
        compress=False,
    )  # fmt: skip
    texts = pi.texts(pdf)
    assert "Run #7 · 2026-11-25 14:05 · Calibration page — check print with correction" in texts
    assert "avery-5160 v1 · Office · tray tray-1" in texts
    assert (
        "Correction applied: dx 1.20 mm · dy -0.40 mm · scale x 1.001000 · scale y 1.000000 · "
        "rotation 0.000000°"
    ) in texts
    assert "Not as shipped: pitch x 67.00 mm" in texts


def test_check_run_codes_refuses_a_page_code_that_does_not_fit_and_never_cuts_it() -> None:
    key = "avery-5160"
    g = _geometry(key)
    payload = _payload(key, printable_area(g, _hw(500), IDENTITY), run_no=10**110)
    with pytest.raises(CalibrationQrTooLargeError) as e:
        check_run_codes(payload)
    assert e.value.error_code == "calibration_qr_too_large" and e.value.status == 422
    with pytest.raises(CalibrationQrTooLargeError):  # the renderer refuses it as well
        render_calibration_page(payload)
    check_run_codes(_payload(key, printable_area(g, _hw(500), IDENTITY), run_no=999_999))
    check_run_codes(_payload(key, printable_area(g, _hw(500), IDENTITY), run_no=None))


def test_a_payload_from_before_1_8_0_renders_with_5_mm_and_without_codes() -> None:
    key = "avery-5160"
    g = _geometry(key)
    payload = _payload(key, printable_area(g, _hw(500), IDENTITY), run_no=None)
    for k in ("prefix", "allow_qr", "geometry_diff", "printable_area"):
        del payload.detail[k]
    layout = payload_layout(payload)
    assert layout.area == printable_area(g, None, IDENTITY)
    pdf = render_calibration_page(payload, compress=False)
    assert pi.placed_images(pdf) == []
    assert layout == check_page_layout(g, layout.area, allow_qr=True, passes=1)
