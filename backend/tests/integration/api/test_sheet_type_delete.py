# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deleting sheet types (ADR 0028, spec 6.1.5): any type without sheets, one or several at once.

The prefix counter stays, so a re-added type never issues a sheet code twice; a Grocy label
setting that names a type keeps it; page jobs of a deleted type stay readable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.models.app_setting import AppSetting
from app.models.job import Job
from app.models.sheet import Sheet, SheetIdCounter, SheetType, SheetTypeVersion
from app.settings_store.registry import (
    GROCY_LABEL_DEFAULT_SHEET_TYPE_KEY,
    grocy_label_sheet_type_key,
)
from app.sheets import sheet_service as ss
from app.sheets import type_service as ts
from sqlalchemy import func, select, update
from sqlmodel import col

from tests.integration.api.test_sheet_print_api import _printer
from tests.integration.api.test_sheet_types_api import _create, _err, _factory, _insert_key

pytestmark = pytest.mark.asyncio

SEED_KEY = "avery-5160"  # an upgraded installation's seeded type, without sheets


@pytest.fixture
async def admin() -> dict[str, str]:
    return {"X-Label-Hub-Key": await _insert_key(["admin"])}


@pytest.fixture
async def reader() -> dict[str, str]:
    return {"X-Label-Hub-Key": await _insert_key(["read"])}


async def _sheet(key: str = "acme-t1") -> str:
    async with _factory()() as s:
        sheet = await ss.create_sheet(
            s,
            type_key=key,
            version=None,
            id_placement="manual",
            used_cells=[],
            note=None,
            actor="test",
        )
        return sheet.code


async def _exists(key: str) -> bool:
    async with _factory()() as s:
        found = await s.execute(select(SheetType).where(col(SheetType.key) == key))
        return found.scalar_one_or_none() is not None


async def _setting(key: str, value: Any) -> None:
    async with _factory()() as s:
        s.add(AppSetting(key=key, value_json=json.dumps(value)))
        await s.commit()


async def _delete_unused(client: Any, admin: dict[str, str], keys: list[str]) -> Any:
    return await client.post(
        "/api/v1/sheet-types/delete-unused", json={"keys": keys}, headers=admin
    )


async def test_delete_seed_type_without_sheets_is_204(api_client_with_seed, admin):
    await api_client_with_seed.get("/api/v1/sheet-types", headers=admin)  # starts the app
    async with _factory()() as s:
        assert (await ts.get_type(s, SEED_KEY)).origin == "seed"
    resp = await api_client_with_seed.delete(f"/api/v1/sheet-types/{SEED_KEY}", headers=admin)
    assert resp.status_code == 204, resp.text
    gone = await api_client_with_seed.get(f"/api/v1/sheet-types/{SEED_KEY}", headers=admin)
    assert gone.status_code == 404
    async with _factory()() as s:
        versions = await s.execute(select(func.count()).select_from(SheetTypeVersion))
        assert versions.scalar_one() > 0  # only that type's versions went


async def test_delete_imported_type_without_sheets_is_204(api_client_with_seed, admin):
    await _create(api_client_with_seed, admin)
    doc = (
        await api_client_with_seed.get("/api/v1/sheet-types/acme-t1/export", headers=admin)
    ).json()
    await api_client_with_seed.delete("/api/v1/sheet-types/acme-t1", headers=admin)
    imported = await api_client_with_seed.post(
        "/api/v1/sheet-types/import", json=doc, headers=admin
    )
    assert imported.status_code == 201, imported.text
    resp = await api_client_with_seed.delete("/api/v1/sheet-types/acme-t1", headers=admin)
    assert resp.status_code == 204, resp.text


async def test_delete_with_sheets_is_409_has_sheets(api_client_with_seed, admin):
    """(guard) Kept from 1.7.x: counter-test, the sheet count check removed, turns it red."""
    await _create(api_client_with_seed, admin)
    await _sheet()
    resp = await api_client_with_seed.delete("/api/v1/sheet-types/acme-t1", headers=admin)
    assert resp.status_code == 409
    err = _err(resp)
    assert err["error_code"] == "type_in_use"
    assert err["error_detail"] == {"reason": "has_sheets"}
    assert await _exists("acme-t1")


@pytest.mark.parametrize(
    "setting",
    [GROCY_LABEL_DEFAULT_SHEET_TYPE_KEY, grocy_label_sheet_type_key("chore")],
)
async def test_delete_type_in_grocy_settings_is_409(api_client_with_seed, admin, setting):
    await _setting(setting, SEED_KEY)
    resp = await api_client_with_seed.delete(f"/api/v1/sheet-types/{SEED_KEY}", headers=admin)
    assert resp.status_code == 409, resp.text
    err = _err(resp)
    assert err["error_code"] == "type_in_use"
    assert err["error_detail"] == {"reason": "type_in_settings", "setting": setting}
    assert await _exists(SEED_KEY)


async def test_delete_needs_admin(api_client_with_seed, reader):
    """(guard) The route keeps its admin scope."""
    resp = await api_client_with_seed.delete(f"/api/v1/sheet-types/{SEED_KEY}", headers=reader)
    assert resp.status_code == 403
    assert await _exists(SEED_KEY)


