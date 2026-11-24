# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Guard (ADR 0034): every job payload this release writes can be read by 1.7.1.

The way back from 1.8.0 is its downgrade migrations (or the backup); after a downgrade 1.7.1
retries and re-renders the stored jobs from their payloads. So each payload kind is built here by
its **production** builder and parsed with the frozen 1.7.1 models
(``tests/fixtures/models_1_7_1.py``, which import nothing from ``app``). A task that writes a new
payload key adds a case; a new key in an ``extra="forbid"`` model of 1.7.1 fails here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import app.models  # noqa: F401  (registers every table)
import pytest
import pytest_asyncio
from app.calibration import page as calibration_page
from app.models.printer import Printer
from app.models.printer_calibration import PrinterCalibration
from app.models.printer_tray import PrinterTray
from app.models.sheet import Sheet, SheetType, SheetTypeVersion
from app.page_render.test_page import build_test_page_payload
from app.printer_backends.snmp_helper import PreflightStatus
from app.schemas.capabilities import HardwareMargins
from app.schemas.content_type import ContentType
from app.schemas.label_data import LabelData
from app.schemas.print_request import PrintRequest, RawLabelData
from app.services.layout_engine import LayoutEngine
from app.services.print_service import PrintService
from app.sheets import print_service as sps
from app.sheets import type_service as ts
from app.sheets.back_side import loading_page_payload
from app.sheets.geometry import Geometry
from app.sheets.planner import FeedMarkPlacement, MarginPlacement
from app.sheets.reservation import CellAssignment, ItemMeta, ReservedPage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from tests.fixtures import models_1_7_1 as v171

pytestmark = pytest.mark.asyncio

# 45.7 x 21.2 mm labels, 4 x 12 on A4.
G48 = Geometry("A4", 21000, 29700, 4570, 2120, 4, 12, 4820, 2120, 2130, 985, None, "rectangle")


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _read_by_1_7_1(payload: dict[str, Any]) -> v171.PagePayload:
    """What 1.7.1 does first with a stored page job: parse its payload."""
    return v171.PagePayload.model_validate(payload)


async def _ipp_printer(factory: async_sessionmaker[AsyncSession]) -> Printer:
    async with factory() as s:
        p = Printer(
            id=uuid4(),
            name="Office",
            slug="office",
            model="m",
            backend="ipp",
            connection={"uri": "ipp://printer.example.invalid/ipp/print"},
        )
        s.add(p)
        s.add(PrinterTray(printer_id=p.id, tray_key="auto", label="Auto", is_default=True))
        await s.commit()
        return p


async def _sheet_type(factory: async_sessionmaker[AsyncSession], key: str) -> None:
    async with factory() as s:
        await ts.create_type(
            s, ts.TypeFields(key, "ACME48", "Acme", [], None, None), G48, actor="t"
        )


