# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The upgrade of an existing installation to the catalog (A3, ADR 0028, spec 6.1.4, #335).

Each test builds the database of a 1.7.1 installation (``tests/helpers/db_1_7_1.py``), migrates it
to the 1.8.0 schema with Alembic and runs the startup step ``run_catalog_migration`` on it, the
way the first start of 1.8.0 does.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from app.catalog import loader, upgrade
from app.catalog.upgrade import (
    MIGRATED_KEY,
    catalog_update_state,
    is_locally_edited,
    provenance_inputs,
    run_catalog_migration,
)
from app.models.app_setting import AppSetting
from app.models.sheet import Sheet, SheetType, SheetTypeVersion
from app.seed.sheet_seed import CORRECTED_KEY, NOTICES_KEY, load_seed
from app.sheets import type_service as ts
from app.sheets.exchange import geometry_from_doc
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import col

from tests.helpers.db_1_7_1 import (
    CUSTOM_KEY,
    EDITED_KEY,
    SHEET_TYPE_KEY,
    build_db_1_7_1,
    factory_for,
    migrate,
)

Factory = async_sessionmaker[AsyncSession]
SEED_V1_5_2 = Path(__file__).parents[1] / "fixtures" / "seed" / "sheet_types_v1_5_2.json"
K6121 = "avery-zweckform-6121"
K4731 = "avery-zweckform-l4731rev"


@pytest.fixture(autouse=True)
def _fresh_catalog_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the cached sidecar around each test, after undoing the patches of the test."""
    loader.clear_caches()
    yield
    monkeypatch.undo()
    loader.clear_caches()


Build = Callable[..., Awaitable[Factory]]


@pytest.fixture
async def upgraded(tmp_path: Path) -> AsyncIterator[Build]:
    """``await upgraded(**build_kwargs)``: a 1.7.1 database migrated to the 1.8.0 head."""
    disposers: list[Callable[[], Awaitable[None]]] = []

    async def _build(**kwargs: Any) -> Factory:
        db = await build_db_1_7_1(tmp_path / f"hub{len(disposers)}.db", **kwargs)
        await migrate(db, "head")
        factory, dispose = factory_for(db)
        disposers.append(dispose)
        return factory

    yield _build
    for dispose in disposers:
        await dispose()


async def _types(factory: Factory) -> dict[str, SheetType]:
    async with factory() as s:
        return {t.key: t for t in (await s.execute(select(SheetType))).scalars()}


async def _edited(factory: Factory, key: str) -> bool:
    async with factory() as s:
        t = await ts.get_type(s, key)
        current, design = await provenance_inputs(s, t)
        return is_locally_edited(t, current, design)


async def _marker(factory: Factory) -> AppSetting | None:
    async with factory() as s:
        return await s.get(AppSetting, MIGRATED_KEY)


async def test_upgrade_keeps_all_39_types_and_the_custom_one(upgraded: Build) -> None:
    factory = await upgraded()
    report = await run_catalog_migration(factory)
    assert report.ran and not report.failed
    types = await _types(factory)
    assert len(types) == 40
    assert types[CUSTOM_KEY].origin == "custom"
    assert types[CUSTOM_KEY].catalog_key is None
    async with factory() as s:
        sheets = list((await s.execute(select(Sheet))).scalars())
    assert [types[SHEET_TYPE_KEY].id] == [sh.type_id for sh in sheets]
    assert sorted(report.stamped) == sorted(k for k, t in types.items() if t.origin == "seed")
    assert await _marker(factory) is not None


async def test_upgrade_marks_the_edited_description_as_edited(upgraded: Build) -> None:
    factory = await upgraded()
    await run_catalog_migration(factory)
    t = (await _types(factory))[EDITED_KEY]
    assert t.catalog_fields_hash == loader.load_meta(EDITED_KEY).baseline_fields_hash
    assert await _edited(factory, EDITED_KEY)
    assert not await _edited(factory, SHEET_TYPE_KEY)


async def test_upgrade_gives_unedited_seed_types_catalog_provenance(upgraded: Build) -> None:
    factory = await upgraded()
    await run_catalog_migration(factory)
    for key, t in (await _types(factory)).items():
        if t.origin != "seed":
            continue
        meta = loader.load_meta(key)
        assert (t.catalog_key, t.catalog_revision) == (key, 1)
        assert t.catalog_fields_hash == meta.baseline_fields_hash, key
        assert await _edited(factory, key) is (key == EDITED_KEY), key


async def test_upgraded_type_is_offered_a_newer_catalog_revision(
    upgraded: Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later catalog revision (10c) reaches an upgraded installation as an offered update."""
    factory = await upgraded()
    key = SHEET_TYPE_KEY
    newer = loader.load_meta(key).model_copy(update={"revision": 2})
    metas = {**loader._all_meta(), key: newer}
    doc = loader.load_entry(key)
    changed = doc.model_copy(
        update={"type": doc.type.model_copy(update={"description": "Revised address labels"})}
    )
    real_entry = loader.load_entry
    monkeypatch.setattr(loader, "_all_meta", lambda: metas)
    monkeypatch.setattr(loader, "load_entry", lambda k: changed if k == key else real_entry(k))
    await run_catalog_migration(factory)
    async with factory() as s:
        t = await ts.get_type(s, key)
        current, design = await provenance_inputs(s, t)
        state = catalog_update_state(t, current, design)
        assert t.catalog_revision == 1
        assert not is_locally_edited(t, current, design)
        assert state.update_available and not state.catalog_newer
        edited = await ts.get_type(s, EDITED_KEY)
        current, design = await provenance_inputs(s, edited)
        assert not catalog_update_state(edited, current, design).update_available


