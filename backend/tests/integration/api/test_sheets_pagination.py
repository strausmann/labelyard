# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""``GET /api/v1/sheets`` is bounded at 500 rows with a ``Link`` to the next page (#204, ADR 0021).

The OC-34 order and every filter (``full``, ``q``, ``back_missing``, ``hide_used_up``) run in SQL,
so a page is never thinned out after the fact.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import app.models  # noqa: F401
import pytest
from app.models.sheet import Sheet, SheetCell, SheetType, SheetTypeVersion
from sqlalchemy import select

from tests.integration.api.test_sheet_types_api import _body, _factory, _insert_key

pytestmark = pytest.mark.asyncio

TOTAL = 48  # the acme-t1 type of test_sheet_types_api: 4 x 12 cells


@pytest.fixture
async def reader() -> dict[str, str]:
    return {"X-Label-Hub-Key": await _insert_key(["read"])}


@pytest.fixture
async def ready(api_client_with_seed):
    admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
    resp = await api_client_with_seed.post(
        "/api/v1/sheet-types", json=_body("acme-t1", "AT1"), headers=admin
    )
    assert resp.status_code == 201, resp.text
    return api_client_with_seed


async def _insert(specs: list[dict[str, Any]]) -> None:
    """Sheets straight into the database: ``code``, ``used`` cells and sheet columns."""
    async with _factory()() as s:
        t = (await s.execute(select(SheetType).where(SheetType.key == "acme-t1"))).scalar_one()
        v = (
            await s.execute(select(SheetTypeVersion).where(SheetTypeVersion.type_id == t.id))
        ).scalar_one()
        for spec in specs:
            used = spec.pop("used", 0)
            sheet = Sheet(
                type_id=t.id,
                type_version_id=v.id,
                id_placement=spec.pop("id_placement", "manual"),
                **spec,
            )
            s.add(sheet)
            await s.flush()
            for n in range(1, used + 1):
                s.add(SheetCell(sheet_id=sheet.id, cell_no=n, state="used", source="hub"))
        await s.commit()


def _codes(resp) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [r["code"] for r in resp.json()]


def _next(resp) -> str | None:
    link = resp.headers.get("link")
    if link is None:
        return None
    url, rel = (part.strip() for part in link.split(";"))
    assert rel == 'rel="next"'
    return url.strip("<>")


async def _all_pages(client, headers, params: dict[str, Any]) -> list[str]:
    resp = await client.get("/api/v1/sheets", params=params, headers=headers)
    codes = _codes(resp)
    seen = 0
    while (url := _next(resp)) is not None:
        seen += 1
        assert seen < 50
        resp = await client.get(url, headers=headers)
        codes += _codes(resp)
    return codes


async def test_list_returns_at_most_500_and_a_next_link(ready, reader) -> None:
    await _insert([{"code": f"AT1-{n:04d}"} for n in range(1, 502)])

    resp = await ready.get("/api/v1/sheets", headers=reader)

    assert len(_codes(resp)) == 500
    url = _next(resp)
    assert url is not None
    parts = urlsplit(url)
    assert parts.path == "/api/v1/sheets"
    assert parse_qs(parts.query) == {"limit": ["500"], "offset": ["500"]}


async def test_link_keeps_the_other_query_parameters(ready, reader) -> None:
    await _insert([{"code": f"AT1-{n:04d}"} for n in range(1, 6)])

    resp = await ready.get(
        "/api/v1/sheets", params={"type": "acme-t1", "q": "AT1", "limit": 2}, headers=reader
    )

    url = _next(resp)
    assert url is not None
    assert parse_qs(urlsplit(url).query) == {
        "type": ["acme-t1"],
        "q": ["AT1"],
        "limit": ["2"],
        "offset": ["2"],
    }


async def test_second_page_has_no_link(ready, reader) -> None:
    await _insert([{"code": f"AT1-{n:04d}"} for n in range(1, 502)])

    resp = await ready.get("/api/v1/sheets", params={"offset": 500}, headers=reader)

    assert _codes(resp) == ["AT1-0501"]
    assert "link" not in resp.headers


async def test_limit_is_bounded(ready, reader) -> None:
    for bad in ({"limit": 501}, {"limit": 0}, {"offset": -1}):
        resp = await ready.get("/api/v1/sheets", params=bad, headers=reader)
        assert resp.status_code == 422, bad


