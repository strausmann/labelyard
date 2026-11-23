# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The catalog service and the delete/usage parts of the type service, called directly.

The HTTP behaviour is covered under ``tests/integration/api``; these tests pin the branches of
the services (refusals, filters, sorting) without an app.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from app.catalog import loader
from app.catalog import service as cs
from app.catalog.upgrade import CATALOG_ACTOR, is_locally_edited, provenance_inputs
from app.models.app_setting import AppSetting
from app.models.backing_design import BackingDesign
from app.models.sheet import Sheet, SheetCell, SheetIdCounter
from app.repositories import sheet_types as repo
from app.settings_store.registry import (
    GROCY_LABEL_DEFAULT_SHEET_TYPE_KEY,
    grocy_label_sheet_type_key,
)
from app.sheets import type_service as ts
from app.sheets.errors import (
    BackingDesignDiffersError,
    BackingDesignPageSizeMismatchError,
    CatalogEntryNotFoundError,
    InvalidFieldError,
    NoCatalogUpdateError,
    PrefixTakenError,
    SheetTypeInUseError,
    SheetTypeNotFoundError,
    TypeExistsError,
    TypeKeyReservedError,
    TypeLocallyEditedError,
    TypeNotUnusedError,
)
from app.sheets.geometry import Geometry
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import col

pytestmark = pytest.mark.asyncio

K5160 = "avery-5160"  # Letter, no design
K6121 = "avery-zweckform-6121"  # A4, embeds the UltraGrip design

G_A4 = Geometry(
    page_size="A4",
    page_w=21000,
    page_h=29700,
    label_w=4570,
    label_h=2120,
    cols=4,
    rows=12,
    pitch_x=4850,
    pitch_y=2120,
    margin_top=2150,
    margin_left=840,
    corner_radius=None,
    shape="rectangle",
)


@pytest.fixture(autouse=True)
def _fresh_catalog_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    loader.clear_caches()
    yield
    monkeypatch.undo()
    loader.clear_caches()


