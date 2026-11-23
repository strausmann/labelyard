# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Migration A1 (20261121a1, #246): preprint presets and the position of the margin sheet code.

Tested on a populated database: a 1.7.1 installation migrated to A1, a preset linked by a type
that sets zones and a position, then downgraded to A2 (the table and the columns go, every type
and sheet stays) and upgraded again (the defaults return).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from tests.helpers.db_1_7_1 import build_db_1_7_1, migrate

_REV = "20261121a1"
_PREV = "20261121a2"
_COLUMNS = {
    "id_margin_band",
    "id_margin_align",
    "id_margin_offset_hmm",
    "preprint_zones",
    "preprint_preset_id",
}


def _columns(db: Path) -> set[str]:
    with sqlite3.connect(db) as con:
        return {r[1] for r in con.execute("PRAGMA table_info(sheet_types)")}


def _tables(db: Path) -> set[str]:
    with sqlite3.connect(db) as con:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _one(db: Path, sql: str, *args: object) -> tuple[object, ...]:
    with sqlite3.connect(db) as con:
        row = con.execute(sql, args).fetchone()
    assert row is not None
    return tuple(row)


async def test_a1_downgrade_on_a_populated_db(tmp_path: Path) -> None:
    db = await build_db_1_7_1(tmp_path / "hub.db")
    await migrate(db, _PREV)
    assert not _columns(db) & _COLUMNS and "preprint_presets" not in _tables(db)

    await migrate(db, _REV)

    assert _columns(db) >= _COLUMNS
    # every existing type gets the defaults that place the code as 1.7.x did
    assert _one(
        db,
        "SELECT count(*) FROM sheet_types WHERE id_margin_band = 'auto' AND id_margin_align = "
        "'start' AND id_margin_offset_hmm = 0 AND preprint_zones = '[]' "
        "AND preprint_preset_id IS NULL",
    ) == _one(db, "SELECT count(*) FROM sheet_types")
    preset_id = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO preprint_presets (id, key, name, page_size, zones, origin, created_at,"
            " updated_at) VALUES (?, 'avery-front', 'Avery', 'A4', ?, 'custom', ?, ?)",
            (preset_id, json.dumps([{"x": 15000, "y": 500, "w": 4000, "h": 1000}]), now, now),
        )
        key = con.execute("SELECT key FROM sheet_types ORDER BY key LIMIT 1").fetchone()[0]
        con.execute(
            "UPDATE sheet_types SET id_margin_band = 'top', id_margin_align = 'end',"
            " id_margin_offset_hmm = 2550, preprint_zones = ?, preprint_preset_id = ?"
            " WHERE key = ?",
            (
                json.dumps([{"x": 985, "y": 600, "w": 4000, "h": 1000, "note": "Logo"}]),
                preset_id,
                key,
            ),
        )
    types, sheets = (
        _one(db, "SELECT count(*) FROM sheet_types"),
        _one(db, "SELECT count(*) FROM sheets"),
    )
    assert sheets[0] == 1  # the 1.7.1 database carries a sheet

    await migrate(db, f"-{_PREV}")

    assert not _columns(db) & _COLUMNS and "preprint_presets" not in _tables(db)
    assert _one(db, "SELECT count(*) FROM sheet_types") == types
    assert _one(db, "SELECT count(*) FROM sheets") == sheets

    await migrate(db, _REV)

    assert _one(
        db, "SELECT id_margin_band, id_margin_offset_hmm FROM sheet_types WHERE key = ?", key
    ) == (
        "auto",
        0,
    )
    assert _one(db, "SELECT count(*) FROM preprint_presets") == (0,)
