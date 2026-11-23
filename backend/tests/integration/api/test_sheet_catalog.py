# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The sheet-type catalog API: list, detail, install and update (ADR 0028, spec 6.1.2, S12)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from app.catalog import loader
from app.catalog.upgrade import CATALOG_ACTOR, is_locally_edited, provenance_inputs
from app.models.sheet import SheetType, SheetTypeVersion
from app.sheets import type_service as ts
from sqlalchemy import func, select
from sqlmodel import col

from tests.integration.api.test_sheet_types_api import _create, _err, _factory, _insert_key

pytestmark = pytest.mark.asyncio

K5160 = "avery-5160"  # unverified, Letter, no backing design
K6121 = "avery-zweckform-6121"  # verified, embeds the UltraGrip backing design
ULTRAGRIP = "avery-zweckform-ultragrip"


@pytest.fixture(autouse=True)
def _fresh_catalog_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    loader.clear_caches()
    yield
    monkeypatch.undo()
    loader.clear_caches()


@pytest.fixture
async def admin() -> dict[str, str]:
    return {"X-Label-Hub-Key": await _insert_key(["admin"])}


@pytest.fixture
async def reader() -> dict[str, str]:
    return {"X-Label-Hub-Key": await _insert_key(["read"])}


async def _install(client: Any, admin: dict[str, str], key: str, **body: Any) -> Any:
    return await client.post(f"/api/v1/sheet-catalog/{key}/install", json=body, headers=admin)


async def _unedited(key: str) -> bool:
    async with _factory()() as s:
        t = await ts.get_type(s, key)
        current, design = await provenance_inputs(s, t)
        return not is_locally_edited(t, current, design)


def _offer(monkeypatch: pytest.MonkeyPatch, key: str, revision: int, **changes: Any) -> None:
    """Make the catalog of the image carry ``revision`` of ``key`` with ``changes``.

    ``geometry`` in ``changes`` is a dict of geometry fields; every other change is a type field.
    """
    geometry = changes.pop("geometry", None)
    newer = loader.load_meta(key).model_copy(update={"revision": revision})
    metas = {**loader._all_meta(), key: newer}
    doc = loader.load_entry(key)
    version = doc.version
    if geometry is not None:
        version = version.model_copy(
            update={"geometry": version.geometry.model_copy(update=geometry)}
        )
    changed = doc.model_copy(
        update={"type": doc.type.model_copy(update=changes), "version": version}
    )
    real_entry = loader.load_entry
    monkeypatch.setattr(loader, "_all_meta", lambda: metas)
    monkeypatch.setattr(loader, "load_entry", lambda k: changed if k == key else real_entry(k))


# ---------------------------------------------------------------------------
# List and detail
# ---------------------------------------------------------------------------


@pytest.mark.fresh_install
async def test_catalog_lists_entries_with_installed_flags(api_client_with_seed, admin, reader):
    resp = await api_client_with_seed.get("/api/v1/sheet-catalog", headers=reader)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == len(loader.load_index()) == 39
    assert not any(e["installed"] for e in body["items"])
    assert (await _install(api_client_with_seed, admin, K5160)).status_code == 201

    body = (await api_client_with_seed.get("/api/v1/sheet-catalog", headers=reader)).json()
    flags = {e["key"]: (e["installed"], e["update_available"]) for e in body["items"]}
    assert flags[K5160] == (True, False)
    assert sum(installed for installed, _ in flags.values()) == 1
    assert [e["key"] for e in body["items"]] == sorted(flags)


