# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Migration C2 (20261121c2, #351): the ``sheet_type_log`` table.

The upgrade adds the table and touches no existing row. The downgrade drops it and leaves every
other table as it was; tested on a populated database (a 1.7.1 installation migrated to C2 with
log entries of every kind, with and without a printer), down to P1 and to 1.7.1, and up again.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from tests.helpers.db_1_7_1 import HEAD_1_7_1, build_db_1_7_1, migrate

_REV = "20261121c2"
_PREV = "20261121p1"
_NOW = "2026-11-21 10:00:00"
_KINDS = ("check_print", "verified", "new_version", "catalog_update", "note")


def _rows(db: Path, sql: str, *args: object) -> list[tuple[object, ...]]:
    with sqlite3.connect(db) as con:
        return [tuple(r) for r in con.execute(sql, args).fetchall()]


def _tables(db: Path) -> set[str]:
    return {str(r[0]) for r in _rows(db, "SELECT name FROM sqlite_master WHERE type = 'table'")}


def _snapshot(db: Path) -> dict[str, list[tuple[object, ...]]]:
    """Every row of every table but the log and Alembic's own."""
    skip = {"sheet_type_log", "alembic_version"}
    return {
        t: _rows(db, f'SELECT * FROM "{t}" ORDER BY rowid')
        for t in sorted(_tables(db) - skip)
        if not t.startswith("sqlite_")
    }


async def _populated(tmp_path: Path) -> tuple[Path, str]:
    db = await build_db_1_7_1(tmp_path / "hub.db")
    await migrate(db, _REV)
    pid = uuid.uuid4().hex
    with sqlite3.connect(db) as con:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute(
            "INSERT INTO printers (id, name, slug, model, backend, connection, enabled, created_at,"
            " updated_at) VALUES (?, 'Office', 'office-c2', 'm', 'ipp', ?, 1, ?, ?)",
            (pid, json.dumps({"uri": "ipp://printer.example.invalid/ipp"}), _NOW, _NOW),
        )
        type_id, version_id = con.execute(
            "SELECT t.id, v.id FROM sheet_types t JOIN sheet_type_versions v ON v.type_id = t.id"
            " ORDER BY t.key LIMIT 1"
        ).fetchone()
        for n, kind in enumerate(_KINDS):
            con.execute(
                "INSERT INTO sheet_type_log (id, type_id, type_version_id, kind, printer_id, tray,"
                " history_id, run_id, readings, verdict, note, actor, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admin', ?)",
                (
                    uuid.uuid4().hex,
                    type_id,
                    version_id,
                    kind,
                    pid if n % 2 == 0 else None,
                    "auto" if n % 2 == 0 else None,
                    uuid.uuid4().hex if kind == "check_print" else None,
                    uuid.uuid4().hex if kind == "check_print" else None,
                    json.dumps([{"col_gap": 1, "row_gap": 1, "across_mm": 0.2, "down_mm": -0.3}])
                    if kind == "check_print"
                    else None,
                    "fits" if kind == "check_print" else "none",
                    f"note {n}",
                    _NOW,
                ),
            )
        con.commit()
    return db, pid


async def test_c2_upgrade_adds_the_table_and_touches_nothing_else(tmp_path: Path) -> None:
    db = await build_db_1_7_1(tmp_path / "hub.db")
    await migrate(db, _PREV)
    before = _snapshot(db)
    assert "sheet_type_log" not in _tables(db)
    await migrate(db, _REV)
    assert "sheet_type_log" in _tables(db)
    assert _snapshot(db) == before
    assert _rows(db, "SELECT count(*) FROM sheet_type_log") == [(0,)]


async def test_c2_downgrade_on_a_populated_db(tmp_path: Path) -> None:
    db, _pid = await _populated(tmp_path)
    assert _rows(db, "SELECT count(*) FROM sheet_type_log") == [(len(_KINDS),)]
    before = _snapshot(db)

    await migrate(db, f"-{_PREV}")

    assert "sheet_type_log" not in _tables(db)
    assert _snapshot(db) == before  # every other row stays, byte for byte

    await migrate(db, f"-{HEAD_1_7_1}")  # on to 1.7.1
    assert "sheet_type_log" not in _tables(db)

    await migrate(db, _REV)  # and up again: the log starts empty
    assert _rows(db, "SELECT count(*) FROM sheet_type_log") == [(0,)]


async def test_c2_foreign_keys_cascade_with_the_type_and_null_the_printer(tmp_path: Path) -> None:
    db, pid = await _populated(tmp_path)
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA foreign_keys = ON")  # as the hub runs (app/db/engine.py)
        con.execute("DELETE FROM printer_trays WHERE printer_id = ?", (pid,))
        con.execute("DELETE FROM printers WHERE id = ?", (pid,))
        con.commit()
        assert con.execute("SELECT count(*) FROM sheet_type_log").fetchone() == (len(_KINDS),)
        assert con.execute(
            "SELECT count(*) FROM sheet_type_log WHERE printer_id IS NOT NULL"
        ).fetchone() == (0,)
        (type_id,) = con.execute("SELECT DISTINCT type_id FROM sheet_type_log").fetchone()
        con.execute("DELETE FROM sheet_type_versions WHERE type_id = ?", (type_id,))
        con.execute("DELETE FROM sheets WHERE type_id = ?", (type_id,))
        con.execute("DELETE FROM sheet_types WHERE id = ?", (type_id,))
        con.commit()
        assert con.execute("SELECT count(*) FROM sheet_type_log").fetchone() == (0,)
    finally:
        con.close()