async def test_upgraded_6121_is_unedited(upgraded: Build) -> None:
    factory = await upgraded()
    await run_catalog_migration(factory)
    async with factory() as s:
        t = await ts.get_type(s, K6121)
        _current, design = await provenance_inputs(s, t)
    assert design is not None and design.key == "avery-zweckform-ultragrip"
    assert not await _edited(factory, K6121)


async def test_adopted_6121_counts_as_edited(upgraded: Build) -> None:
    """A user's own 6121 that 1.7.x adopted kept the user's fields and current version."""
    shipped = next(d for d in load_seed().docs if d.type.key == K6121)

    async def own_6121(factory: Factory) -> None:
        fields = ts.TypeFields(
            key=K6121,
            prefix="MY6121",
            vendor="Avery Zweckform",
            product_codes=["6121"],
            description="My own UltraGrip sheet",
            allow_qr=True,
        )
        async with factory() as s:
            await ts.create_type(
                s,
                fields,
                geometry_from_doc(shipped.version.geometry),
                actor="user:admin",
                origin="custom",
            )

    factory = await upgraded(prepare=own_6121)
    async with factory() as s:
        t = await ts.get_type(s, K6121)
        current, _design = await provenance_inputs(s, t)
        assert (t.origin, current.created_by) == ("seed", "user:admin")  # adopted by 1.7.x
    await run_catalog_migration(factory)
    t = (await _types(factory))[K6121]
    assert (t.catalog_key, t.catalog_revision) == (K6121, 1)
    assert await _edited(factory, K6121)


async def test_type_with_a_current_version_not_written_by_the_catalog_is_edited(
    upgraded: Build,
) -> None:
    """Same type fields as shipped, but the current version is a user's: edited (spec 6.1.3)."""
    factory = await upgraded()
    await run_catalog_migration(factory)
    async with factory() as s:
        t = await ts.get_type(s, SHEET_TYPE_KEY)
        await s.execute(
            update(SheetTypeVersion)
            .where(col(SheetTypeVersion.type_id) == t.id)
            .values(created_by="user:admin")
        )
        await s.commit()
    assert await _edited(factory, SHEET_TYPE_KEY)


async def test_l4731rev_from_a_1_5_install_is_unedited(upgraded: Build) -> None:
    """Seeded by v1.5.2 (``allow_qr`` off), then by 1.7.1: a prior shipped state, not an edit."""
    factory = await upgraded(seeds=(SEED_V1_5_2, None), populate=False)
    await run_catalog_migration(factory)
    t = (await _types(factory))[K4731]
    meta = loader.load_meta(K4731)
    assert t.catalog_fields_hash in meta.prior_fields_hashes
    assert not await _edited(factory, K4731)


