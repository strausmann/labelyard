# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Migration C1 (20261121c1, #336, #240; ADR 0029): calibration runs, counters, history and check
sheets.

Tested on a populated database: a 1.7.1 installation migrated to the revision before C1, two
printers with stored calibrations, then C1 (one ``imported`` history entry per calibration), rows
in every new table, a downgrade (the tables go, ``printer_calibrations`` stays as it was) and an
upgrade again. The foreign keys are checked on the migrated schema itself, with the hub's
``foreign_keys`` pragma on.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from tests.helpers.db_1_7_1 import build_db_1_7_1, migrate

_REV = "20261121c1"
_PREV = "20261121a1"
_TABLES = {
    "calibration_runs",
    "calibration_run_counters",
    "calibration_history",
    "calibration_check_sheets",
    "calibration_check_sheet_passes",
}
_NOW = datetime(2026, 11, 20, 9, 30, tzinfo=UTC).isoformat()


def _tables(db: Path) -> set[str]:
    with sqlite3.connect(db) as con:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _rows(db: Path, sql: str, *args: object) -> list[tuple[object, ...]]:
    with sqlite3.connect(db) as con:
        return [tuple(r) for r in con.execute(sql, args).fetchall()]


def _printer(con: sqlite3.Connection, name: str) -> str:
    pid = uuid.uuid4().hex
    con.execute(
        "INSERT INTO printers (id, name, slug, model, backend, connection, enabled, created_at,"
        " updated_at) VALUES (?, ?, ?, 'm', 'ipp', ?, 1, ?, ?)",
        (pid, name, name.lower(), json.dumps({"uri": "ipp://printer.example.invalid/ipp"}),
         _NOW, _NOW),
    )  # fmt: skip
    return pid


def _calibration(con: sqlite3.Connection, pid: str, tray: str, dx: int, by: str | None) -> None:
    con.execute(
        "INSERT INTO printer_calibrations (printer_id, tray_key, method, offset_x_hmm,"
        " offset_y_hmm, scale_x_ppm, scale_y_ppm, rotation_udeg, measured_on_type_version_id,"
        " updated_at, updated_by) VALUES (?, ?, 'manual', ?, -7, 1000400, 999800, 150, ?, ?, ?)",
        (pid, tray, dx, uuid.uuid4().hex, _NOW, by),
    )


async def _populated(tmp_path: Path) -> tuple[Path, str, str]:
    db = await build_db_1_7_1(tmp_path / "hub.db")
    await migrate(db, _PREV)
    with sqlite3.connect(db) as con:
        a, b = _printer(con, "Office"), _printer(con, "Hall")
        _calibration(con, a, "tray-1", 12, "admin")
        _calibration(con, a, "tray-2", -30, None)
        _calibration(con, b, "auto", 5, "admin")
    return db, a, b


def _fill_new_tables(con: sqlite3.Connection, pid: str) -> None:
    run_id, sheet_id = uuid.uuid4().hex, uuid.uuid4().hex
    con.execute(
        "INSERT INTO calibration_runs (id, printer_id, tray, run_no, kind, job_id,"
        " type_version_id, requested_tray, media_source_reported, created_at)"
        " VALUES (?, ?, 'tray-1', 1, 'check_page', ?, ?, 'tray-1', 0, ?)",
        (run_id, pid, uuid.uuid4().hex, uuid.uuid4().hex, _NOW),
    )
    con.execute(
        "INSERT INTO calibration_run_counters (printer_id, tray, next_run) VALUES (?, 'tray-1', 2)",
        (pid,),
    )
    con.execute(
        "INSERT INTO calibration_check_sheets (id, printer_id, tray, type_version_id,"
        " passes_total, created_at) VALUES (?, ?, 'tray-1', ?, 3, ?)",
        (sheet_id, pid, uuid.uuid4().hex, _NOW),
    )
    con.execute(
        "INSERT INTO calibration_check_sheet_passes (check_sheet_id, pass_no, run_id)"
        " VALUES (?, 1, ?)",
        (sheet_id, run_id),
    )


