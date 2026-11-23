# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exchange format version 3: the sheet code position, keep-out zones and presets (#246).

The new keys are written only when they are not the default, so a type without them stays a
version-1 or -2 document an older hub imports; a version-3 document goes through the same checks
as a write (``_Strict`` and ``preprint_service``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from app.catalog import loader
from app.models.sheet import SheetType
from app.sheets import exchange
from app.sheets import preprint_service as pps
from app.sheets import type_service as ts
from app.sheets.errors import (
    IdPositionOverlapsCellError,
    InvalidImportError,
    PreprintZoneOverlapsCellError,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from tests.unit.sheets.test_exchange import G, _base_doc, _doc_dict, _fields, _placed_type, _raw

_ZONES = [{"x": 1000, "y": 500, "w": 3000, "h": 1000, "note": "Logo"}]
_PRESET_ZONES = [{"x": 15000, "y": 500, "w": 4000, "h": 1000, "note": "Format text"}]


@pytest_asyncio.fixture
async def session(async_session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[Any]:
    async with async_session_factory() as s:
        yield s


@pytest_asyncio.fixture
async def other_session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    import app.models  # noqa: F401

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'other.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _positioned_type(session: AsyncSession) -> None:
    await ts.create_type(session, _fields(), G, actor="tester")
    await pps.create(
        session, key="avery-front", name="Avery front", page_size="A4", zones=_PRESET_ZONES
    )
    await ts.patch_type(
        session,
        "avery-l4736",
        {
            "id_margin_band": "top",
            "id_margin_align": "end",
            "id_margin_offset_hmm": 2550,
            "preprint_zones": _ZONES,
            "preprint_preset": "avery-front",
        },
        actor="tester",
    )


async def test_exchange_round_trip_of_the_new_keys(
    session: AsyncSession, other_session: AsyncSession
) -> None:
    await _positioned_type(session)
    raw = _doc_dict(await exchange.export_type(session, "avery-l4736", None))
    assert raw["format_version"] == 3
    t = raw["type"]
    assert (t["id_margin_band"], t["id_margin_align"], t["id_margin_offset_mm"]) == (
        "top",
        "end",
        25.5,
    )
    assert t["preprint_zones"] == [
        {"x_mm": 10.0, "y_mm": 5.0, "w_mm": 30.0, "h_mm": 10.0, "note": "Logo"}
    ]
    assert t["preprint_preset"]["key"] == "avery-front"

    await exchange.import_type(other_session, exchange.parse_import(_raw(raw)), actor="importer")

    copy = await ts.get_type(other_session, "avery-l4736")
    assert (copy.id_margin_band, copy.id_margin_align, copy.id_margin_offset_hmm) == (
        "top",
        "end",
        2550,
    )
    assert copy.preprint_zones == _ZONES
    preset = await pps.get(other_session, "avery-front")
    assert preset.origin == "imported" and preset.zones == _PRESET_ZONES
    assert copy.preprint_preset_id == preset.id
    again = _doc_dict(await exchange.export_type(other_session, "avery-l4736", None))
    assert again["type"] == t


async def test_v2_document_still_imports(
    session: AsyncSession, other_session: AsyncSession
) -> None:
    """(guard) A 1.7.x version-2 document imports and gets the default position."""
    await _placed_type(session)
    raw = _doc_dict(await exchange.export_type(session, "avery-l4736", None))
    assert raw["format_version"] == 2

    await exchange.import_type(other_session, exchange.parse_import(_raw(raw)), actor="importer")

    t = await ts.get_type(other_session, "avery-l4736")
    assert (t.id_margin_band, t.id_margin_align, t.id_margin_offset_hmm) == ("auto", "start", 0)
    assert t.preprint_zones == [] and t.preprint_preset_id is None


async def test_export_without_new_keys_is_version_2(session: AsyncSession) -> None:
    """(guard) The new keys are written only when they are not the default."""
    await _placed_type(session)
    raw = _doc_dict(await exchange.export_type(session, "avery-l4736", None))
    assert raw["format_version"] == 2
    for name in exchange.POSITION_DEFAULTS:
        assert name not in raw["type"]


async def test_version_2_document_with_a_new_key_is_refused(session: AsyncSession) -> None:
    doc = await _base_doc(session)
    doc["format_version"] = 2
    doc["type"]["id_margin_band"] = "top"
    with pytest.raises(InvalidImportError):
        exchange.parse_import(_raw(doc))
    doc["format_version"] = 3
    assert exchange.parse_import(_raw(doc)).type.id_margin_band == "top"


async def test_imported_zone_over_a_cell_is_refused(session: AsyncSession) -> None:
    doc = await _base_doc(session)
    doc["format_version"] = 3
    doc["type"]["preprint_zones"] = [{"x_mm": 20.0, "y_mm": 30.0, "w_mm": 5.0, "h_mm": 5.0}]
    with pytest.raises(PreprintZoneOverlapsCellError):
        exchange.parse_import(_raw(doc))
    doc["type"]["preprint_zones"] = []
    doc["type"]["id_margin_band"] = "left"  # 8.4 mm left margin: the 5 mm + 4 mm box reaches a cell
    with pytest.raises(IdPositionOverlapsCellError):
        exchange.parse_import(_raw(doc))
    doc["type"]["id_margin_band"] = "top"
    doc["type"]["preprint_zones"] = [{"x_mm": 1, "y_mm": 1, "w_mm": 1, "h_mm": 1, "evil": 1}]
    with pytest.raises(InvalidImportError):
        exchange.parse_import(_raw(doc))
    doc["type"]["preprint_zones"] = [{"x_mm": 1, "y_mm": 1, "w_mm": 1, "h_mm": 1}] * 17
    with pytest.raises(InvalidImportError):
        exchange.parse_import(_raw(doc))


async def test_catalog_types_stay_unedited_after_the_migration(session: AsyncSession) -> None:
    """Task 8c: the new fields hold their defaults on every installed type, so the fields hash
    of a type that never set them does not change and it still counts as unedited."""
    entry, doc = next(
        (e, d)
        for e in loader.load_index()
        if (d := loader.load_entry(e.key)).type.backing_design is None
    )
    t = SheetType(
        key=doc.type.key,
        prefix=doc.type.prefix,
        vendor=doc.type.vendor,
        product_codes=list(doc.type.product_codes),
        description=doc.type.description,
        allow_qr=doc.type.allow_qr,
        verified=doc.type.verified,
        origin="seed",
        source_name=doc.type.source_name,
        source_url=doc.type.source_url,
        notes=doc.type.notes,
        id_placement=doc.type.id_placement,
        id_cell=doc.type.id_cell,
    )
    session.add(t)
    await session.flush()
    stored = (await session.execute(select(SheetType).where(SheetType.key == t.key))).scalar_one()
    assert stored.id_margin_band == "auto" and stored.preprint_zones == []
    rebuilt = loader.type_doc_of(stored, None)
    assert rebuilt is not None
    assert loader.fields_hash(rebuilt) == loader.fields_hash(doc.type) == entry.fields_hash
    stored.id_margin_band = "top"
    edited = loader.type_doc_of(stored, None)
    assert edited is not None and loader.fields_hash(edited) != entry.fields_hash
