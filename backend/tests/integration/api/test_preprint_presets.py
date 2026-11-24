# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""``/api/v1/preprint-presets`` (#246): the front-side twin of the backing designs.

The full app (``test_sheet_print_api``'s rig, type ``acme-t1``). Reads need ``read`` scope, writes
``admin``; a preset a type links is not deleted (409 ``preset_in_use``).
"""

from __future__ import annotations

import pytest
from app.models.preprint_preset import PreprintPreset
from sqlalchemy import func, select

from tests.integration.api.test_sheet_print_api import _err, running
from tests.integration.api.test_sheet_types_api import _factory, _insert_key

pytestmark = pytest.mark.asyncio

URL = "/api/v1/preprint-presets"
FORMAT_TEXT = {"x_mm": 150, "y_mm": 5, "w_mm": 40, "h_mm": 10, "note": "Format text"}


async def _count() -> int:
    async with _factory()() as s:
        return int((await s.execute(select(func.count()).select_from(PreprintPreset))).scalar_one())


async def test_preset_crud_and_delete_in_use_is_409() -> None:
    async with running() as rig:
        admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
        body = {
            "key": "avery-front",
            "name": "Avery front",
            "page_size": "A4",
            "zones": [FORMAT_TEXT],
        }

        denied = await rig.client.post(URL, json=body, headers=rig.reader)
        assert denied.status_code == 403
        created = await rig.client.post(URL, json=body, headers=admin)
        assert created.status_code == 201, created.text
        assert created.json()["zones"] == [FORMAT_TEXT] and created.json()["origin"] == "custom"
        again = await rig.client.post(URL, json=body, headers=admin)
        assert again.status_code == 409 and _err(again)["error_code"] == "preprint_preset_exists"
        off = await rig.client.post(
            URL,
            json={**body, "key": "off-page", "zones": [{**FORMAT_TEXT, "x_mm": 200}]},
            headers=admin,
        )
        assert off.status_code == 422 and _err(off)["error_code"] == "preprint_zone_off_page"
        assert await _count() == 1

        listed = await rig.client.get(URL, headers=rig.reader)
        assert [p["key"] for p in listed.json()] == ["avery-front"]

        linked = await rig.client.patch(
            "/api/v1/sheet-types/acme-t1", json={"preprint_preset": "avery-front"}, headers=admin
        )
        assert linked.status_code == 200, linked.text
        assert linked.json()["preprint_preset"]["key"] == "avery-front"
        assert linked.json()["preprint_preset"]["types_in_use"] == 1

        renamed = await rig.client.patch(
            f"{URL}/avery-front", json={"name": "Avery"}, headers=admin
        )
        assert renamed.status_code == 200 and renamed.json()["types_in_use"] == 1
        over_cell = await rig.client.patch(
            f"{URL}/avery-front",
            json={"zones": [{"x_mm": 20, "y_mm": 30, "w_mm": 5, "h_mm": 5}]},
            headers=admin,
        )
        assert over_cell.status_code == 422
        assert _err(over_cell)["error_code"] == "preprint_zone_overlaps_cell"

        in_use = await rig.client.delete(f"{URL}/avery-front", headers=admin)
        assert in_use.status_code == 409 and _err(in_use)["error_code"] == "preset_in_use"
        assert _err(in_use)["error_detail"] == {"types_in_use": 1}

        await rig.client.patch(
            "/api/v1/sheet-types/acme-t1", json={"preprint_preset": None}, headers=admin
        )
        deleted = await rig.client.delete(f"{URL}/avery-front", headers=admin)
        assert deleted.status_code == 204
        assert await _count() == 0
        missing = await rig.client.delete(f"{URL}/avery-front", headers=admin)
        assert missing.status_code == 404
        assert _err(missing)["error_code"] == "preprint_preset_not_found"
        unknown = await rig.client.patch(
            "/api/v1/sheet-types/acme-t1", json={"preprint_preset": "nope"}, headers=admin
        )
        assert unknown.status_code == 404
