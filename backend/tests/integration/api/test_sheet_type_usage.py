# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Usage numbers of sheet types (spec 6.1.5): computed in SQL, one statement for the list."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.models.sheet import Sheet, SheetCell
from app.repositories import sheet_types as repo
from app.sheets import sheet_service as ss
from app.sheets import type_service as ts
from app.sheets.sheet_service import MarkedCell
from sqlalchemy import event, insert, select, update
from sqlmodel import col

from tests.integration.api.test_sheet_types_api import _create, _factory, _insert_key

pytestmark = pytest.mark.asyncio

CELLS = 48  # the test geometry: 4 x 12


@pytest.fixture
async def admin() -> dict[str, str]:
    return {"X-Label-Hub-Key": await _insert_key(["admin"])}


async def _sheet(key: str, used: int = 0, *, last_used: datetime | None = None) -> str:
    async with _factory()() as s:
        sheet = await ss.create_sheet(
            s,
            type_key=key,
            version=None,
            id_placement="manual",
            used_cells=[MarkedCell(cell=n) for n in range(1, used + 1)],
            note=None,
            actor="test",
        )
        code = sheet.code
    if last_used is not None:
        async with _factory()() as s:
            stmt = update(Sheet).where(col(Sheet.code) == code).values(last_used_at=last_used)
            await s.execute(stmt)
            await s.commit()
    return code


async def _discard(code: str) -> None:
    async with _factory()() as s:
        await ss.discard_sheet(s, code, actor="test")