async def test_c1_imports_one_history_entry_per_existing_calibration(tmp_path: Path) -> None:
    db, a, b = await _populated(tmp_path)
    assert not _tables(db) & _TABLES

    await migrate(db, _REV)

    assert _tables(db) >= _TABLES
    entries = _rows(
        db,
        "SELECT printer_id, tray, seq, action, method, offset_x_hmm, offset_y_hmm, scale_x_ppm,"
        " scale_y_ppm, rotation_udeg, previous, run_id, readings, actor, created_at"
        " FROM calibration_history ORDER BY offset_x_hmm",
    )
    assert len(entries) == 3 == len(_rows(db, "SELECT 1 FROM printer_calibrations"))
    assert {(e[0], e[1]) for e in entries} == {(a, "tray-1"), (a, "tray-2"), (b, "auto")}
    first = entries[0]  # dx -30, a's tray-2, no updated_by
    assert first[2:10] == (1, "imported", "manual", -30, -7, 1000400, 999800, 150)
    assert first[10:14] == (None, None, None, None)  # no previous values, run or readings
    assert str(first[14]).startswith("2026-11-20 09:30")
    assert [e[13] for e in entries] == [None, "admin", "admin"]
    # the measured-on version is carried over row by row
    assert _rows(
        db,
        "SELECT count(*) FROM calibration_history h JOIN printer_calibrations c"
        " ON c.printer_id = h.printer_id AND c.tray_key = h.tray"
        " AND c.measured_on_type_version_id = h.measured_on_type_version_id",
    ) == [(3,)]
    # pages printed before have no run: every counter starts at 1
    assert _rows(db, "SELECT count(*) FROM calibration_runs") == [(0,)]
    assert _rows(db, "SELECT count(*) FROM calibration_run_counters") == [(0,)]


async def test_c1_downgrade_on_a_populated_db(tmp_path: Path) -> None:
    db, a, _b = await _populated(tmp_path)
    before = _rows(db, "SELECT * FROM printer_calibrations ORDER BY printer_id, tray_key")
    await migrate(db, _REV)
    with sqlite3.connect(db) as con:
        _fill_new_tables(con, a)
    assert _rows(db, "SELECT count(*) FROM calibration_check_sheet_passes") == [(1,)]

    await migrate(db, f"-{_PREV}")

    assert not _tables(db) & _TABLES
    assert _rows(db, "SELECT * FROM printer_calibrations ORDER BY printer_id, tray_key") == before

    await migrate(db, _REV)  # and up again: the import runs once more, nothing else returns

    assert len(_rows(db, "SELECT 1 FROM calibration_history")) == 3
    assert _rows(db, "SELECT count(*) FROM calibration_runs") == [(0,)]


async def test_printer_delete_removes_runs_history_counters_and_check_sheets(
    tmp_path: Path,
) -> None:
    db, a, b = await _populated(tmp_path)
    await migrate(db, _REV)
    with sqlite3.connect(db) as con:
        _fill_new_tables(con, a)
        _fill_new_tables(con, b)
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA foreign_keys = ON")  # as the hub runs (app/db/engine.py)
        # The printer's rows without a cascade go first, as an admin removal would do.
        con.execute("DELETE FROM printer_calibrations WHERE printer_id = ?", (a,))
        con.execute("DELETE FROM printer_trays WHERE printer_id = ?", (a,))
        con.execute("DELETE FROM printers WHERE id = ?", (a,))
        con.commit()
        for table in sorted(_TABLES - {"calibration_check_sheet_passes"}):
            owners = {r[0] for r in con.execute(f"SELECT printer_id FROM {table}")}
            assert a not in owners and b in owners, table
        passes = con.execute(
            "SELECT p.check_sheet_id FROM calibration_check_sheet_passes p"
            " JOIN calibration_check_sheets s ON s.id = p.check_sheet_id"
        ).fetchall()
        assert len(passes) == 1  # b's pass stays, a's went with its check sheet and run
        assert con.execute("SELECT count(*) FROM calibration_check_sheet_passes").fetchone() == (1,)
    finally:
        con.close()