async def test_order_is_the_oc34_order_across_pages(ready, reader) -> None:
    used = {"AT1-0001": 0, "AT1-0002": 10, "AT1-0003": 48, "AT1-0004": 5, "AT1-0005": 10}
    used |= {"AT1-0006": 47, "AT1-10000": 10, "AT1-0007": 46}
    await _insert([{"code": c, "used": u} for c, u in used.items()])

    def order(code: str) -> tuple[int, int, int, str]:
        free = TOTAL - used[code]
        return (0 if free >= 3 else 1, -free, len(code), code)

    expected = sorted(used, key=order)
    got = await _all_pages(ready, reader, {"free_at_least": 3, "limit": 3})

    assert got == expected
    assert expected[:3] == ["AT1-0001", "AT1-0004", "AT1-0002"]  # the order is not trivial
    assert expected.index("AT1-0005") < expected.index("AT1-10000")  # (length, code)


async def test_q_full_back_missing_filter_before_paging(ready, reader) -> None:
    await _insert(
        [
            {"code": "AT1-0001"},
            {"code": "AT1-0002", "used": TOTAL},
            {"code": "AT1-0003", "used": TOTAL},
            {"code": "AT1-0004"},
            {"code": "AT1-0005", "used": TOTAL},
            {"code": "AT1-0006", "id_placement": "back_side"},
            {"code": "AT1-0007", "back_side_state": "pending"},
            {"code": "AT1-0008", "id_placement": "back_side", "id_printed": True},
            {"code": "AT1-0009", "back_side_state": "confirmed"},
            {"code": "AT1-0010", "used": TOTAL, "back_side_state": "pending"},
            {"code": "AT1-0011", "id_placement": "back_side", "state": "discarded"},
        ]
    )

    full = await ready.get("/api/v1/sheets", params={"state": "full", "limit": 2}, headers=reader)
    assert _codes(full) == ["AT1-0002", "AT1-0003"]  # no page thinned out by a later filter
    assert await _all_pages(ready, reader, {"state": "full", "limit": 2}) == [
        "AT1-0002",
        "AT1-0003",
        "AT1-0005",
        "AT1-0010",
    ]

    q = await ready.get("/api/v1/sheets", params={"q": "at1-001", "limit": 1}, headers=reader)
    assert _codes(q) == ["AT1-0010"]

    back = await _all_pages(ready, reader, {"back_missing": "true", "state": "all", "limit": 1})
    assert back == ["AT1-0006", "AT1-0007", "AT1-0010"]

    roomy = await ready.get(
        "/api/v1/sheets", params={"hide_used_up": "true", "limit": 4}, headers=reader
    )
    assert _codes(roomy) == ["AT1-0001", "AT1-0004", "AT1-0006", "AT1-0007"]
    rest = await _all_pages(ready, reader, {"hide_used_up": "true", "limit": 4})
    assert rest == ["AT1-0001", "AT1-0004", "AT1-0006", "AT1-0007", "AT1-0008", "AT1-0009"]


async def test_used_up_hidden_header_counts_the_hidden_sheets(ready, reader) -> None:
    await _insert(
        [
            {"code": "AT1-0001"},
            {"code": "AT1-0002", "used": TOTAL},
            {"code": "AT1-0003", "used": TOTAL},
            {"code": "AT1-0004", "used": TOTAL, "id_placement": "back_side"},
            {"code": "AT1-0005", "used": TOTAL, "state": "discarded"},
        ]
    )

    hidden = await ready.get(
        "/api/v1/sheets", params={"hide_used_up": "true", "limit": 1}, headers=reader
    )
    assert _codes(hidden) == ["AT1-0001"]
    assert hidden.headers["x-used-up-hidden"] == "3"  # over every page, not only this one

    narrowed = await ready.get(
        "/api/v1/sheets",
        params={"hide_used_up": "true", "back_missing": "true", "state": "all"},
        headers=reader,
    )
    assert _codes(narrowed) == []
    assert narrowed.headers["x-used-up-hidden"] == "1"  # only what the other filters match

    plain = await ready.get("/api/v1/sheets", headers=reader)
    assert "x-used-up-hidden" not in plain.headers
    assert len(_codes(plain)) == 4
