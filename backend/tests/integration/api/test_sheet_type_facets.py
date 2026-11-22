# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Sheet-type facets and opt-in pagination of the type list (#219)."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.integration.api.test_sheet_types_api import _body, _insert_key

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def admin() -> dict[str, str]:
    return {"X-Label-Hub-Key": await _insert_key(["admin"])}


@pytest.fixture
async def reader() -> dict[str, str]:
    return {"X-Label-Hub-Key": await _insert_key(["read"])}


async def _make(client, admin, key: str, prefix: str, vendor: str, **geom: Any) -> None:
    body = _body(key, prefix, vendor=vendor)
    body["geometry"].update(geom)
    resp = await client.post("/api/v1/sheet-types", json=body, headers=admin)
    assert resp.status_code == 201, resp.text


async def _facets(client, reader, **params: Any) -> dict[str, dict[str, int]]:
    resp = await client.get("/api/v1/sheet-types/facets", params=params, headers=reader)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return {
        name: {f["value"]: f["count"] for f in data[name]} for name in ("vendors", "page_sizes")
    }


async def test_list_without_limit_is_the_full_array(api_client_with_seed, admin, reader) -> None:
    """(guard) No `limit`: the whole array and no Link header."""
    for n in range(3):
        await _make(api_client_with_seed, admin, f"pg-t{n}", f"PG{n}", "Pager")
    resp = await api_client_with_seed.get("/api/v1/sheet-types", headers=reader)
    assert resp.status_code == 200
    assert "link" not in resp.headers
    assert len(resp.json()) > 3


async def test_list_with_limit_pages_and_links(api_client_with_seed, admin, reader) -> None:
    for n in range(5):
        await _make(api_client_with_seed, admin, f"pg-t{n}", f"PG{n}", "Pager")
    params = {"vendor": "Pager", "limit": 2}
    first = await api_client_with_seed.get("/api/v1/sheet-types", params=params, headers=reader)
    assert [t["key"] for t in first.json()] == ["pg-t0", "pg-t1"]
    url = first.headers["link"].split(";")[0].strip("<>")
    assert parse_qs(urlsplit(url).query) == {"vendor": ["Pager"], "limit": ["2"], "offset": ["2"]}
    keys = [t["key"] for t in first.json()]
    resp = first
    while "link" in resp.headers:
        resp = await api_client_with_seed.get(
            resp.headers["link"].split(";")[0].strip("<>"), headers=reader
        )
        keys += [t["key"] for t in resp.json()]
    assert keys == [f"pg-t{n}" for n in range(5)]
    assert "link" not in resp.headers


async def test_limit_out_of_range_is_rejected(api_client_with_seed, reader) -> None:
    for bad in (0, 201):
        resp = await api_client_with_seed.get(
            "/api/v1/sheet-types", params={"limit": bad}, headers=reader
        )
        assert resp.status_code == 422


async def test_facets_ignore_the_vendor_filter(api_client_with_seed, admin, reader) -> None:
    await _make(api_client_with_seed, admin, "fa-a", "FAA", "Alpha")
    await _make(api_client_with_seed, admin, "fa-b", "FAB", "alpha")
    await _make(api_client_with_seed, admin, "fa-c", "FAC", "Beta", page_size="Letter")

    everything = await _facets(api_client_with_seed, reader, q="fa-")
    assert everything["vendors"] == {"Alpha": 2, "Beta": 1}
    assert everything["page_sizes"] == {"A4": 2, "Letter": 1}

    listed = await api_client_with_seed.get(
        "/api/v1/sheet-types", params={"q": "fa-", "vendor": "Beta"}, headers=reader
    )
    assert [t["key"] for t in listed.json()] == ["fa-c"]  # the list is narrowed ...
    # ... and the facets of the same filter are not: "vendor" is not a facet parameter
    again = await _facets(api_client_with_seed, reader, q="fa-", vendor="Beta")
    assert again == everything


async def test_facet_vendors_are_sorted_case_insensitively(
    api_client_with_seed, admin, reader
) -> None:
    for key, prefix, vendor in (
        ("so-a", "SOA", "zeta"),
        ("so-b", "SOB", "Alpha"),
        ("so-c", "SOC", "beta"),
    ):
        await _make(api_client_with_seed, admin, key, prefix, vendor)
    resp = await api_client_with_seed.get(
        "/api/v1/sheet-types/facets", params={"q": "so-"}, headers=reader
    )
    assert [f["value"] for f in resp.json()["vendors"]] == ["Alpha", "beta", "zeta"]