@pytest.mark.fresh_install
async def test_catalog_search_by_product_code(api_client_with_seed, reader):
    resp = await api_client_with_seed.get(
        "/api/v1/sheet-catalog", params={"q": "5260"}, headers=reader
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [e["key"] for e in body["items"]] == [K5160]
    assert body["total"] == 1
    assert body["facets"]["vendors"] == [{"value": "Avery", "count": 1}]
    assert body["facets"]["page_sizes"] == [{"value": "Letter", "count": 1}]


@pytest.mark.fresh_install
async def test_catalog_facets_ignore_the_vendor_filter(api_client_with_seed, reader):
    everything = (await api_client_with_seed.get("/api/v1/sheet-catalog", headers=reader)).json()
    one = (
        await api_client_with_seed.get(
            "/api/v1/sheet-catalog", params={"vendor": "avery", "limit": 2}, headers=reader
        )
    ).json()
    assert one["facets"] == everything["facets"]
    assert len(one["items"]) == 2
    assert {e["vendor"] for e in one["items"]} == {"Avery"}
    assert one["total"] > 2


@pytest.mark.fresh_install
async def test_catalog_filters_verified_entries(api_client_with_seed, reader):
    body = (
        await api_client_with_seed.get(
            "/api/v1/sheet-catalog", params={"verified": "true"}, headers=reader
        )
    ).json()
    keys = [e["key"] for e in body["items"]]
    assert K6121 in keys
    assert keys == sorted(e.key for e in loader.load_index() if e.verified)


@pytest.mark.fresh_install
async def test_catalog_entry_detail_has_its_geometry(api_client_with_seed, reader):
    resp = await api_client_with_seed.get(f"/api/v1/sheet-catalog/{K6121}", headers=reader)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["prefix"] == "A6121"
    assert body["geometry"]["cols"] == 5 and body["geometry"]["rows"] == 13
    assert body["installed"] is False
    missing = await api_client_with_seed.get("/api/v1/sheet-catalog/acme-nope", headers=reader)
    assert missing.status_code == 404
    assert _err(missing)["error_code"] == "catalog_entry_not_found"


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


@pytest.mark.fresh_install
async def test_install_creates_the_type_with_provenance_and_origin_seed(
    api_client_with_seed, admin
):
    resp = await _install(api_client_with_seed, admin, K5160)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    doc = loader.load_entry(K5160)
    assert body["origin"] == "seed"
    assert body["catalog_key"] == K5160
    assert body["catalog_revision"] == loader.load_meta(K5160).revision
    assert body["prefix"] == doc.type.prefix
    assert body["product_codes"] == doc.type.product_codes
    assert body["usage_state"] == "unused"
    async with _factory()() as s:
        t = await ts.get_type(s, K5160)
        v = await ts.current_version(s, t.id)
        assert v.created_by == CATALOG_ACTOR
        assert t.catalog_fields_hash == loader.fields_hash(doc.type)


@pytest.mark.fresh_install
@pytest.mark.parametrize("key", [K5160, K6121])
async def test_type_is_unedited_right_after_install(api_client_with_seed, admin, key):
    resp = await _install(api_client_with_seed, admin, key)
    assert resp.status_code == 201, resp.text
    assert resp.json()["verified"] is loader.load_entry(key).type.verified  # D19 not applied
    assert await _unedited(key)


@pytest.mark.fresh_install
async def test_install_6121_links_the_ultragrip_backing_design(api_client_with_seed, admin):
    resp = await _install(api_client_with_seed, admin, K6121)
    assert resp.status_code == 201, resp.text
    design = resp.json()["backing_design"]
    assert design["key"] == ULTRAGRIP
    assert design["origin"] == "seed"
    assert len(design["strips"]) == 2
    assert resp.json()["verified"] is True


@pytest.mark.fresh_install
async def test_install_as_new_key_is_imported_without_provenance(api_client_with_seed, admin):
    resp = await _install(
        api_client_with_seed, admin, K5160, as_new_key="my-avery-5160", prefix="MY5160"
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["key"], body["prefix"], body["origin"]) == ("my-avery-5160", "MY5160", "imported")
    assert body["catalog_key"] is None and body["catalog_revision"] is None
    async with _factory()() as s:
        t = await ts.get_type(s, "my-avery-5160")
        assert t.catalog_fields_hash is None
        assert (await ts.current_version(s, t.id)).created_by != CATALOG_ACTOR
    listed = (await api_client_with_seed.get("/api/v1/sheet-catalog", headers=admin)).json()
    assert not next(e for e in listed["items"] if e["key"] == K5160)["installed"]


@pytest.mark.fresh_install
async def test_install_as_new_key_refuses_a_reserved_key(api_client_with_seed, admin):
    for reserved in ("delete-unused", "facets"):
        resp = await _install(
            api_client_with_seed, admin, K5160, as_new_key=reserved, prefix="RSV1"
        )
        assert resp.status_code == 422, resp.text
        assert _err(resp)["error_code"] == "type_key_reserved"


@pytest.mark.fresh_install
async def test_install_existing_key_is_409_type_exists(api_client_with_seed, admin):
    assert (await _install(api_client_with_seed, admin, K5160)).status_code == 201
    again = await _install(api_client_with_seed, admin, K5160)
    assert again.status_code == 409
    assert _err(again)["error_code"] == "type_exists"


@pytest.mark.fresh_install
async def test_install_taken_prefix_is_409_with_a_suggestion(api_client_with_seed, admin):
    prefix = loader.load_entry(K5160).type.prefix
    await _create(api_client_with_seed, admin, "acme-mine", prefix)
    resp = await _install(api_client_with_seed, admin, K5160)
    assert resp.status_code == 409, resp.text
    err = _err(resp)
    assert err["error_code"] == "prefix_taken"
    suggested = err["error_detail"]["suggested_prefix"]
    assert suggested and suggested != prefix
    retry = await _install(api_client_with_seed, admin, K5160, prefix=suggested)
    assert retry.status_code == 201, retry.text
    assert retry.json()["prefix"] == suggested
    assert retry.json()["catalog_key"] == K5160  # its own key: still the catalog's type


@pytest.mark.fresh_install
async def test_concurrent_install_gives_one_type_and_one_409(
    production_engine, api_client_with_seed, admin
):
    results = await asyncio.gather(
        *(_install(api_client_with_seed, admin, K6121) for _ in range(2))
    )
    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    loser = next(r for r in results if r.status_code == 409)
    assert _err(loser)["error_code"] == "type_exists"
    async with _factory()() as s:
        n = (
            await s.execute(
                select(func.count()).select_from(SheetType).where(col(SheetType.key) == K6121)
            )
        ).scalar_one()
    assert n == 1


@pytest.mark.fresh_install
async def test_install_needs_admin(api_client_with_seed, reader):
    resp = await _install(api_client_with_seed, reader, K5160)
    assert resp.status_code == 403, resp.text
    async with _factory()() as s:
        assert (await s.execute(select(func.count()).select_from(SheetType))).scalar_one() == 0


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------


@pytest.mark.fresh_install
async def test_update_offered_for_unedited_type_with_newer_revision(
    api_client_with_seed, admin, monkeypatch
):
    assert (await _install(api_client_with_seed, admin, K5160)).status_code == 201
    _offer(monkeypatch, K5160, 2, description="Revised address labels")
    detail = (await api_client_with_seed.get(f"/api/v1/sheet-types/{K5160}", headers=admin)).json()
    assert (detail["update_available"], detail["catalog_newer"]) == (True, False)
    listed = (await api_client_with_seed.get("/api/v1/sheet-catalog", headers=admin)).json()
    entry = next(e for e in listed["items"] if e["key"] == K5160)
    assert (entry["update_available"], entry["catalog_newer"]) == (True, False)


@pytest.mark.fresh_install
async def test_update_not_offered_for_edited_type_but_catalog_newer_shown(
    api_client_with_seed, admin, monkeypatch
):
    assert (await _install(api_client_with_seed, admin, K5160)).status_code == 201
    patched = await api_client_with_seed.patch(
        f"/api/v1/sheet-types/{K5160}", json={"description": "Mine now"}, headers=admin
    )
    assert patched.status_code == 200, patched.text
    _offer(monkeypatch, K5160, 2, description="Revised address labels")
    detail = (await api_client_with_seed.get(f"/api/v1/sheet-types/{K5160}", headers=admin)).json()
    assert (detail["update_available"], detail["catalog_newer"]) == (False, True)


@pytest.mark.fresh_install
async def test_catalog_update_adds_a_version_only_for_new_geometry(
    api_client_with_seed, admin, monkeypatch
):
    assert (await _install(api_client_with_seed, admin, K5160)).status_code == 201
    margin = loader.load_entry(K5160).version.geometry.margin_top
    _offer(monkeypatch, K5160, 2, description="Revised address labels")
    resp = await api_client_with_seed.post(
        f"/api/v1/sheet-types/{K5160}/catalog-update", headers=admin
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["description"], body["current_version"]) == ("Revised address labels", 1)
    assert body["catalog_revision"] == 2

    # revision 3 on top of revision 2: the same fields, a new geometry
    _offer(monkeypatch, K5160, 3, geometry={"margin_top": round(margin + 0.1, 2)})
    resp = await api_client_with_seed.post(
        f"/api/v1/sheet-types/{K5160}/catalog-update", headers=admin
    )
    assert resp.status_code == 200, resp.text
    assert (resp.json()["current_version"], resp.json()["catalog_revision"]) == (2, 3)
    async with _factory()() as s:
        t = await ts.get_type(s, K5160)
        versions = (
            (
                await s.execute(
                    select(SheetTypeVersion)
                    .where(col(SheetTypeVersion.type_id) == t.id)
                    .order_by(col(SheetTypeVersion.version))
                )
            )
            .scalars()
            .all()
        )
    assert [v.created_by for v in versions] == [CATALOG_ACTOR, CATALOG_ACTOR]
    assert versions[1].change_note == "Catalog revision 3"


@pytest.mark.fresh_install
async def test_type_is_unedited_right_after_a_catalog_update(
    api_client_with_seed, admin, monkeypatch
):
    assert (await _install(api_client_with_seed, admin, K5160)).status_code == 201
    _offer(monkeypatch, K5160, 2, description="Revised address labels", allow_qr=False)
    resp = await api_client_with_seed.post(
        f"/api/v1/sheet-types/{K5160}/catalog-update", headers=admin
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["update_available"] is False
    assert await _unedited(K5160)


@pytest.mark.fresh_install
async def test_catalog_update_of_edited_type_is_409(api_client_with_seed, admin, monkeypatch):
    assert (await _install(api_client_with_seed, admin, K5160)).status_code == 201
    await api_client_with_seed.patch(
        f"/api/v1/sheet-types/{K5160}", json={"notes": "my own note"}, headers=admin
    )
    _offer(monkeypatch, K5160, 2, description="Revised address labels")
    resp = await api_client_with_seed.post(
        f"/api/v1/sheet-types/{K5160}/catalog-update", headers=admin
    )
    assert resp.status_code == 409, resp.text
    assert _err(resp)["error_code"] == "locally_edited"
    detail = (await api_client_with_seed.get(f"/api/v1/sheet-types/{K5160}", headers=admin)).json()
    assert detail["catalog_revision"] == 1 and detail["notes"] == "my own note"


@pytest.mark.fresh_install
async def test_catalog_update_without_a_newer_revision_is_409_no_update(
    api_client_with_seed, admin
):
    assert (await _install(api_client_with_seed, admin, K5160)).status_code == 201
    await _create(api_client_with_seed, admin)  # a custom type has no catalog entry at all
    for key in (K5160, "acme-t1"):
        resp = await api_client_with_seed.post(
            f"/api/v1/sheet-types/{key}/catalog-update", headers=admin
        )
        assert resp.status_code == 409, resp.text
        assert _err(resp)["error_code"] == "no_update"


@pytest.mark.fresh_install
async def test_catalog_update_needs_admin(api_client_with_seed, admin, reader, monkeypatch):
    assert (await _install(api_client_with_seed, admin, K5160)).status_code == 201
    _offer(monkeypatch, K5160, 2, description="Revised address labels")
    resp = await api_client_with_seed.post(
        f"/api/v1/sheet-types/{K5160}/catalog-update", headers=reader
    )
    assert resp.status_code == 403


@pytest.mark.fresh_install
async def test_entry_removed_from_image_shows_catalog_missing(
    api_client_with_seed, admin, monkeypatch
):
    assert (await _install(api_client_with_seed, admin, K5160)).status_code == 201
    index = tuple(e for e in loader.load_index() if e.key != K5160)
    metas = {k: m for k, m in loader._all_meta().items() if k != K5160}
    monkeypatch.setattr(loader, "load_index", lambda: index)
    monkeypatch.setattr(loader, "_all_meta", lambda: metas)
    detail = await api_client_with_seed.get(f"/api/v1/sheet-types/{K5160}", headers=admin)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["catalog_missing"] is True
    assert (body["update_available"], body["catalog_newer"]) == (False, False)
    listed = (await api_client_with_seed.get("/api/v1/sheet-catalog", headers=admin)).json()
    assert K5160 not in [e["key"] for e in listed["items"]]