async def _list(client: Any, admin: dict[str, str], **params: Any) -> list[dict[str, Any]]:
    resp = await client.get("/api/v1/sheet-types", params=params, headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _by_key(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["key"]: r for r in rows}


async def test_usage_numbers_equal_the_sheets_table(api_client_with_seed, admin):
    await _create(api_client_with_seed, admin)
    now = datetime.now(UTC).replace(microsecond=0)
    await _sheet("acme-t1", 2, last_used=now - timedelta(days=2))
    await _sheet("acme-t1", CELLS, last_used=now - timedelta(days=1))  # used up
    open_late = await _sheet("acme-t1", 5, last_used=now)
    gone = await _sheet("acme-t1", 1, last_used=now - timedelta(days=3))
    await _discard(gone)

    async with _factory()() as s:
        t = await ts.get_type(s, "acme-t1")
        sheets = list((await s.execute(select(Sheet).where(col(Sheet.type_id) == t.id))).scalars())
        taken = {
            sh.id: len(
                (
                    await s.execute(
                        select(SheetCell).where(
                            col(SheetCell.sheet_id) == sh.id,
                            col(SheetCell.state).in_(("used", "reserved", "unconfirmed")),
                        )
                    )
                ).all()
            )
            for sh in sheets
        }
    open_sheets = [sh for sh in sheets if sh.state == "active" and CELLS - taken[sh.id] > 0]
    row = _by_key(await _list(api_client_with_seed, admin))["acme-t1"]
    assert row["sheets_total"] == len(sheets) == 4
    assert row["sheets_with_free_cells"] == len(open_sheets) == 2
    assert row["free_labels"] == sum(CELLS - taken[sh.id] for sh in open_sheets) == 46 + 43
    assert row["sheets_in_use"] == 4  # the old field keeps its meaning
    assert row["usage_state"] == "in_use"
    assert datetime.fromisoformat(row["last_used_at"].replace("Z", "+00:00")) == now
    assert open_late  # the newest use is the one reported
    detail = (await api_client_with_seed.get("/api/v1/sheet-types/acme-t1", headers=admin)).json()
    assert {k: detail[k] for k in ("sheets_total", "free_labels", "usage_state")} == {
        "sheets_total": 4,
        "free_labels": 89,
        "usage_state": "in_use",
    }


async def test_usage_states(api_client_with_seed, admin):
    for key, prefix in (("acme-open", "AOP"), ("acme-done", "ADN"), ("acme-none", "ANO")):
        await _create(api_client_with_seed, admin, key, prefix)
    await _sheet("acme-open", 1)
    await _sheet("acme-done", CELLS)
    await _discard(await _sheet("acme-done", 0))
    rows = _by_key(await _list(api_client_with_seed, admin))
    assert rows["acme-open"]["usage_state"] == "in_use"
    assert rows["acme-done"]["usage_state"] == "used_before"
    assert (rows["acme-done"]["sheets_with_free_cells"], rows["acme-done"]["free_labels"]) == (0, 0)
    assert rows["acme-none"]["usage_state"] == "unused"
    unused = [r["key"] for r in await _list(api_client_with_seed, admin, usage_state="unused")]
    assert "acme-none" in unused and "acme-open" not in unused and "acme-done" not in unused


async def _bulk_sheets(type_ids: list[tuple[UUID, UUID, str]], per_type: int) -> int:
    rows: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    for type_id, version_id, prefix in type_ids:
        for n in range(per_type):
            sheet_id = uuid4()
            rows.append(
                {
                    "id": sheet_id,
                    "code": f"{prefix}-{n:05d}",
                    "type_id": type_id,
                    "type_version_id": version_id,
                    "state": "active" if n % 3 else "discarded",
                    "id_placement": "manual",
                    "id_printed": False,
                    "feed_mark_printed": False,
                    "created_at": now,
                    "last_used_at": now - timedelta(minutes=n),
                }
            )
            if n % 2:
                cells.append(
                    {
                        "sheet_id": sheet_id,
                        "cell_no": 1,
                        "state": "used",
                        "source": "manual",
                        "marked_by_hand": True,
                        "updated_at": now,
                    }
                )
    async with _factory()() as s:
        await s.execute(insert(Sheet), rows)
        await s.execute(insert(SheetCell), cells)
        await s.commit()
    return len(rows)


async def test_usage_list_is_one_statement_for_2000_sheets(
    api_client_with_seed, admin, monkeypatch
):
    targets = []
    for i in range(4):
        await _create(api_client_with_seed, admin, f"acme-u{i}", f"AU{i}")
    async with _factory()() as s:
        for i in range(4):
            t = await ts.get_type(s, f"acme-u{i}")
            targets.append((t.id, (await ts.current_version(s, t.id)).id, f"AU{i}"))
    assert await _bulk_sheets(targets, 500) == 2000

    import app.db.engine as engine_module

    statements: list[str] = []

    def _count(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    async with _factory()() as s:
        event.listen(engine_module.engine.sync_engine, "before_cursor_execute", _count)
        try:
            usage = await repo.usage_by_type(s)
        finally:
            event.remove(engine_module.engine.sync_engine, "before_cursor_execute", _count)
    assert len(statements) == 1, statements
    for type_id, _, _ in targets:
        u = usage[type_id]
        assert u.sheets_total == 500
        active = sum(1 for n in range(500) if n % 3)
        assert u.sheets_with_free_cells == active
        assert u.free_labels == sum(CELLS - (1 if n % 2 else 0) for n in range(500) if n % 3)

    calls: list[int] = []
    real = repo.usage_by_type

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(repo, "usage_by_type", _spy)
    rows = _by_key(await _list(api_client_with_seed, admin))
    assert len(calls) == 1  # the list asks once for every type, never per type
    assert rows["acme-u0"]["sheets_total"] == 500


async def test_sort_by_usage_and_last_used(api_client_with_seed, admin):
    now = datetime.now(UTC).replace(microsecond=0)
    for key, prefix in (("acme-a", "AAA"), ("acme-b", "ABB"), ("acme-c", "ACC")):
        await _create(api_client_with_seed, admin, key, prefix)
    await _sheet("acme-a", 40, last_used=now)  # one open sheet, 8 free
    await _sheet("acme-b", 1, last_used=now - timedelta(days=5))  # two open sheets
    await _sheet("acme-b", 1, last_used=now - timedelta(days=4))
    # acme-c: never used

    by_usage = [r["key"] for r in await _list(api_client_with_seed, admin, sort="usage")]
    ours = [k for k in by_usage if k.startswith("acme-")]
    assert ours == ["acme-b", "acme-a", "acme-c"]

    by_last = [r["key"] for r in await _list(api_client_with_seed, admin, sort="last_used")]
    ours = [k for k in by_last if k.startswith("acme-")]
    assert ours == ["acme-a", "acme-b", "acme-c"]
    assert by_last.index("acme-b") < by_last.index("avery-5160")  # never used sort last

    paged = await api_client_with_seed.get(
        "/api/v1/sheet-types", params={"sort": "usage", "limit": 1}, headers=admin
    )
    assert [r["key"] for r in paged.json()] == ["acme-b"]
    assert "sort=usage" in paged.headers["link"]


async def test_filter_unused_since(api_client_with_seed, admin):
    now = datetime.now(UTC)
    for key, prefix in (("acme-old", "AOL"), ("acme-new", "ANW"), ("acme-never", "ANV")):
        await _create(api_client_with_seed, admin, key, prefix)
    await _sheet("acme-old", 1, last_used=now - timedelta(days=40))
    await _sheet("acme-new", 1, last_used=now - timedelta(days=1))
    cutoff = (now - timedelta(days=10)).date()
    keys = [
        r["key"] for r in await _list(api_client_with_seed, admin, unused_since=cutoff.isoformat())
    ]
    assert "acme-old" in keys and "acme-never" in keys
    assert "acme-new" not in keys
    today = [
        r["key"]
        for r in await _list(api_client_with_seed, admin, unused_since=date.today().isoformat())
    ]
    assert "acme-new" in today  # used yesterday: not used since today


async def test_facets_respect_usage_state(api_client_with_seed, admin):
    await _create(api_client_with_seed, admin, "acme-used", "AUS", vendor="Zeta Labels")
    await _create(api_client_with_seed, admin, "acme-idle", "AID", vendor="Omega Labels")
    await _sheet("acme-used", 1)
    in_use = (
        await api_client_with_seed.get(
            "/api/v1/sheet-types/facets", params={"usage_state": "in_use"}, headers=admin
        )
    ).json()
    assert in_use["vendors"] == [{"value": "Zeta Labels", "count": 1}]
    unused = (
        await api_client_with_seed.get(
            "/api/v1/sheet-types/facets", params={"usage_state": "unused"}, headers=admin
        )
    ).json()
    vendors = {v["value"] for v in unused["vendors"]}
    assert "Omega Labels" in vendors and "Zeta Labels" not in vendors