async def test_facets_respect_other_filters(api_client_with_seed, admin, reader) -> None:
    await _make(api_client_with_seed, admin, "fo-a", "FOA", "Alpha")
    await _make(api_client_with_seed, admin, "fo-c", "FOC", "Beta", page_size="Letter")
    only_letter = await _facets(api_client_with_seed, reader, q="fo-", page="Letter")
    assert only_letter["vendors"] == {"Beta": 1}


async def test_facets_respect_include_archived(api_client_with_seed, admin, reader) -> None:
    await _make(api_client_with_seed, admin, "ar-a", "ARA", "Alpha")
    await _make(api_client_with_seed, admin, "ar-b", "ARB", "Gone")
    resp = await api_client_with_seed.post("/api/v1/sheet-types/ar-b/archive", headers=admin)
    assert resp.status_code == 200
    assert (await _facets(api_client_with_seed, reader, q="ar-"))["vendors"] == {"Alpha": 1}
    with_archived = await _facets(api_client_with_seed, reader, q="ar-", include_archived="true")
    assert with_archived["vendors"] == {"Alpha": 1, "Gone": 1}


async def test_facets_route_is_not_a_type_key(api_client_with_seed, reader) -> None:
    resp = await api_client_with_seed.get("/api/v1/sheet-types/facets", headers=reader)
    assert resp.status_code == 200
    assert "vendors" in resp.json()


async def test_facets_need_a_key(api_client_with_seed) -> None:
    resp = await api_client_with_seed.get("/api/v1/sheet-types/facets")
    assert resp.status_code in (401, 403)


# --- reserved keys (Codex P2: a type called "facets" would be shadowed by the route) ---


def _static_segments(schema: dict[str, Any]) -> set[str]:
    lead = "/api/v1/sheet-types/"
    return {
        p[len(lead) :].split("/", 1)[0]
        for p in schema["paths"]
        if p.startswith(lead) and not p[len(lead) :].startswith("{")
    }


async def test_every_static_segment_under_sheet_types_is_reserved(api_client_with_seed) -> None:
    """Guard: a route added later (Task 8c's catalog) reserves its segment automatically."""
    from app.sheets.naming import RESERVED_TYPE_KEYS

    schema = (await api_client_with_seed.get("/openapi.json")).json()
    segments = _static_segments(schema)
    assert {"facets", "import"} <= segments
    assert segments <= RESERVED_TYPE_KEYS


@pytest.mark.parametrize("key", ["facets", "import"])
async def test_creating_a_type_with_a_reserved_key_is_422(api_client_with_seed, admin, key) -> None:
    resp = await api_client_with_seed.post(
        "/api/v1/sheet-types", json=_body(key, "RSV"), headers=admin
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "type_key_reserved"


def _doc(key: str) -> dict[str, Any]:
    return {
        "format": "label-printer-hub.sheet-type",
        "format_version": 1,
        "exported_at": "2026-11-01T08:00:00Z",
        "exported_by_hub": "test",
        "type": {
            "key": key,
            "prefix": "RSV",
            "allow_qr": True,
            "product_codes": [],
            "verified": False,
        },
        "version": {"version": 1, "geometry": _body()["geometry"]},
    }


async def test_importing_a_reserved_key_is_422(api_client_with_seed, admin) -> None:
    resp = await api_client_with_seed.post(
        "/api/v1/sheet-types/import", json=_doc("facets"), headers=admin
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "type_key_reserved"


async def test_importing_as_a_reserved_new_key_is_422(api_client_with_seed, admin) -> None:
    await _make(api_client_with_seed, admin, "rs-orig", "RSO", "Alpha")
    resp = await api_client_with_seed.post(
        "/api/v1/sheet-types/import",
        json=_doc("rs-orig"),
        params={"mode": "as_new_key", "new_key": "facets", "new_prefix": "RSF"},
        headers=admin,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "type_key_reserved"


async def test_vendor_filter_folds_non_ascii_case(api_client_with_seed, admin, reader) -> None:
    """SQLite's lower() only folds ASCII; "Ärger" must match "ärger" and be listed as a facet."""
    await _make(api_client_with_seed, admin, "ae-a", "AEA", "Ärger")
    for spelling in ("ärger", "Ärger"):
        resp = await api_client_with_seed.get(
            "/api/v1/sheet-types", params={"vendor": spelling}, headers=reader
        )
        assert [t["key"] for t in resp.json()] == ["ae-a"], spelling
    assert (await _facets(api_client_with_seed, reader, q="ae-"))["vendors"] == {"Ärger": 1}