async def test_payload_fixture_matches_1_7_1_calibration_page(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    printer = await _ipp_printer(factory)
    await _sheet_type(factory, "acme-48")
    async with factory() as s:
        s.add(
            PrinterCalibration(
                printer_id=printer.id,
                tray_key="auto",
                method="manual",
                offset_x_hmm=12,
                offset_y_hmm=-7,
                scale_x_ppm=1_000_400,
                scale_y_ppm=999_800,
                rotation_udeg=150,
            )
        )
        await s.commit()
        payload = await calibration_page.build_payload(
            s, printer.id, "auto", type_key="acme-48", version=None, corrected=True
        )
    read = _read_by_1_7_1(payload.model_dump(mode="json"))
    assert read.kind == "calibration_page"
    assert read.calibration is not None and read.calibration.dx_hmm == 12
    v171.Geometry(**read.detail["geometry"])  # the renderer of 1.7.1 builds it from the detail


async def test_payload_fixture_matches_1_7_1_sheet_page() -> None:
    info = sps._TypeInfo(
        key="acme-48", label="ACME48", id_title="Acme ACME48", allow_qr=True, geometry=G48
    )
    page = ReservedPage(
        job_id=uuid4(),
        sheet_id=uuid4(),
        sheet_code="ACME48-0001",
        type_version_id=uuid4(),
        assignments=[
            CellAssignment(cell=1, item_index=None, source="hub", purpose="id", ref=None),
            CellAssignment(cell=2, item_index=0, source="api", purpose="Drill", ref=None),
        ],
        id_cell=1,
        margin_id=True,
        hw_margin_cells=[],
        margin_placement=MarginPlacement(band="top", corner="top_left", depth_hmm=900, qr=False),
        feed_mark=FeedMarkPlacement(
            text="Feed", x_hmm=17000, baseline_y_hmm=600, size_pt=7.0, width_hmm=800
        ),
    )
    item = sps.ResolvedItem(
        label=LabelData(source_app="manual", title="Drill", primary_id="T-1", qr_payload="x"),
        content_type=ContentType.QR_TWO_LINES,
        meta=ItemMeta(source="api", purpose="Drill", ref=None),
        lookup=None,
    )
    payload = sps._page_payload(
        page,
        [item],
        info,
        tray="auto",
        render_dpi=600,
        calibration=None,
        scan=lambda code: f"https://hub.example.invalid/s/{code}",
    )
    read = _read_by_1_7_1(payload.model_dump(mode="json"))
    detail = v171.SheetPageDetail.model_validate(read.detail)
    assert detail.margin_id is not None and detail.feed_mark is not None
    assert detail.id_cell is not None and [c.cell for c in detail.cells] == [2]


def _page_with(placement: MarginPlacement, mark: FeedMarkPlacement) -> dict[str, Any]:
    info = sps._TypeInfo(
        key="acme-48", label="ACME48", id_title="Acme ACME48", allow_qr=True, geometry=G48
    )
    page = ReservedPage(
        job_id=uuid4(),
        sheet_id=uuid4(),
        sheet_code="ACME48-0001",
        type_version_id=uuid4(),
        assignments=[CellAssignment(cell=2, item_index=0, source="api", purpose="x", ref=None)],
        id_cell=None,
        margin_id=True,
        hw_margin_cells=[],
        margin_placement=placement,
        feed_mark=mark,
    )
    item = sps.ResolvedItem(
        label=LabelData(source_app="manual", title="Drill", primary_id="T-1", qr_payload="x"),
        content_type=ContentType.QR_TWO_LINES,
        meta=ItemMeta(source="api", purpose="x", ref=None),
        lookup=None,
    )
    payload = sps._page_payload(
        page,
        [item],
        info,
        tray="auto",
        render_dpi=600,
        calibration=None,
        scan=lambda code: f"https://hub.example.invalid/s/{code}",
    )
    return payload.model_dump(mode="json")


async def test_1_8_sheet_page_payload_validates_with_the_1_7_1_models() -> None:
    """#246 (S2): the chosen rectangle goes into ``SheetPageDetail`` only; 1.7.1 ignores it."""
    placement = MarginPlacement(
        band="top",
        corner="top_left",
        depth_hmm=1530,
        qr=True,
        x_hmm=5085,
        y_hmm=500,
        w_hmm=4000,
        content="qr_code",
    )
    mark = FeedMarkPlacement(
        text="Feed", x_hmm=17000, baseline_y_hmm=600, size_pt=7.0, width_hmm=800
    )
    raw = _page_with(placement, mark)
    assert raw["detail"]["margin_id_rect"] == {
        "x_hmm": 5085, "y_hmm": 500, "w_hmm": 4000, "h_hmm": 1530, "content": "qr_code",
    }  # fmt: skip
    read = _read_by_1_7_1(raw)
    detail = v171.SheetPageDetail.model_validate(read.detail)
    assert detail.margin_id is not None and detail.margin_id.text == "ACME48-0001"
    assert detail.margin_id.band == "top" and detail.feed_mark is not None


async def test_mark_at_the_left_end_validates_with_the_1_7_1_models() -> None:
    """#246 (S15): a mark at the top-left end keeps ``anchor="top_right"``; its x carries it."""
    placement = MarginPlacement(
        band="top", corner="top_left", depth_hmm=900, qr=False, x_hmm=10000, y_hmm=1130,
        w_hmm=6000, content="code",
    )  # fmt: skip
    mark = FeedMarkPlacement(
        text="Feed", x_hmm=985, baseline_y_hmm=600, size_pt=7.0, width_hmm=800, end="top_left"
    )
    raw = _page_with(placement, mark)
    assert raw["detail"]["feed_mark_end"] == "top_left"
    assert raw["detail"]["feed_mark"]["x_hmm"] == 985
    detail = v171.SheetPageDetail.model_validate(_read_by_1_7_1(raw).detail)
    assert detail.feed_mark is not None and detail.feed_mark.x_hmm == 985
    assert detail.feed_mark.anchor == "top_right"


async def test_payload_fixture_matches_1_7_1_sheet_back_page(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    printer = await _ipp_printer(factory)
    await _sheet_type(factory, "acme-48")
    async with factory() as s:
        t = (await s.execute(select(SheetType).where(SheetType.key == "acme-48"))).scalar_one()
        v = (
            await s.execute(select(SheetTypeVersion).where(SheetTypeVersion.type_id == t.id))
        ).scalar_one()
        sheet = Sheet(
            id=uuid4(),
            code="ACME48-0001",
            type_id=t.id,
            type_version_id=v.id,
            created_at=datetime(2026, 11, 21, tzinfo=UTC),
        )
        payload = await sps._back_payload(
            s, sheet, tray_key="auto", printer_id=printer.id, scan=lambda c: f"https://x/{c}"
        )
    read = _read_by_1_7_1(payload.model_dump(mode="json"))
    assert read.kind == "sheet_back_page"
    v171.SheetBackDetail.model_validate(read.detail)


async def test_payload_fixture_matches_1_7_1_test_page() -> None:
    printer = Printer(name="Office", slug="office", model="m", backend="ipp", connection={})
    payload = build_test_page_payload(
        printer,
        "auto",
        "A4",
        HardwareMargins(top=4.2, left=4.2, bottom=4.2, right=4.2),
        datetime(2026, 11, 21, 12, 0, tzinfo=UTC),
        "1.8.0",
        "https://hub.example.invalid/printers/office",
    )
    assert _read_by_1_7_1(payload.model_dump(mode="json")).kind == "test_page"


async def test_payload_fixture_matches_1_7_1_tray_loading_page() -> None:
    payload = loading_page_payload("auto", "A4", "de")
    assert _read_by_1_7_1(payload.model_dump(mode="json")).kind == "tray_loading_page"


async def test_payload_fixture_matches_1_7_1_tape_job() -> None:
    """A tape job has no ``kind``; 1.7.1 needs its re-render keys and a content type it knows."""
    store = MagicMock()
    store.save_queued = AsyncMock()
    queue = MagicMock()
    queue.enqueue_batch = AsyncMock()
    backend = AsyncMock()
    backend.half_cut_supported = False
    backend.preflight_check = AsyncMock(
        return_value=PreflightStatus(hr_printer_status="idle", loaded_tape_mm=12, error_flags=[])
    )
    service = PrintService(
        printer_id=uuid4(), backend=backend, queue=queue, store=store, engine=LayoutEngine()
    )
    request = PrintRequest(
        content_type=ContentType.QR_TWO_LINES,
        data=RawLabelData(title="Drill", primary_id="T-1", qr_payload="https://x.invalid/1"),
    )
    await service.submit_batch_job([request], half_cut=False)
    stored = [call.args[0].payload for call in store.save_queued.await_args_list]
    assert stored
    for payload in stored:
        assert "kind" not in payload
        assert payload.keys() >= v171.TAPE_RERENDER_KEYS
        assert payload["content_type"] in v171.ContentType.__args__  # type: ignore[attr-defined]


async def test_the_fixture_imports_nothing_from_app() -> None:
    """(guard) The frozen copy must not follow the live models."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(v171.__file__).read_text(encoding="utf-8"))
    imported = [
        n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    ]
    assert [m for m in imported if m and (m == "app" or m.startswith("app."))] == []


async def test_a_new_key_in_a_forbid_model_is_refused_by_1_7_1() -> None:
    """(guard) The fixture really refuses what 1.7.1 refuses: a feed mark with a new key."""
    mark = {"text": "Feed", "x_hmm": 1, "baseline_y_hmm": 1, "size_pt": 7.0}
    v171.FeedMarkPayload.model_validate(mark)
    with pytest.raises(ValueError):
        v171.FeedMarkPayload.model_validate({**mark, "anchor": "top_left"})
    with pytest.raises(ValueError):
        v171.MarginIdPayload.model_validate({"text": "x", "usable_hmm": 1, "new": 1})