async def test_counter_survives_delete_and_readd(api_client_with_seed, admin):
    """(guard) Counter-test: deleting the prefix counter with the type turns it red."""
    await _create(api_client_with_seed, admin)
    first = await _sheet()
    async with _factory()() as s:
        await ss.discard_sheet(s, first, actor="test")
        await s.execute(
            update(Sheet)
            .where(col(Sheet.code) == first)
            .values(discarded_at=datetime.now(UTC) - timedelta(days=3))
        )
        await s.commit()
    async with _factory()() as s:
        purged = await ss.purge_discarded(s, older_than_days=1, limit=10, dry_run=False)
    assert purged.codes == [first]
    resp = await api_client_with_seed.delete("/api/v1/sheet-types/acme-t1", headers=admin)
    assert resp.status_code == 204, resp.text
    async with _factory()() as s:
        counter = await s.get(SheetIdCounter, "AT1")
        assert counter is not None

    await _create(api_client_with_seed, admin)
    second = await _sheet()
    assert second != first
    assert second.split("-")[0] == first.split("-")[0]
    assert int(second.rsplit("-", 1)[1]) > int(first.rsplit("-", 1)[1])


async def test_delete_unused_all_or_nothing(api_client_with_seed, admin):
    await _create(api_client_with_seed, admin, "acme-t1", "AT1")
    await _create(api_client_with_seed, admin, "acme-t2", "AT2")
    await _sheet("acme-t2")
    keys = [SEED_KEY, "acme-t1", "acme-t2"]
    resp = await _delete_unused(api_client_with_seed, admin, keys)
    assert resp.status_code == 409, resp.text
    err = _err(resp)
    assert err["error_code"] == "not_unused"
    assert err["error_detail"] == {"keys": ["acme-t2"]}
    for key in keys:
        assert await _exists(key), key

    ok = await _delete_unused(api_client_with_seed, admin, [SEED_KEY, "acme-t1"])
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"deleted": ["acme-t1", SEED_KEY]}
    assert not await _exists(SEED_KEY) and not await _exists("acme-t1")
    assert await _exists("acme-t2")


async def test_delete_unused_rechecks_inside_the_transaction(api_client_with_seed, admin):
    await _create(api_client_with_seed, admin)
    listed = await api_client_with_seed.get(
        "/api/v1/sheet-types", params={"usage_state": "unused"}, headers=admin
    )
    assert "acme-t1" in [t["key"] for t in listed.json()]
    await _sheet()  # a sheet created between the client's list and its call

    resp = await _delete_unused(api_client_with_seed, admin, ["acme-t1"])
    assert resp.status_code == 409, resp.text
    assert _err(resp)["error_detail"] == {"keys": ["acme-t1"]}
    assert await _exists("acme-t1")


async def test_delete_unused_refuses_a_type_named_in_settings(api_client_with_seed, admin):
    await _create(api_client_with_seed, admin)
    await _setting(grocy_label_sheet_type_key("product"), "acme-t1")
    resp = await _delete_unused(api_client_with_seed, admin, [SEED_KEY, "acme-t1"])
    assert resp.status_code == 409, resp.text
    assert _err(resp)["error_detail"] == {
        "reason": "type_in_settings",
        "setting": grocy_label_sheet_type_key("product"),
        "key": "acme-t1",
    }
    assert await _exists(SEED_KEY)


async def test_delete_unused_unknown_key_is_404(api_client_with_seed, admin):
    resp = await _delete_unused(api_client_with_seed, admin, [SEED_KEY, "acme-nope"])
    assert resp.status_code == 404, resp.text
    assert _err(resp)["error_detail"] == {"keys": ["acme-nope"]}
    assert await _exists(SEED_KEY)


async def test_delete_unused_needs_admin_and_bounds_its_list(api_client_with_seed, admin, reader):
    assert (await _delete_unused(api_client_with_seed, reader, [SEED_KEY])).status_code == 403
    assert (await _delete_unused(api_client_with_seed, admin, [])).status_code == 422
    many = [f"acme-x{i}" for i in range(501)]
    assert (await _delete_unused(api_client_with_seed, admin, many)).status_code == 422
    assert await _exists(SEED_KEY)


async def test_delete_unused_is_a_reserved_type_key(api_client_with_seed, admin):
    """Guard: the static route reserves its segment (Task 7's derivation from the routes)."""
    resp = await api_client_with_seed.post(
        "/api/v1/sheet-types",
        json={
            "key": "delete-unused",
            "prefix": "DUN",
            "geometry": {
                "page_size": "A4",
                "label_w": 45.7,
                "label_h": 21.2,
                "cols": 4,
                "rows": 12,
                "pitch_x": 48.2,
                "pitch_y": 21.2,
                "margin_top": 21.3,
                "margin_left": 9.85,
                "corner_radius": None,
                "shape": "rectangle",
            },
        },
        headers=admin,
    )
    assert resp.status_code == 422, resp.text
    assert _err(resp)["error_code"] == "type_key_reserved"


async def test_delete_of_a_type_without_sheets_leaves_page_jobs_readable(
    api_client_with_seed, admin
):
    """(guard) A calibration page of a type is a job row without a foreign key to the type."""
    await _create(api_client_with_seed, admin)
    printer_id = await _printer("page-del")
    async with _factory()() as s:
        t = await ts.get_type(s, "acme-t1")
        version = await ts.current_version(s, t.id)
        job = Job(
            printer_id=printer_id,
            template_key=None,
            state="done",
            payload={
                "kind": "calibration_page",
                "media": "A4",
                "tray": "auto",
                "render_dpi": 300,
                "job_name": "calibration acme-t1",
                "detail": {"type_key": "acme-t1", "type_version_id": str(version.id)},
            },
        )
        s.add(job)
        await s.commit()
        job_id = job.id

    resp = await api_client_with_seed.delete("/api/v1/sheet-types/acme-t1", headers=admin)
    assert resp.status_code == 204, resp.text
    read = await api_client_with_seed.get(f"/api/v1/jobs/{job_id}", headers=admin)
    assert read.status_code == 200, read.text
    assert read.json()["payload"]["detail"]["type_key"] == "acme-t1"
