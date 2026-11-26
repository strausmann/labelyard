# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Migration P1 (20261121p1, #241, rollback ruling P1): the ``probe_page`` job kind.

The upgrade changes nothing. The downgrade removes the probe jobs, whose kind 1.7.x refuses, and
the probe runs; every other job and run stays as it was. Tested on a populated database: a 1.7.1
installation migrated to P1 with jobs of every kind (one with a payload that is not JSON) and
runs of every kind, a downgrade to C1 and then to 1.7.1, where every remaining page job still
validates with the frozen 1.7.1 models, and an upgrade again.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from tests.fixtures import models_1_7_1 as v171
from tests.helpers.db_1_7_1 import HEAD_1_7_1, build_db_1_7_1, migrate

_REV = "20261121p1"
_PREV = "20261121c1"
_NOW = "2026-11-21 10:00:00"


def _page(kind: str) -> dict[str, object]:
    return {
        "kind": kind,
        "media": "A4",
        "tray": "auto",
        "render_dpi": 600,
        "job_name": f"{kind} · acme v1",
        "calibration": None,
        "detail": {"type_key": "acme", "version": 1, "run_no": 1},
    }


_JOBS: dict[str, str] = {
    "probe": json.dumps(_page("probe_page")),
    "grid": json.dumps({**_page("probe_page"), "detail": {"grid": True}}),
    "calibration": json.dumps(_page("calibration_page")),
    "test": json.dumps(_page("test_page")),
    "tape": json.dumps({"content_type": "qr_two_lines", "data": {}}),
    "broken": "{not json",
}
_GONE = {"probe", "grid"}


def _rows(db: Path, sql: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(db) as con:
        return [tuple(r) for r in con.execute(sql).fetchall()]


async def _populated(tmp_path: Path) -> tuple[Path, dict[str, str], dict[str, str]]:
    db = await build_db_1_7_1(tmp_path / "hub.db")
    await migrate(db, _REV)
    pid = uuid.uuid4().hex
    jobs = {name: uuid.uuid4().hex for name in _JOBS}
    run_ids = {kind: uuid.uuid4().hex for kind in ("probe_page", "calibration_page", "check_page")}
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO printers (id, name, slug, model, backend, connection, enabled, created_at,"
            " updated_at) VALUES (?, 'Office', 'office-p1', 'm', 'ipp', ?, 1, ?, ?)",
            (pid, json.dumps({"uri": "ipp://printer.example.invalid/ipp"}), _NOW, _NOW),
        )
        for name, payload in _JOBS.items():
            con.execute(
                "INSERT INTO jobs (id, printer_id, state, payload, created_at, updated_at)"
                " VALUES (?, ?, 'done', ?, ?, ?)",
                (jobs[name], pid, payload, _NOW, _NOW),
            )
        for no, (kind, run_id) in enumerate(run_ids.items(), start=1):
            con.execute(
                "INSERT INTO calibration_runs (id, printer_id, tray, run_no, kind, job_id,"
                " type_version_id, requested_tray, media_source_reported, created_at)"
                " VALUES (?, ?, 'auto', ?, ?, ?, ?, 'auto', 0, ?)",
                (run_id, pid, no, kind, uuid.uuid4().hex, uuid.uuid4().hex, _NOW),
            )
        con.commit()
    return db, jobs, run_ids


async def test_p1_upgrade_changes_nothing(tmp_path: Path) -> None:
    db = await build_db_1_7_1(tmp_path / "hub.db")
    await migrate(db, _PREV)
    schema = _rows(db, "SELECT type, name, sql FROM sqlite_master ORDER BY name")
    jobs = _rows(db, "SELECT * FROM jobs ORDER BY id")
    await migrate(db, _REV)
    assert _rows(db, "SELECT type, name, sql FROM sqlite_master ORDER BY name") == schema
    assert _rows(db, "SELECT * FROM jobs ORDER BY id") == jobs


async def test_p1_downgrade_removes_probe_jobs_on_a_populated_db(tmp_path: Path) -> None:
    db, jobs, run_ids = await _populated(tmp_path)
    kept_before = _rows(
        db,
        "SELECT * FROM jobs WHERE id IN ("
        + ",".join(f"'{jobs[n]}'" for n in _JOBS if n not in _GONE)
        + ") ORDER BY id",
    )

    await migrate(db, f"-{_PREV}")

    left = {r[0] for r in _rows(db, "SELECT id FROM jobs")}
    assert left == {jobs[n] for n in _JOBS if n not in _GONE}
    # Every other job is untouched, byte for byte.
    assert _rows(db, "SELECT * FROM jobs ORDER BY id") == kept_before
    runs = {r[0]: r[1] for r in _rows(db, "SELECT id, kind FROM calibration_runs")}
    assert runs == {run_ids["calibration_page"]: "calibration_page",
                    run_ids["check_page"]: "check_page"}  # fmt: skip

    # On to 1.7.1: every page job left is one 1.7.1 can read.
    await migrate(db, f"-{HEAD_1_7_1}")
    for (payload,) in _rows(db, "SELECT payload FROM jobs"):
        try:
            data = json.loads(str(payload))
        except ValueError:
            continue  # the broken row: 1.7.1 skips it as it always did
        if "kind" in data:
            v171.PagePayload.model_validate(data)

    await migrate(db, _REV)  # and up again
    assert {r[0] for r in _rows(db, "SELECT id FROM jobs")} == left