@pytest_asyncio.fixture
async def session(async_session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[Any]:
    async with async_session_factory() as s:
        yield s


def _fields(key: str, prefix: str, **kw: Any) -> ts.TypeFields:
    return ts.TypeFields(
        key=key, prefix=prefix, vendor=kw.pop("vendor", "Acme"), product_codes=[],
        description=None, allow_qr=None, **kw,
    )  # fmt: skip


async def _custom(session: AsyncSession, key: str, prefix: str, **kw: Any) -> None:
    await ts.create_type(session, _fields(key, prefix, **kw), G_A4, actor="t")


async def _sheet(session: AsyncSession, key: str, code: str, **kw: Any) -> Sheet:
    t = await ts.get_type(session, key)
    v = await ts.current_version(session, t.id)
    sheet = Sheet(code=code, type_id=t.id, type_version_id=v.id, id_placement="manual", **kw)
    session.add(sheet)
    await session.commit()
    return sheet


def _offer(monkeypatch: pytest.MonkeyPatch, key: str, revision: int, **changes: Any) -> None:
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
    real = loader.load_entry
    monkeypatch.setattr(loader, "_all_meta", lambda: metas)
    monkeypatch.setattr(loader, "load_entry", lambda k: changed if k == key else real(k))


async def _unedited(session: AsyncSession, key: str) -> bool:
    t = await ts.get_type(session, key)
    current, design = await provenance_inputs(session, t)
    return not is_locally_edited(t, current, design)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


async def test_list_entries_filters(session: AsyncSession) -> None:
    every = await cs.list_entries(
        session, q=None, page=None, w=None, h=None, verified=None, vendor=None,
        limit=200, offset=0,
    )  # fmt: skip
    assert every.total == len(loader.load_index())
    assert sum(n for _, n in every.page_sizes) == every.total

    letter = await cs.list_entries(
        session, q=None, page="Letter", w=66.67, h=25.4, verified=False, vendor=None,
        limit=200, offset=0,
    )  # fmt: skip
    assert K5160 in [i.entry.key for i in letter.items]
    assert all(i.entry.page_size == "Letter" for i in letter.items)

    none = await cs.list_entries(
        session, q=None, page=None, w=999, h=None, verified=None, vendor=None,
        limit=200, offset=0,
    )  # fmt: skip
    assert none.total == 0 and none.vendors == []
    tall = await cs.list_entries(
        session, q=None, page=None, w=None, h=999, verified=None, vendor=None,
        limit=200, offset=0,
    )  # fmt: skip
    assert tall.total == 0

    paged = await cs.list_entries(
        session, q="avery", page=None, w=None, h=None, verified=None, vendor="AVERY",
        limit=2, offset=1,
    )  # fmt: skip
    assert len(paged.items) == 2 and paged.total > 2
    assert (
        paged.items[0].entry.key
        == sorted(e.key for e in loader.load_index() if (e.vendor or "").casefold() == "avery")[1]
    )


async def test_installed_state_counts_the_key_and_the_provenance(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _custom(session, K5160, "MY1")  # a custom type that took the entry's key
    await cs.install(session, K6121, as_new_key=None, prefix=None, actor="admin")
    item, doc = await cs.get_entry(session, K5160)
    assert item.installed and not item.state.update_available
    assert doc.type.key == K5160
    _offer(monkeypatch, K6121, 2, description="Newer")
    item, _ = await cs.get_entry(session, K6121)
    assert item.installed and item.state.update_available
    with pytest.raises(CatalogEntryNotFoundError):
        cs.entry_of("acme-nope")


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


async def test_install_under_its_own_key(session: AsyncSession) -> None:
    t = await cs.install(session, K6121, as_new_key=None, prefix=None, actor="admin")
    assert (t.origin, t.catalog_key, t.catalog_revision) == ("seed", K6121, 1)
    assert t.verified is True and t.created_by == "admin"
    v = await ts.current_version(session, t.id)
    assert v.created_by == CATALOG_ACTOR
    design = await session.get(BackingDesign, t.backing_design_id)
    assert design is not None and design.origin == "seed"
    assert await _unedited(session, K6121)


async def test_install_reuses_an_existing_design(session: AsyncSession) -> None:
    await cs.install(session, K6121, as_new_key="my-6121", prefix="MY6121", actor="admin")
    t = await cs.install(session, K6121, as_new_key=None, prefix=None, actor="admin")
    mine = await ts.get_type(session, "my-6121")
    assert mine.origin == "imported" and mine.catalog_key is None
    assert t.backing_design_id == mine.backing_design_id
    design = await session.get(BackingDesign, t.backing_design_id)
    assert design is not None and design.origin == "imported"  # the first install made it


async def test_install_as_its_own_key_written_as_new_key(session: AsyncSession) -> None:
    t = await cs.install(session, K5160, as_new_key=K5160, prefix=None, actor="admin")
    assert (t.origin, t.catalog_key) == ("seed", K5160)


async def test_install_refusals(session: AsyncSession) -> None:
    with pytest.raises(CatalogEntryNotFoundError):
        await cs.install(session, "acme-nope", as_new_key=None, prefix=None, actor="a")
    with pytest.raises(InvalidFieldError):
        await cs.install(session, K5160, as_new_key="Bad Key", prefix=None, actor="a")
    with pytest.raises(TypeKeyReservedError):
        await cs.install(session, K5160, as_new_key="facets", prefix="FAC1", actor="a")
    with pytest.raises(InvalidFieldError):
        await cs.install(session, K5160, as_new_key=None, prefix="bad", actor="a")
    await cs.install(session, K5160, as_new_key=None, prefix=None, actor="a")
    with pytest.raises(TypeExistsError):
        await cs.install(session, K5160, as_new_key=None, prefix="OTHER1", actor="a")


async def test_install_taken_prefix_suggests_a_free_one(session: AsyncSession) -> None:
    prefix = loader.load_entry(K5160).type.prefix
    await _custom(session, "acme-a", prefix)
    await _custom(session, "acme-b", prefix[:9] + "2")
    with pytest.raises(PrefixTakenError) as exc:
        await cs.install(session, K5160, as_new_key=None, prefix=None, actor="a")
    assert exc.value.detail == {"suggested_prefix": prefix[:9] + "3"}


async def test_suggest_prefix_gives_up_after_its_range(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def taken(_session: Any, _prefix: str) -> object:
        return object()

    monkeypatch.setattr(cs.repo, "get_by_prefix", taken)
    assert await cs.suggest_prefix(session, "ABC") is None


async def test_install_refuses_a_design_of_another_page_size(session: AsyncSession) -> None:
    from app.sheets import backing_design_service as bds

    doc = loader.load_entry(K6121)
    assert doc.type.backing_design is not None
    await bds.create(
        session, key=doc.type.backing_design.key, name="Letter one", page_size="Letter", strips=[]
    )
    with pytest.raises(BackingDesignPageSizeMismatchError):
        await cs.install(session, K6121, as_new_key=None, prefix=None, actor="a")
    with pytest.raises(SheetTypeNotFoundError):
        await ts.get_type(session, K6121)  # rolled back


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def test_update_refusals(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    await _custom(session, "acme-a", "ACA")
    with pytest.raises(NoCatalogUpdateError):  # no provenance
        await cs.apply_update(session, "acme-a", actor="a")
    await cs.install(session, K5160, as_new_key=None, prefix=None, actor="a")
    with pytest.raises(NoCatalogUpdateError):  # no newer revision
        await cs.apply_update(session, K5160, actor="a")

    await ts.patch_type(session, K5160, {"notes": "mine"}, actor="a")
    _offer(monkeypatch, K5160, 2, description="Newer")
    with pytest.raises(TypeLocallyEditedError):
        await cs.apply_update(session, K5160, actor="a")

    index = tuple(e for e in loader.load_index() if e.key != K5160)
    monkeypatch.setattr(loader, "load_index", lambda: index)
    with pytest.raises(NoCatalogUpdateError):  # the entry left the image
        await cs.apply_update(session, K5160, actor="a")


async def test_update_applies_fields_and_geometry(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await cs.install(session, K6121, as_new_key=None, prefix=None, actor="a")
    margin = loader.load_entry(K6121).version.geometry.margin_top
    _offer(
        monkeypatch, K6121, 2, description="Newer", backing_design=None,
        geometry={"margin_top": round(margin + 0.1, 2)},
    )  # fmt: skip
    t = await cs.apply_update(session, K6121, actor="a")
    assert (t.description, t.catalog_revision, t.backing_design_id) == ("Newer", 2, None)
    assert (await ts.current_version(session, t.id)).version == 2
    assert await _unedited(session, K6121)


# ---------------------------------------------------------------------------
# Delete and usage (type service)
# ---------------------------------------------------------------------------


async def test_types_in_settings_skips_unreadable_values(session: AsyncSession) -> None:
    session.add(AppSetting(key=GROCY_LABEL_DEFAULT_SHEET_TYPE_KEY, value_json="{not json"))
    session.add(AppSetting(key=grocy_label_sheet_type_key("chore"), value_json=json.dumps(3)))
    session.add(AppSetting(key=grocy_label_sheet_type_key("battery"), value_json=None))
    session.add(AppSetting(key=grocy_label_sheet_type_key("recipe"), value_json='" acme-a "'))
    session.add(AppSetting(key=grocy_label_sheet_type_key("product"), value_json='"acme-a"'))
    await session.commit()
    found = await ts.types_in_settings(session, {"acme-a", "acme-b"})
    assert found == {"acme-a": grocy_label_sheet_type_key("product")}  # registry order


async def test_delete_unused_branches(session: AsyncSession) -> None:
    await _custom(session, "acme-a", "ACA")
    await _custom(session, "acme-b", "ACB")
    await _custom(session, "acme-c", "ACC")
    await _sheet(session, "acme-c", "ACC-0001")
    with pytest.raises(SheetTypeNotFoundError) as missing:
        await ts.delete_unused(session, ["acme-a", "acme-x"], actor="a")
    assert missing.value.detail == {"keys": ["acme-x"]}
    with pytest.raises(TypeNotUnusedError) as used:
        await ts.delete_unused(session, ["acme-c", "acme-a"], actor="a")
    assert used.value.detail == {"keys": ["acme-c"]}
    session.add(AppSetting(key=GROCY_LABEL_DEFAULT_SHEET_TYPE_KEY, value_json='"acme-b"'))
    await session.commit()
    with pytest.raises(SheetTypeInUseError) as named:
        await ts.delete_unused(session, ["acme-b", "acme-a"], actor="a")
    assert named.value.detail == {
        "reason": "type_in_settings",
        "setting": GROCY_LABEL_DEFAULT_SHEET_TYPE_KEY,
        "key": "acme-b",
    }
    with pytest.raises(SheetTypeInUseError):
        await ts.delete_type(session, "acme-b", actor="a")
    assert await ts.delete_unused(session, ["acme-a", "acme-a"], actor="a") == ["acme-a"]
    with pytest.raises(SheetTypeNotFoundError):
        await ts.get_type(session, "acme-a")
    assert await session.get(SheetIdCounter, "ACA") is None  # never had a sheet: no counter


async def test_usage_queries_on_empty_inputs(session: AsyncSession) -> None:
    assert await repo.usage_by_type(session, []) == {}
    assert await repo.sheet_counts(session, []) == {}
    assert await repo.get_many_by_key(session, []) == {}
    assert repo.NO_USAGE.state == "unused"


async def test_list_page_usage_filters_and_sorts(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    for key, prefix in (("acme-a", "ACA"), ("acme-b", "ACB"), ("acme-c", "ACC")):
        await _custom(session, key, prefix, vendor=key.upper())
    a = await _sheet(session, "acme-a", "ACA-0001", last_used_at=now - timedelta(days=30))
    await _sheet(session, "acme-b", "ACB-0001", last_used_at=now)
    await _sheet(session, "acme-b", "ACB-0002", last_used_at=now - timedelta(days=1))
    for n in range(1, 49):  # acme-a's only sheet is used up
        session.add(SheetCell(sheet_id=a.id, cell_no=n, state="used", source="manual"))
    await session.commit()

    def keys(page: ts.TypePage) -> list[str]:
        return [t.key for t in page.types]

    common: dict[str, Any] = {
        "page": None, "vendor": None, "q": None, "w": None, "h": None, "include_archived": False,
    }  # fmt: skip
    by_usage = await ts.list_types_page(session, **common, sort="usage")
    assert keys(by_usage) == ["acme-b", "acme-a", "acme-c"]
    by_last = await ts.list_types_page(session, **common, sort="last_used")
    assert keys(by_last) == ["acme-b", "acme-a", "acme-c"]
    assert keys(await ts.list_types_page(session, **common, usage_state="used_before")) == [
        "acme-a"
    ]
    since = await ts.list_types_page(
        session, **common, unused_since=(now - timedelta(days=7)).date()
    )
    assert keys(since) == ["acme-a", "acme-c"]
    paged = await ts.list_types_page(session, **common, sort="usage", limit=1, offset=1)
    assert keys(paged) == ["acme-a"] and paged.has_more
    assert paged.usage[a.type_id].sheets_total == 1

    facets = await ts.type_facets(
        session, page=None, q=None, w=None, h=None, include_archived=False, usage_state="in_use"
    )
    assert facets.vendors == [("ACME-B", 1)]
    assert await ts.list_types(session, **common) is not None


async def test_unused_since_treats_naive_timestamps_as_utc(session: AsyncSession) -> None:
    await _custom(session, "acme-a", "ACA")
    sheet = await _sheet(session, "acme-a", "ACA-0001")
    await session.execute(
        update(Sheet)
        .where(col(Sheet.id) == sheet.id)
        .values(last_used_at=datetime(2026, 1, 2, 12, 0, tzinfo=UTC))
    )
    await session.commit()
    common: dict[str, Any] = {
        "page": None, "vendor": None, "q": None, "w": None, "h": None, "include_archived": False,
    }  # fmt: skip
    before = await ts.list_types_page(session, **common, unused_since=date(2026, 1, 3))
    after = await ts.list_types_page(session, **common, unused_since=date(2026, 1, 2))
    assert [t.key for t in before.types] == ["acme-a"]
    assert [t.key for t in after.types] == []


# ---------------------------------------------------------------------------
# The backing design of an entry (operator ruling 2026-11-23, fix round 1)
# ---------------------------------------------------------------------------

K3490 = "avery-zweckform-3490"  # links the same UltraGrip design as 6121
ULTRAGRIP = "avery-zweckform-ultragrip"


def _moved_design() -> Any:
    """The entry's UltraGrip design with its first strip moved by 0.5 mm."""
    design = loader.load_entry(K6121).type.backing_design
    assert design is not None
    first = design.strips[0].model_copy(update={"x_mm": design.strips[0].x_mm + 0.5})
    return design.model_copy(update={"strips": [first, *design.strips[1:]]})


async def _design(session: AsyncSession) -> BackingDesign:
    from app.sheets import backing_design_service as bds

    session.expire_all()
    return await bds.get(session, ULTRAGRIP)


async def test_update_pulls_the_changed_strips_of_an_unedited_shared_design(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await cs.install(session, K6121, as_new_key=None, prefix=None, actor="a")
    await cs.install(session, K3490, as_new_key=None, prefix=None, actor="a")
    moved = _moved_design()
    _offer(monkeypatch, K6121, 2, backing_design=moved)
    t = await cs.apply_update(session, K6121, actor="a")
    assert t.catalog_revision == 2
    from app.sheets import backing_design_service as bds

    assert bds.to_doc(await _design(session)) == moved
    assert await _unedited(session, K6121)


async def test_update_refused_when_the_design_is_linked_by_a_custom_type(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await cs.install(session, K6121, as_new_key=None, prefix=None, actor="a")
    await _custom(session, "acme-mine", "ACM")
    await ts.patch_type(session, "acme-mine", {"backing_design": ULTRAGRIP}, actor="a")
    before = list((await _design(session)).strips)
    _offer(monkeypatch, K6121, 2, backing_design=_moved_design())
    with pytest.raises(BackingDesignDiffersError) as exc:
        await cs.apply_update(session, K6121, actor="a")
    assert exc.value.detail == {"design": ULTRAGRIP}
    assert (await _design(session)).strips == before
    assert (await ts.get_type(session, K6121)).catalog_revision == 1


async def test_update_refused_when_the_design_is_linked_by_an_edited_catalog_type(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await cs.install(session, K6121, as_new_key=None, prefix=None, actor="a")
    await cs.install(session, K3490, as_new_key=None, prefix=None, actor="a")
    await ts.patch_type(session, K3490, {"description": "mine"}, actor="a")
    _offer(monkeypatch, K6121, 2, backing_design=_moved_design())
    with pytest.raises(BackingDesignDiffersError):
        await cs.apply_update(session, K6121, actor="a")
    assert (await ts.get_type(session, K6121)).catalog_revision == 1


async def test_update_refused_when_the_design_was_edited_here(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edited design makes the type linking it edited: refused before the design is read."""
    from app.sheets import backing_design_service as bds

    await cs.install(session, K6121, as_new_key=None, prefix=None, actor="a")
    mine = bds.strips_from_mm(_moved_design().strips)
    await bds.patch(session, ULTRAGRIP, {"strips": mine})
    _offer(monkeypatch, K6121, 2, description="Newer")
    with pytest.raises(TypeLocallyEditedError):
        await cs.apply_update(session, K6121, actor="a")
    assert (await _design(session)).strips == mine


async def _shipped_design_alone(session: AsyncSession) -> None:
    """The UltraGrip design as shipped, linked by no type (an earlier install was deleted)."""
    from app.sheets import backing_design_service as bds

    shipped = loader.load_entry(K6121).type.backing_design
    assert shipped is not None
    await bds.create(
        session,
        key=shipped.key,
        name=shipped.name,
        page_size=shipped.page_size,
        strips=bds.strips_from_mm(shipped.strips),
    )


async def test_install_brings_a_differing_unedited_design_to_the_entry(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.sheets import backing_design_service as bds

    await _shipped_design_alone(session)
    moved = _moved_design()
    _offer(monkeypatch, K6121, 1, backing_design=moved)
    t = await cs.install(session, K6121, as_new_key=None, prefix=None, actor="a")
    linked = t.backing_design_id
    design = await _design(session)
    assert linked == design.id
    assert bds.to_doc(design) == moved
    assert await _unedited(session, K6121)


async def test_install_refuses_a_differing_edited_design(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.sheets import backing_design_service as bds

    await _shipped_design_alone(session)
    mine = bds.strips_from_mm(_moved_design().strips)
    await bds.patch(session, ULTRAGRIP, {"strips": mine})
    with pytest.raises(BackingDesignDiffersError):
        await cs.install(session, K6121, as_new_key=None, prefix=None, actor="a")
    with pytest.raises(SheetTypeNotFoundError):
        await ts.get_type(session, K6121)
    assert (await _design(session)).strips == mine


async def test_install_as_new_key_links_a_differing_design_as_stored(
    session: AsyncSession,
) -> None:
    from app.sheets import backing_design_service as bds

    await _shipped_design_alone(session)
    mine = bds.strips_from_mm(_moved_design().strips)
    await bds.patch(session, ULTRAGRIP, {"strips": mine})
    t = await cs.install(session, K6121, as_new_key="my-6121", prefix="MY6121", actor="a")
    linked = t.backing_design_id
    assert linked == (await _design(session)).id
    assert (await _design(session)).strips == mine  # a copy follows the import's rule


async def test_a_catalog_write_that_would_leave_the_type_edited_is_rolled_back(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive recheck before the commit (fix round 1)."""
    monkeypatch.setattr(cs, "is_locally_edited", lambda *_a: True)
    with pytest.raises(TypeLocallyEditedError):
        await cs.install(session, K5160, as_new_key=None, prefix=None, actor="a")
    with pytest.raises(SheetTypeNotFoundError):
        await ts.get_type(session, K5160)