async def test_row_documents_cannot_carry_counts_as_edited(upgraded: Build) -> None:
    """1.7.x stored a source URL with user info; #427 refuses it. Edited, and A3 does not fail."""
    factory = await upgraded()
    async with factory() as s:
        await s.execute(
            update(SheetType)
            .where(col(SheetType.key) == SHEET_TYPE_KEY)
            .values(source_url="https://user:secret@example.invalid/sheet")
        )
        await s.commit()
    report = await run_catalog_migration(factory)
    assert report.ran and not report.failed
    t = (await _types(factory))[SHEET_TYPE_KEY]
    assert t.catalog_fields_hash == loader.load_meta(SHEET_TYPE_KEY).baseline_fields_hash
    assert await _edited(factory, SHEET_TYPE_KEY)


async def _versions(factory: Factory, key: str) -> list[int]:
    async with factory() as s:
        t = await ts.get_type(s, key)
        rows = await s.execute(
            select(col(SheetTypeVersion.version)).where(col(SheetTypeVersion.type_id) == t.id)
        )
        return sorted(rows.scalars())


async def test_upgrade_runs_pending_corrections_once(upgraded: Build) -> None:
    """An installation seeded by v1.5.2 only never ran the L4731REV correction: A3 runs it."""
    factory = await upgraded(seeds=(SEED_V1_5_2,), populate=False)
    assert await _versions(factory, K4731) == [1]
    report = await run_catalog_migration(factory)
    assert report.corrected == [K4731]
    assert await _versions(factory, K4731) == [1, 2]
    async with factory() as s:
        corrected = await s.get(AppSetting, CORRECTED_KEY)
        assert corrected is not None and json.loads(corrected.value_json or "{}") == {K4731: 2}
        await s.execute(delete(AppSetting).where(col(AppSetting.key) == MIGRATED_KEY))
        await s.commit()
    again = await run_catalog_migration(factory)
    assert again.ran and again.corrected == []
    assert await _versions(factory, K4731) == [1, 2]
    assert not await _edited(factory, K4731)


async def test_catalog_step_does_not_restamp_later_provenance(upgraded: Build) -> None:
    factory = await upgraded()
    await run_catalog_migration(factory)
    later = "f" * 64
    async with factory() as s:
        await s.execute(
            update(SheetType)
            .where(col(SheetType.key) == SHEET_TYPE_KEY)
            .values(catalog_revision=2, catalog_fields_hash=later)
        )
        await s.commit()
    skipped = await run_catalog_migration(factory)  # the marker is set: nothing runs
    assert not skipped.ran and skipped.stamped == []
    async with factory() as s:
        await s.execute(delete(AppSetting).where(col(AppSetting.key) == MIGRATED_KEY))
        await s.commit()
    again = await run_catalog_migration(factory)
    assert again.ran and again.stamped == []
    t = (await _types(factory))[SHEET_TYPE_KEY]
    assert (t.catalog_revision, t.catalog_fields_hash) == (2, later)


async def _notices(factory: Factory) -> list[dict[str, Any]]:
    async with factory() as s:
        row = await s.get(AppSetting, NOTICES_KEY)
        loaded = json.loads(row.value_json or "[]") if row is not None else []
        return [n for n in loaded if n.get("kind") == "catalog_migration_failed"]


async def test_failed_catalog_step_leaves_a_notice_and_retries(upgraded: Build) -> None:
    factory = await upgraded()

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("disk on fire")

    with patch.object(upgrade, "_stamp", _boom):
        report = await run_catalog_migration(factory)
    assert report.ran and report.failed
    assert await _marker(factory) is None
    assert all(t.catalog_key is None for t in (await _types(factory)).values())
    notices = await _notices(factory)
    assert len(notices) == 1 and "disk on fire" not in json.dumps(notices)
    with patch.object(upgrade, "_stamp", _boom):  # a second failed start: still one notice
        await run_catalog_migration(factory)
    assert len(await _notices(factory)) == 1
    retried = await run_catalog_migration(factory)
    assert retried.ran and not retried.failed and retried.stamped
    assert await _marker(factory) is not None


async def test_failing_correction_rolls_the_whole_step_back(
    upgraded: Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One transaction: a correction that raises leaves no provenance and no marker."""
    factory = await upgraded(seeds=(SEED_V1_5_2,), populate=False)

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("app.seed.sheet_seed._correct_one", _boom)
    report = await run_catalog_migration(factory)
    assert report.failed
    assert await _marker(factory) is None
    async with factory() as s:
        stamped = await s.scalar(
            select(func.count())
            .select_from(SheetType)
            .where(col(SheetType.catalog_key).is_not(None))
        )
    assert stamped == 0
