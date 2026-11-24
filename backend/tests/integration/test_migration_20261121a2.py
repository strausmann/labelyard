# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Migration A2 (20261121a2, #335): catalog provenance columns on ``sheet_types``.

The downgrade drops the three columns and deletes the marker of the startup step
(``internal.catalog.migrated``), so that a later upgrade stamps the types again. Tested on a
populated database: a 1.7.1 installation migrated to A2 and stamped by the startup step.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.catalog.upgrade import MIGRATED_KEY, run_catalog_migration

from tests.helpers.db_1_7_1 import EDITED_KEY, build_db_1_7_1, factory_for, migrate

_REV = "20261121a2"
_PREV = "20261121e1"
_COLUMNS = {"catalog_key", "catalog_revision", "catalog_fields_hash"}


def _columns(db: Path) -> set[str]:
    with sqlite3.connect(db) as con:
        return {r[1] for r in con.execute("PRAGMA table_info(sheet_types)")}


def _count(db: Path, sql: str, *args: object) -> int:
    with sqlite3.connect(db) as con:
        return int(con.execute(sql, args).fetchone()[0])


async def _stamp(db: Path) -> None:
    # The startup step runs on the live models, which carry the columns of later revisions (#246):
    # the database is brought to the head first, as a real start does.
    await migrate(db, "head")
    factory, dispose = factory_for(db)
    try:
        report = await run_catalog_migration(factory)
        assert report.ran and not report.failed
        # the startup step ran on the populated rows: the one locally edited type is found
        assert report.edited == [EDITED_KEY]
    finally:
        await dispose()


async def test_a2_downgrade_on_a_populated_db_clears_the_marker(tmp_path: Path) -> None:
    db = await build_db_1_7_1(tmp_path / "hub.db")
    assert not _columns(db) & _COLUMNS
    await migrate(db, _REV)
    assert _columns(db) >= _COLUMNS
    await _stamp(db)
    marker = "SELECT count(*) FROM app_settings WHERE key = ?"
    stamped = "SELECT count(*) FROM sheet_types WHERE catalog_key IS NOT NULL"
    assert _count(db, marker, MIGRATED_KEY) == 1
    assert _count(db, stamped) == 39
    types, sheets = (
        _count(db, "SELECT count(*) FROM sheet_types"),
        _count(db, "SELECT count(*) FROM sheets"),
    )

    await migrate(db, f"-{_PREV}")
    assert not _columns(db) & _COLUMNS
    assert _count(db, marker, MIGRATED_KEY) == 0
    assert _count(db, "SELECT count(*) FROM sheet_types") == types == 40
    assert _count(db, "SELECT count(*) FROM sheets") == sheets == 1
    # the seeder's own state survives: only the catalog marker goes
    assert _count(db, marker, "internal.seed.adopted") == 1

    await migrate(db, _REV)
    assert _count(db, stamped) == 0
    await _stamp(db)
    assert _count(db, stamped) == 39
