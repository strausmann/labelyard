# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The versions of a sheet type and "Restore as new version" (#336 point 4, design 7.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import app.models  # noqa: F401
import bcrypt
import pytest
from app.models.api_key import ApiKey
from app.models.sheet import Sheet, SheetType, SheetTypeVersion
from app.sheets import type_service as ts
from app.sheets.geometry import Geometry
from sqlalchemy import select
from sqlmodel import col

pytestmark = pytest.mark.asyncio

G48 = Geometry("A4", 21000, 29700, 4570, 2120, 4, 12, 4820, 2120, 2130, 985, None, "rectangle")
KEY = "tvr-test"


def _factory():  # type: ignore[no-untyped-def]
    import app.db.engine as _engine_module

    return _engine_module.async_session


async def _key(scopes: list[str]) -> dict[str, str]:
    plaintext = f"lh_pat_{uuid4().hex}"
    hashed = bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt(rounds=4)).decode()
    async with _factory()() as s:
        s.add(
            ApiKey(
                name=f"tvr-{uuid4().hex[:6]}",
                key_hash=hashed,
                key_prefix=plaintext[:16],
                scopes=scopes,
                allowed_printer_ids=[],
                enabled=True,
                rate_limit_per_minute=6000,
            )
        )
        await s.commit()
    return {"X-Label-Hub-Key": plaintext}


def _geometry_mm(**changes: Any) -> dict[str, Any]:
    g = {
        "page_size": "A4",
        "label_w": 45.7,
        "label_h": 21.2,
        "cols": 4,
        "rows": 12,
        "pitch_x": 48.2,
        "pitch_y": 21.2,
        "margin_top": 21.3,
        "margin_left": 9.85,
        "shape": "rectangle",
    }
    return {**g, **changes}


async def _type_with_two_versions(c: Any, admin: dict[str, str]) -> None:
    async with _factory()() as s:
        await ts.create_type(
            s, ts.TypeFields(KEY, "TVR", "Acme", [], None, None), G48, actor="user:first"
        )
    r = await c.post(
        f"/api/v1/sheet-types/{KEY}/versions",
        json={"geometry": _geometry_mm(label_w=45.5, margin_left=9.95), "change_note": "Wider"},
        headers=admin,
    )
    assert r.status_code == 201, r.text


async def _sheet_on(version: int) -> None:
    async with _factory()() as s:
        t = (await s.execute(select(SheetType).where(col(SheetType.key) == KEY))).scalar_one()
        v = (
            await s.execute(
                select(SheetTypeVersion).where(
                    col(SheetTypeVersion.type_id) == t.id, col(SheetTypeVersion.version) == version
                )
            )
        ).scalar_one()
        s.add(
            Sheet(
                id=uuid4(),
                code=f"TVR-{uuid4().hex[:4]}",
                type_id=t.id,
                type_version_id=v.id,
                id_placement="cell",
                created_at=datetime(2026, 11, 21, tzinfo=UTC),
            )
        )
        await s.commit()


async def test_type_versions_list_shows_changed_fields(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin, reader = await _key(["admin"]), await _key(["read"])
    await _type_with_two_versions(c, admin)
    r = await c.get(f"/api/v1/sheet-types/{KEY}/versions", headers=reader)
    assert r.status_code == 200, r.text
    v2, v1 = r.json()  # newest first
    assert v1["version"] == 1 and v1["changed_fields"] == {} and v1["created_by"] == "user:first"
    assert v2["version"] == 2 and v2["change_note"] == "Wider"
    assert v2["changed_fields"] == {
        "label_w": {"old": 45.7, "new": 45.5},
        "margin_left": {"old": 9.85, "new": 9.95},
    }
    assert v2["created_by"] is not None and v2["created_at"]
    assert v2["geometry"]["label_w"] == 45.5 and v2["sheets"] == 0
    assert v1["restorable"] is True and v2["restorable"] is False  # the newest is current
    missing = await c.get("/api/v1/sheet-types/no-such-type/versions", headers=reader)
    assert missing.status_code == 404


async def test_type_version_restore_adds_a_version(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    await _type_with_two_versions(c, admin)
    await _sheet_on(1)  # a sheet on the restored version itself does not block
    r = await c.post(f"/api/v1/sheet-types/{KEY}/versions/1/restore", headers=admin)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["version"] == 3 and out["change_note"] == "Restored from version 1"
    assert out["geometry"]["label_w"] == 45.7 and out["geometry"]["margin_left"] == 9.85
    listed = (await c.get(f"/api/v1/sheet-types/{KEY}/versions", headers=admin)).json()
    assert [v["version"] for v in listed] == [3, 2, 1]
    assert listed[0]["changed_fields"]["label_w"] == {"old": 45.5, "new": 45.7}
    # The newest version cannot be restored onto itself: its geometry is unchanged.
    same = await c.post(f"/api/v1/sheet-types/{KEY}/versions/3/restore", headers=admin)
    assert same.status_code == 422
    unknown = await c.post(f"/api/v1/sheet-types/{KEY}/versions/9/restore", headers=admin)
    assert unknown.status_code == 404
    reader = await c.post(
        f"/api/v1/sheet-types/{KEY}/versions/1/restore", headers=await _key(["read"])
    )
    assert reader.status_code == 403


async def test_type_version_restore_refused_when_a_sheet_uses_a_newer_version(
    api_client_with_seed: Any,
) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    await _type_with_two_versions(c, admin)
    await _sheet_on(2)
    r = await c.post(f"/api/v1/sheet-types/{KEY}/versions/1/restore", headers=admin)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error_code"] == "newer_version_in_use"
    listed = (await c.get(f"/api/v1/sheet-types/{KEY}/versions", headers=admin)).json()
    assert [v["version"] for v in listed] == [2, 1]
    assert listed[1]["restorable"] is False and listed[0]["restorable"] is False
