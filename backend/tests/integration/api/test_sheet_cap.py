# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Caps on active sheets (#204): ``sheets.max_active`` per hub, ``sheets.max_active_per_type``.

Every new sheet goes through ``NewSheetBatch.add`` inside its write transaction, so the cap holds
for every way a sheet is created: the plain create, the ID-label and back-side creates and the
sheets a sheet print opens. Only active sheets count; discarding one frees room.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from app.models.sheet import Sheet, SheetCell
from sqlalchemy import text

from tests.integration.api.test_sheet_print_api import (
    Rig,
    _count,
    _err,
    _factory,
    _item,
    _type,
    running,
)
from tests.integration.api.test_sheet_types_api import _insert_key

pytestmark = pytest.mark.asyncio


async def _caps(rig: Rig, *, hub: int | None = None, per_type: int | None = None) -> None:
    values: dict[str, Any] = {}
    if hub is not None:
        values["sheets.max_active"] = hub
    if per_type is not None:
        values["sheets.max_active_per_type"] = per_type
    async with _factory()() as s:
        await rig.app.state.settings_store.set_many(s, values)


async def _create(rig: Rig, type_key: str = "acme-t1", **kw: Any) -> httpx.Response:
    body = {"type": type_key, "id_placement": "manual", **kw}
    return await rig.client.post("/api/v1/sheets", json=body, headers=rig.key)


def _limit(resp: httpx.Response, cap: int, scope: str) -> None:
    assert resp.status_code == 409, resp.text
    err = _err(resp)
    assert err["error_code"] == "sheet_limit_reached"
    assert err["error_detail"] == {"cap": cap, "scope": scope}


async def test_production_engine_has_the_pragmas(production_engine) -> None:
    """(guard) The fixture is configured like ``app/db/engine.py``."""
    async with production_engine.connect() as conn:
        assert (await conn.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await conn.execute(text("PRAGMA busy_timeout"))).scalar() == 5000
        assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1


async def test_hub_cap_refuses_with_409() -> None:
    async with running() as rig:
        await _type("acme-t2", "AT2")
        await _caps(rig, hub=2)
        assert (await _create(rig)).status_code == 201
        assert (await _create(rig, "acme-t2")).status_code == 201

        _limit(await _create(rig), 2, "hub")
        _limit(await _create(rig, "acme-t2"), 2, "hub")
        assert await _count(Sheet) == 2


async def test_type_cap_refuses_with_409() -> None:
    async with running() as rig:
        await _type("acme-t2", "AT2")
        await _caps(rig, per_type=1)
        assert (await _create(rig)).status_code == 201

        _limit(await _create(rig), 1, "type")
        assert (await _create(rig, "acme-t2")).status_code == 201  # another type has room
        assert await _count(Sheet) == 2


async def test_cap_is_read_live_from_the_setting() -> None:
    async with running() as rig:
        await _caps(rig, hub=1)
        assert (await _create(rig)).status_code == 201
        _limit(await _create(rig), 1, "hub")
        await _caps(rig, hub=3)
        assert (await _create(rig)).status_code == 201


async def test_discarding_frees_room() -> None:
    async with running() as rig:
        await _caps(rig, per_type=1)
        first = (await _create(rig)).json()["code"]
        _limit(await _create(rig), 1, "type")
        admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
        resp = await rig.client.post(f"/api/v1/sheets/{first}/discard", headers=admin)
        assert resp.status_code == 200, resp.text

        assert (await _create(rig)).status_code == 201


async def test_cap_counts_only_active() -> None:
    async with running() as rig:
        await _caps(rig, hub=2, per_type=2)
        admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
        for _ in range(2):
            code = (await _create(rig)).json()["code"]
            assert (
                await rig.client.post(f"/api/v1/sheets/{code}/discard", headers=admin)
            ).status_code == 200

        assert (await _create(rig)).status_code == 201
        assert (await _create(rig)).status_code == 201
        _limit(await _create(rig), 2, "hub")


async def test_hub_cap_concurrent_creates_respect_the_cap(production_engine) -> None:
    """Two creates at cap - 1: the count is read under the write lock, so one of them loses."""
    async with running() as rig:
        await _caps(rig, hub=3)
        for _ in range(2):
            assert (await _create(rig)).status_code == 201

        for _ in range(3):  # a few rounds: the outcome must not depend on scheduling
            results = await asyncio.gather(*(_create(rig) for _ in range(2)))
            statuses = sorted(r.status_code for r in results)
            assert statuses in ([201, 409], [409, 409]), [r.text for r in results]
        assert await _count(Sheet) == 3


async def test_print_onto_new_sheets_over_the_cap_is_409_and_reserves_nothing() -> None:
    async with running() as rig:
        await _caps(rig, per_type=1)
        assert (await _create(rig)).status_code == 201
        cells_before = await _count(SheetCell)
        body = {
            "items": [_item(i) for i in range(3)],
            "placements": [{"new_sheet": {"type": "acme-t1"}}],
        }

        resp = await rig.print(body)

        _limit(resp, 1, "type")
        assert await _count(Sheet) == 1
        assert await _count(SheetCell) == cells_before == 0
        assert [r for r in rig.fake.received if r.operation == "Print-Job"] == []


async def test_back_label_and_id_label_routes_respect_the_cap() -> None:
    async with running() as rig:
        await _caps(rig, hub=1)
        assert (await _create(rig)).status_code == 201

        cell = await _create(rig, "avery-zweckform-l4736rev", id_placement="cell", printer="page-1")
        _limit(cell, 1, "hub")
        back = await _create(
            rig, "avery-zweckform-3490", id_placement="back_side", printer="page-1"
        )
        _limit(back, 1, "hub")
        assert await _count(Sheet) == 1
        assert [r for r in rig.fake.received if r.operation == "Print-Job"] == []


async def test_cap_setting_is_validated(api_client_with_seed) -> None:
    admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
    for key in ("sheets.max_active", "sheets.max_active_per_type"):
        for bad in (0, 10001, "5", True):
            resp = await api_client_with_seed.put(
                "/api/v1/settings", json={"values": {key: bad}}, headers=admin
            )
            assert resp.status_code == 422, (key, bad)
    listed = {
        s["key"]: s["value"]
        for s in (await api_client_with_seed.get("/api/v1/settings", headers=admin)).json()
    }
    assert listed["sheets.max_active"] == 1000
    assert listed["sheets.max_active_per_type"] == 200
