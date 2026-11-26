# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The probe page and the measurement sheet (#241, #246, design 7.3), read back from the PDF.

The payload is built by the production builder from a database, the page drawn by the production
renderer, and the coordinates read from the content stream (``pdf_inspect``): the label outlines
where the geometry puts them (through the tray's correction when there is one), the millimetre
grid at whole millimetres from the page corner and never corrected, the zones hatched and the ID
rectangle dashed.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
from uuid import UUID, uuid4

import app.models  # noqa: F401
import pytest
from app.calibration import probe
from app.models.printer import Printer
from app.models.printer_calibration import PrinterCalibration
from app.models.printer_tray import PrinterTray
from app.models.sheet import Sheet, SheetCell, SheetType
from app.page_render.pdf import PT_PER_MM
from app.page_render.transform import Deviation, apply_deviation
from app.services.page_jobs import snapshot_to_deviation
from app.sheets import type_service as ts
from app.sheets.geometry import Geometry
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers import pdf_inspect as pi

pytestmark = pytest.mark.asyncio

Factory = async_sessionmaker[AsyncSession]
# 45.7 x 21.2 mm labels, 4 x 12 on A4.
G48 = Geometry("A4", 21000, 29700, 4570, 2120, 4, 12, 4820, 2120, 2130, 985, None, "rectangle")
ROUND = Geometry("A4", 21000, 29700, 4000, 4000, 4, 6, 4500, 4500, 1500, 1500, None, "round")
PAGE_H_PT = 297 * PT_PER_MM
DEV = Deviation(150, -60, 1_000_500, 999_800, 200_000)
TOL_MM = 0.01


async def _printer(factory: Factory) -> UUID:
    async with factory() as s:
        p = Printer(
            id=uuid4(),
            name="Office",
            slug=f"p-{uuid4().hex[:6]}",
            model="m",
            backend="ipp",
            connection={"uri": "ipp://printer.example.invalid/ipp/print"},
        )
        s.add(p)
        s.add(PrinterTray(printer_id=p.id, tray_key="auto", label="Auto", is_default=True))
        await s.commit()
        return p.id


async def _type(factory: Factory, key: str, g: Geometry = G48, **row: Any) -> None:
    async with factory() as s:
        await ts.create_type(
            s, ts.TypeFields(key, key.replace("-", "").upper()[:8], "Acme", [], None, None), g,
            actor="t",
        )  # fmt: skip
        if row:
            await s.execute(update(SheetType).where(SheetType.key == key).values(**row))
            await s.commit()


async def _calibrate(factory: Factory, printer_id: UUID, dev: Deviation = DEV) -> None:
    async with factory() as s:
        s.add(
            PrinterCalibration(
                printer_id=printer_id,
                tray_key="auto",
                method="manual",
                offset_x_hmm=dev.offset_x_hmm,
                offset_y_hmm=dev.offset_y_hmm,
                scale_x_ppm=dev.scale_x_ppm,
                scale_y_ppm=dev.scale_y_ppm,
                rotation_udeg=dev.rotation_udeg,
            )
        )
        await s.commit()


async def _payload(factory: Factory, pid: UUID, key: str, *, grid: bool = False) -> Any:
    async with factory() as s:
        return await probe.build_probe_payload(
            s, pid, "auto", type_key=key, version=None, grid=grid, time_zone="UTC"
        )


# ---------------------------------------------------------------------------- reading the PDF


def _ops(pdf: bytes) -> list[tuple[str, list[float]]]:
    return pi.operations(pdf)


def _ctm(pdf: bytes) -> tuple[float, ...]:
    cms = pi.cm_matrices(pdf)
    return cms[0] if cms else (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _to_mm(m: tuple[float, ...], x: float, y: float) -> tuple[float, float]:
    """A point of the content stream, through the page's ``cm``, in page mm with y down."""
    px, py = m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]
    return px / PT_PER_MM, (PAGE_H_PT - py) / PT_PER_MM


def _rects_mm(pdf: bytes) -> list[tuple[float, float, float, float]]:
    """Every ``re`` as drawn on the page (x0, y0 top-left corner; x1, y1 bottom-right), mm."""
    m = _ctm(pdf)
    out = []
    for op, a in _ops(pdf):
        if op == "re":
            x, y, w, h = a
            x0, y1 = _to_mm(m, x, y)
            x1, y0 = _to_mm(m, x + w, y + h)
            out.append((x0, y0, x1, y1))
    return out


def _lines(pdf: bytes) -> list[dict[str, Any]]:
    """Every stroked ``m``/``l`` segment in content-stream mm (y down), with its width and dash."""
    out: list[dict[str, Any]] = []
    width, dash = 1.0, False
    stack: list[tuple[float, bool]] = []
    start: tuple[float, float] | None = None
    for op, a in _ops(pdf):
        if op == "q":
            stack.append((width, dash))
        elif op == "Q":
            width, dash = stack.pop()
        elif op == "w":
            width = a[0] / PT_PER_MM
        elif op == "d":
            dash = len(a) > 1
        elif op == "m":
            start = (a[0] / PT_PER_MM, (PAGE_H_PT - a[1]) / PT_PER_MM)
        elif op == "l" and start is not None:
            end = (a[0] / PT_PER_MM, (PAGE_H_PT - a[1]) / PT_PER_MM)
            out.append({"a": start, "b": end, "width": width, "dash": dash})
    return out


def _cells_mm(g: Geometry) -> list[tuple[float, float, float, float]]:
    out = []
    for r in range(g.rows):
        for c in range(g.cols):
            x = (g.margin_left + c * g.pitch_x) / 100
            y = (g.margin_top + r * g.pitch_y) / 100
            out.append((x, y, x + g.label_w / 100, y + g.label_h / 100))
    return out


def _close(a: tuple[float, ...], b: tuple[float, ...], tol: float = TOL_MM) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


# ---------------------------------------------------------------------------- the labels


async def test_probe_cell_rectangles_without_calibration(async_session_factory: Factory) -> None:
    pid = await _printer(async_session_factory)
    await _type(async_session_factory, "probe-plain")
    payload = await _payload(async_session_factory, pid, "probe-plain")
    assert payload.kind == "probe_page" and payload.calibration is None
    pdf = probe.render_probe_page(payload, compress=False)
    assert pi.cm_matrices(pdf) == []  # nothing to correct
    drawn = sorted(_rects_mm(pdf))
    expected = sorted(_cells_mm(G48))
    assert len(drawn) == len(expected) == 48
    for got, want in zip(drawn, expected, strict=True):
        assert _close(got, want), (got, want)
    texts = pi.texts(pdf)
    assert "1" in texts and "R1 C1" in texts and "48" in texts and "R12 C4" in texts
    assert "Probe print on plain paper · probe-plain v1" in texts
    assert "No correction applied" in texts
    assert pi.colour_operators(pdf) == set()


async def test_probe_cell_rectangles_with_calibration(async_session_factory: Factory) -> None:
    """With the tray's correction applied, the printer's deviation puts every outline where the
    geometry says the label is: the page is drawn through the inverse of the deviation."""
    pid = await _printer(async_session_factory)
    await _type(async_session_factory, "probe-cal")
    await _calibrate(async_session_factory, pid)
    payload = await _payload(async_session_factory, pid, "probe-cal")
    assert snapshot_to_deviation(payload.calibration) == DEV
    pdf = probe.render_probe_page(payload, compress=False)
    assert len(pi.cm_matrices(pdf)) == 1
    m = _ctm(pdf)
    corners = []
    for op, a in _ops(pdf):
        if op == "re":
            x, y, w, h = a
            top_left = apply_deviation(DEV, "A4", _to_mm(m, x, y + h))
            bottom_right = apply_deviation(DEV, "A4", _to_mm(m, x + w, y))
            corners.append((*top_left, *bottom_right))
    assert len(corners) == 48
    for got, want in zip(sorted(corners), sorted(_cells_mm(G48)), strict=True):
        assert _close(got, want), (got, want)
    # The plain page differs: the correction really moved the outlines.
    plain = sorted(_rects_mm(pdf))
    assert not all(_close(a, b) for a, b in zip(plain, sorted(_cells_mm(G48)), strict=True))
    assert any(t.startswith("Correction applied: dx 1.50 mm") for t in pi.texts(pdf))


async def test_probe_draws_round_labels_as_circles(async_session_factory: Factory) -> None:
    pid = await _printer(async_session_factory)
    await _type(async_session_factory, "probe-round", ROUND)
    pdf = probe.render_probe_page(
        await _payload(async_session_factory, pid, "probe-round"), compress=False
    )
    assert _rects_mm(pdf) == []
    # reportlab draws a circle as four Bézier curves.
    assert sum(1 for op, _ in _ops(pdf) if op == "c") == 4 * ROUND.cols * ROUND.rows


# ---------------------------------------------------------------------------- the grid


async def test_measurement_grid_at_whole_mm_from_the_corner_and_unaffected_by_correction(
    async_session_factory: Factory,
) -> None:
    pid = await _printer(async_session_factory)
    await _type(async_session_factory, "probe-grid")
    await _calibrate(async_session_factory, pid)  # a stored correction must not reach the grid
    payload = await _payload(async_session_factory, pid, "probe-grid", grid=True)
    assert payload.calibration is None and payload.calibration_history_id is None
    pdf = probe.render_probe_page(payload, compress=False)
    assert pi.cm_matrices(pdf) == []  # drawn in the page's own coordinates
    lines = _lines(pdf)
    vertical = [ln for ln in lines if abs(ln["a"][0] - ln["b"][0]) < TOL_MM
                and abs(ln["a"][1]) < TOL_MM and abs(ln["b"][1] - 297) < TOL_MM]  # fmt: skip
    horizontal = [ln for ln in lines if abs(ln["a"][1] - ln["b"][1]) < TOL_MM
                  and abs(ln["a"][0]) < TOL_MM and abs(ln["b"][0] - 210) < TOL_MM]  # fmt: skip
    xs = sorted(ln["a"][0] for ln in vertical)
    ys = sorted(ln["a"][1] for ln in horizontal)
    assert len(xs) == 211 and len(ys) == 298
    assert all(abs(x - round(x)) < 1e-3 for x in xs) and [round(x) for x in xs] == list(range(211))
    assert all(abs(y - round(y)) < 1e-3 for y in ys) and [round(y) for y in ys] == list(range(298))
    for ln in vertical + horizontal:
        at = round(ln["a"][0] if ln in vertical else ln["a"][1])
        want = probe.GRID_HEAVY_MM if at % 5 == 0 else probe.GRID_THIN_MM
        assert abs(ln["width"] - want) < 1e-6, (at, ln["width"])
    texts = pi.texts(pdf)
    assert {str(v) for v in range(10, 210, 10)} <= set(texts)  # labelled every 10 mm
    assert probe.GRID_FOOTER in texts and "Measurement sheet · probe-grid v1" in texts
    # The labels are drawn too, uncorrected, at the geometry.
    for got, want in zip(sorted(_rects_mm(pdf)), sorted(_cells_mm(G48)), strict=True):
        assert _close(got, want), (got, want)


# ---------------------------------------------------------------------------- zones and the ID


async def test_probe_draws_zones_and_id_rectangle(async_session_factory: Factory) -> None:
    zone = {"x": 1500, "y": 800, "w": 5000, "h": 1000, "note": "logo"}
    pid = await _printer(async_session_factory)
    await _type(
        async_session_factory, "probe-zone", preprint_zones=[zone], id_placement="margin",
        id_margin_band="top",
    )  # fmt: skip
    payload = await _payload(async_session_factory, pid, "probe-zone")
    assert payload.detail["zones"] == [{"x": 1500, "y": 800, "w": 5000, "h": 1000}]
    id_rect = payload.detail["id_rect"]
    assert id_rect is not None and id_rect["y"] + id_rect["h"] <= G48.margin_top
    pdf = probe.render_probe_page(payload, compress=False)
    # The zone's outline and its hatch, every hatch line inside the zone and at 45°.
    assert any(_close(r, (15.0, 8.0, 65.0, 18.0)) for r in _rects_mm(pdf))
    hatch = [ln for ln in _lines(pdf) if abs(ln["width"] - probe.HATCH_MM) < 1e-6]
    assert len(hatch) >= 30
    for ln in hatch:
        (ax, ay), (bx, by) = ln["a"], ln["b"]
        assert min(ax, bx) >= 15 - TOL_MM and max(ax, bx) <= 65 + TOL_MM
        assert min(ay, by) >= 8 - TOL_MM and max(ay, by) <= 18 + TOL_MM
        assert abs(abs(bx - ax) - abs(by - ay)) < 1e-4
    # The ID rectangle: four dashed edges, on the rectangle the placement chose.
    dashed = [ln for ln in _lines(pdf) if ln["dash"]]
    assert len(dashed) == 4
    x0, y0 = id_rect["x"] / 100, id_rect["y"] / 100
    x1, y1 = x0 + id_rect["w"] / 100, y0 + id_rect["h"] / 100
    ends = {(round(p[0], 2), round(p[1], 2)) for ln in dashed for p in (ln["a"], ln["b"])}
    assert ends == {(round(x0, 2), round(y0, 2)), (round(x1, 2), round(y0, 2)),
                    (round(x1, 2), round(y1, 2)), (round(x0, 2), round(y1, 2))}  # fmt: skip
    # It keeps clear of the zone.
    assert x1 <= 15 or x0 >= 65 or y1 <= 8 or y0 >= 18


async def test_probe_without_a_margin_id_draws_no_id_rectangle(
    async_session_factory: Factory,
) -> None:
    pid = await _printer(async_session_factory)
    await _type(async_session_factory, "probe-cell", id_placement="cell")
    payload = await _payload(async_session_factory, pid, "probe-cell")
    assert payload.detail["id_rect"] is None
    pdf = probe.render_probe_page(payload, compress=False)
    assert [ln for ln in _lines(pdf) if ln["dash"]] == []


# ---------------------------------------------------------------------------- no state


async def test_probe_changes_no_sheet_or_cell_row(async_session_factory: Factory) -> None:
    pid = await _printer(async_session_factory)
    await _type(async_session_factory, "probe-state")

    async def counts() -> tuple[int, int, int]:
        async with async_session_factory() as s:
            out = []
            for model in (Sheet, SheetCell, PrinterCalibration):
                out.append((await s.execute(select(func.count()).select_from(model))).scalar_one())
            return out[0], out[1], out[2]

    before = await counts()
    payload = await _payload(async_session_factory, pid, "probe-state")
    probe.render_probe_page(payload)
    await _payload(async_session_factory, pid, "probe-state", grid=True)
    assert await counts() == before
    assert asdict(G48) == payload.detail["geometry"]
