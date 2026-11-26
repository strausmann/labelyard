# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The sheet-type log (#351, design 7.6, ruling 18-A).

Written by the check readings of a check print (their own route; the save refuses check runs),
"Mark as verified", every new version, every catalog update and the import of a new version;
read through ``GET /api/v1/sheet-types/{key}/log``. It names printers, so a restricted key reads
only its printers' entries, and it never leaves the hub.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.catalog import loader
from app.catalog.upgrade import CATALOG_ACTOR
from app.models.printer import Printer
from app.models.printer_tray import PrinterTray
from app.models.sheet import SheetTypeVersion
from app.models.sheet_type_log import SheetTypeLog
from app.sheets import type_log
from app.sheets import type_service as ts
from sqlalchemy import delete, func, select
from sqlmodel import col

from tests.integration.api.test_fit_with_run import (
    _check_readings,
    _key,
    _printer,
    _run,
    _save,
    _types,
)
from tests.integration.api.test_sheet_catalog import K5160, _install, _offer
from tests.integration.api.test_sheet_types_api import _create, _err, _factory, _insert_key

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_catalog_caches(monkeypatch: pytest.MonkeyPatch) -> Any:
    loader.clear_caches()
    yield
    monkeypatch.undo()
    loader.clear_caches()


async def _log(c: Any, key: str, headers: dict[str, str], **params: Any) -> list[dict[str, Any]]:
    r = await c.get(f"/api/v1/sheet-types/{key}/log", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return list(r.json())


async def _check(c: Any, pid: UUID, run_id: UUID, headers: dict[str, str], tray: str = "auto"):  # type: ignore[no-untyped-def]
    return await c.post(
        f"/api/v1/printers/{pid}/calibration/{tray}/check-readings",
        json={"run_id": str(run_id), "readings": _check_readings()},
        headers=headers,
    )


def _db_file() -> Path:
    import app.db.engine as engine_module

    url = str(engine_module.engine.url)
    return Path(url.split("///", 1)[1])


def _rows(sql: str, *args: Any) -> list[tuple[Any, ...]]:
    with sqlite3.connect(_db_file()) as con:
        return [tuple(r) for r in con.execute(sql, args).fetchall()]


# ---------------------------------------------------------------------------- check readings


async def test_check_readings_write_a_check_print_entry(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin, read = await _key(["admin"]), await _key(["read"])
    pid = await _printer()
    measured, checked = await _types()
    await _save(c, pid, measured, admin)
    run_id = await _run(pid, "auto", checked)

    r = await _check(c, pid, run_id, admin)
    assert r.status_code == 201, r.text
    entry = r.json()
    assert entry["kind"] == "check_print" and entry["verdict"] == "none"
    assert entry["printer"]["id"] == str(pid) and entry["tray"] == "auto"
    assert entry["tray_label"] == "Auto"
    assert entry["run"]["id"] == str(run_id) and entry["run"]["kind"] == "check_page"
    assert entry["correction"]["seq"] == 1  # the save above, the correction in effect
    assert entry["correction"]["offset_x_mm"] == 1.2
    assert entry["type_version"] == 1
    sent = _check_readings()
    assert [(x["col_gap"], x["row_gap"]) for x in entry["readings"]] == [
        (x["col_gap"], x["row_gap"]) for x in sent
    ]
    assert all(isinstance(x["residual_mm"], float) for x in entry["readings"])
    assert entry["readings"][0]["across_mm"] == pytest.approx(sent[0]["across_mm"])

    # It is in the log of the checked type, not of the type the correction was measured on.
    (listed,) = await _log(c, checked, read)
    assert listed == entry
    assert await _log(c, measured, read) == []


async def test_check_readings_leave_the_correction_unchanged(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    measured, checked = await _types()
    await _save(c, pid, measured, admin)
    run_id = await _run(pid, "auto", checked)
    calibration = "SELECT * FROM printer_calibrations WHERE printer_id = ?"
    history = "SELECT * FROM calibration_history WHERE printer_id = ? ORDER BY seq"
    before = (_rows(calibration, pid.hex), _rows(history, pid.hex))
    assert before[0] and before[1]

    assert (await _check(c, pid, run_id, admin)).status_code == 201

    # Byte for byte: the row and the history are exactly as they were.
    assert (_rows(calibration, pid.hex), _rows(history, pid.hex)) == before
    got = (await c.get(f"/api/v1/printers/{pid}/calibration", headers=admin)).json()
    auto = next(x for x in got if x["tray"] == "auto")
    assert (auto["offset_x_mm"], auto["offset_y_mm"]) == (1.2, -0.4)


async def test_check_readings_refuse_other_runs_and_need_admin(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin, read = await _key(["admin"]), await _key(["read"])
    pid = await _printer()
    measured, checked = await _types()
    await _save(c, pid, measured, admin)
    calibration_page = await _run(pid, "auto", checked, kind="calibration_page")
    probe = await _run(pid, "auto", checked, kind="probe_page")
    other_tray = await _run(pid, "tray-2", checked)

    for run_id, code in (
        (calibration_page, "run_not_a_check_print"),
        (probe, "run_not_a_check_print"),
        (other_tray, "run_not_for_this_tray"),
        (uuid4(), "run_not_for_this_tray"),
    ):
        r = await _check(c, pid, run_id, admin)
        assert r.status_code == 422, r.text
        assert _err(r)["error_code"] == code
    check = await _run(pid, "auto", checked)
    assert (await _check(c, pid, check, read)).status_code == 403
    assert await _log(c, checked, admin) == []


async def test_the_save_still_refuses_a_check_run(api_client_with_seed: Any) -> None:
    """The log has its own route; the save keeps refusing check runs (15b) and logs nothing."""
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    measured, checked = await _types()
    await _save(c, pid, measured, admin)
    run_id = await _run(pid, "auto", checked)
    r = await c.put(
        f"/api/v1/printers/{pid}/calibration/auto",
        json={
            "offset_x_mm": 0, "offset_y_mm": 0, "scale_x": 1, "scale_y": 1, "rotation_deg": 0,
            "measured_on": {"type_key": checked, "version": 1},
            "run_id": str(run_id), "readings": _check_readings(),
        },
        headers=admin,
    )  # fmt: skip
    assert r.status_code == 422 and _err(r)["error_code"] == "run_is_a_check_print"
    assert await _log(c, checked, admin) == []


# ---------------------------------------------------------------------------- the writers


async def test_mark_verified_writes_an_entry(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
    await _create(c, admin)
    r = await c.patch("/api/v1/sheet-types/acme-t1", json={"verified": True}, headers=admin)
    assert r.status_code == 200, r.text
    (entry,) = await _log(c, "acme-t1", admin)
    assert entry["kind"] == "verified" and entry["type_version"] == 1
    assert entry["printer"] is None and entry["actor"]
    # Already verified: no second entry; another edit writes none either.
    await c.patch("/api/v1/sheet-types/acme-t1", json={"verified": True}, headers=admin)
    await c.patch("/api/v1/sheet-types/acme-t1", json={"description": "x"}, headers=admin)
    assert len(await _log(c, "acme-t1", admin)) == 1


@pytest.mark.fresh_install
async def test_new_version_and_catalog_update_write_entries(
    api_client_with_seed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = api_client_with_seed
    admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
    created = await _create(c, admin)
    geometry = {**created["geometry"], "margin_top": created["geometry"]["margin_top"] + 0.5}
    r = await c.post(
        "/api/v1/sheet-types/acme-t1/versions",
        json={"geometry": geometry, "change_note": "Top margin measured"},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    r = await c.post("/api/v1/sheet-types/acme-t1/versions/1/restore", headers=admin)
    assert r.status_code == 201, r.text
    entries = await _log(c, "acme-t1", admin)
    assert [(e["kind"], e["type_version"], e["note"]) for e in entries] == [
        ("new_version", 3, "Restored from version 1"),
        ("new_version", 2, "Top margin measured"),
    ]

    # A catalog update is one entry, with the new version when the geometry changed.
    assert (await _install(c, admin, K5160)).status_code == 201
    _offer(monkeypatch, K5160, 2, description="Revised address labels")
    assert (await c.post(f"/api/v1/sheet-types/{K5160}/catalog-update", headers=admin)).is_success
    margin = loader.load_entry(K5160).version.geometry.margin_top
    _offer(monkeypatch, K5160, 3, geometry={"margin_top": round(margin + 0.1, 2)})
    assert (await c.post(f"/api/v1/sheet-types/{K5160}/catalog-update", headers=admin)).is_success
    entries = await _log(c, K5160, admin)
    assert [(e["kind"], e["type_version"], e["note"]) for e in entries] == [
        ("catalog_update", 2, "Updated to catalog revision 3"),
        ("catalog_update", 1, "Updated to catalog revision 2"),
    ]
    # The person is named in the log; the version itself stays catalog-written.
    assert all(e["actor"] != CATALOG_ACTOR for e in entries)
    async with _factory()() as s:
        t = await ts.get_type(s, K5160)
        v2 = await ts.get_version(s, K5160, 2)
        assert v2.created_by == CATALOG_ACTOR and t.catalog_revision == 3


async def test_import_new_version_writes_an_entry_and_unverifies(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
    await _create(c, admin)
    await c.patch("/api/v1/sheet-types/acme-t1", json={"verified": True}, headers=admin)
    doc = (await c.get("/api/v1/sheet-types/acme-t1/export", headers=admin)).json()
    doc["version"]["geometry"]["margin_top"] += 0.3
    r = await c.post(
        "/api/v1/sheet-types/import",
        params={"mode": "new_version"},
        content=json.dumps(doc),
        headers={**admin, "Content-Type": "application/json"},
    )
    assert r.status_code in (200, 201), r.text
    newest = (await _log(c, "acme-t1", admin))[0]
    assert newest["kind"] == "new_version" and newest["type_version"] == 2
    assert newest["note"] == "Imported from version 1 of an export. No longer verified."
    detail = (await c.get("/api/v1/sheet-types/acme-t1", headers=admin)).json()
    assert detail["verified"] is False


async def test_apply_suggestion_writes_an_entry(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    measured, checked = await _types()
    await _save(c, pid, measured, admin)
    run_id = await _run(pid, "auto", checked)
    r = await c.post(
        f"/api/v1/sheet-types/{checked}/versions/from-suggestion",
        json={"run_id": str(run_id), "pitch_x_mm": 37.55},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    (entry,) = await _log(c, checked, admin)
    assert entry["kind"] == "new_version" and entry["type_version"] == 2
    assert entry["printer"]["id"] == str(pid) and entry["tray"] == "auto"
    assert entry["run"]["id"] == str(run_id) and entry["correction"]["seq"] == 1
    assert entry["note"].startswith("Pitch and margins from the check print of run #")
    # The version is the actor's own (14a): the log entry changes nothing of that.
    async with _factory()() as s:
        v2 = await ts.get_version(s, checked, 2)
        assert v2.created_by == entry["actor"] != CATALOG_ACTOR
        assert v2.change_note is not None and str(pid) not in v2.change_note


# ---------------------------------------------------------------------------- hand entries


async def test_hand_entry_admin_only(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
    read = {"X-Label-Hub-Key": await _insert_key(["read"])}
    await _create(c, admin)
    pid = await _printer()
    body = {"printer_id": str(pid), "tray": "tray-2", "verdict": "feed_variance",
            "note": "Sheets feed 0.3 mm differently."}  # fmt: skip
    assert (
        await c.post("/api/v1/sheet-types/acme-t1/log", json=body, headers=read)
    ).status_code == 403
    r = await c.post("/api/v1/sheet-types/acme-t1/log", json=body, headers=admin)
    assert r.status_code == 201, r.text
    entry = r.json()
    assert (entry["kind"], entry["verdict"], entry["tray"]) == ("note", "feed_variance", "tray-2")
    assert entry["tray_label"] == "Tray 2" and entry["type_version"] == 1

    missing = await c.post(
        "/api/v1/sheet-types/acme-t1/log", json={**body, "printer_id": str(uuid4())}, headers=admin
    )
    assert missing.status_code == 404 and _err(missing)["error_code"] == "printer_not_found"
    no_printer = await c.post(
        "/api/v1/sheet-types/acme-t1/log", json={"tray": "auto"}, headers=admin
    )
    assert no_printer.status_code == 422
    bad = await c.post("/api/v1/sheet-types/acme-t1/log", json={"verdict": "great"}, headers=admin)
    assert bad.status_code == 422
    unknown = await c.post("/api/v1/sheet-types/nope-nope/log", json={}, headers=admin)
    assert unknown.status_code == 404


async def test_patch_verdict_and_note(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
    read = {"X-Label-Hub-Key": await _insert_key(["read"])}
    await _create(c, admin)
    await c.patch("/api/v1/sheet-types/acme-t1", json={"verified": True}, headers=admin)
    (entry,) = await _log(c, "acme-t1", admin)
    url = f"/api/v1/sheet-types/acme-t1/log/{entry['id']}"

    assert (await c.patch(url, json={"verdict": "fits"}, headers=read)).status_code == 403
    r = await c.patch(
        url, json={"verdict": "fits", "note": "Checked on real sheets."}, headers=admin
    )
    assert r.status_code == 200, r.text
    assert (r.json()["verdict"], r.json()["note"]) == ("fits", "Checked on real sheets.")
    # Only the note: the verdict stays; null clears the note.
    r = await c.patch(url, json={"note": None}, headers=admin)
    assert (r.json()["verdict"], r.json()["note"]) == ("fits", None)
    assert {k: v for k, v in r.json().items() if k not in ("verdict", "note")} == {
        k: v for k, v in entry.items() if k not in ("verdict", "note")
    }
    null_verdict = await c.patch(url, json={"verdict": None}, headers=admin)
    assert null_verdict.status_code == 422

    # An entry of another type is not found through this one.
    await _create(c, admin, key="acme-t2", prefix="AT2")
    other = f"/api/v1/sheet-types/acme-t2/log/{entry['id']}"
    r = await c.patch(other, json={"verdict": "printer"}, headers=admin)
    assert r.status_code == 404 and _err(r)["error_code"] == "sheet_type_log_not_found"


async def test_note_sanitised_and_bounded(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
    await _create(c, admin)
    r = await c.post(
        "/api/v1/sheet-types/acme-t1/log",
        json={"note": "  First line​\r\nsecond\x07 line‮  "},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    assert r.json()["note"] == "First line\nsecond line"
    assert (
        await c.post("/api/v1/sheet-types/acme-t1/log", json={"note": "x" * 2000}, headers=admin)
    ).status_code == 201
    too_long = await c.post(
        "/api/v1/sheet-types/acme-t1/log", json={"note": "x" * 2001}, headers=admin
    )
    assert too_long.status_code == 422 and _err(too_long)["error_code"] == "label_too_long"
    blank = await c.post("/api/v1/sheet-types/acme-t1/log", json={"note": " ​ "}, headers=admin)
    assert blank.status_code == 201 and blank.json()["note"] is None


# ---------------------------------------------------------------------------- reading


async def test_log_filter_by_printer(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
    await _create(c, admin)
    p1, p2 = await _printer(), await _printer()
    for pid, tray in ((p1, "auto"), (p1, "tray-2"), (p2, "auto"), (None, None)):
        body = {"printer_id": str(pid), "tray": tray} if pid else {}
        assert (
            await c.post("/api/v1/sheet-types/acme-t1/log", json=body, headers=admin)
        ).is_success

    assert len(await _log(c, "acme-t1", admin)) == 4
    assert {e["tray"] for e in await _log(c, "acme-t1", admin, printer=str(p1))} == {
        "auto",
        "tray-2",
    }
    only = await _log(c, "acme-t1", admin, printer=str(p1), tray="auto")
    assert [(e["printer"]["id"], e["tray"]) for e in only] == [(str(p1), "auto")]
    assert len(await _log(c, "acme-t1", admin, kind="note")) == 4
    assert await _log(c, "acme-t1", admin, kind="verified") == []

    # Newest first, a page at a time, with a Link to the next page.
    r = await c.get("/api/v1/sheet-types/acme-t1/log", params={"limit": 3}, headers=admin)
    assert len(r.json()) == 3 and 'rel="next"' in r.headers["link"]
    times = [e["created_at"] for e in await _log(c, "acme-t1", admin)]
    assert times == sorted(times, reverse=True)
    rest = await c.get(
        "/api/v1/sheet-types/acme-t1/log", params={"limit": 3, "offset": 3}, headers=admin
    )
    assert len(rest.json()) == 1 and "link" not in rest.headers
    too_many = await c.get("/api/v1/sheet-types/acme-t1/log", params={"limit": 101}, headers=admin)
    assert too_many.status_code == 422


async def test_log_hides_other_printers_entries_from_a_restricted_key(
    api_client_with_seed: Any,
) -> None:
    c = api_client_with_seed
    admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
    await _create(c, admin)
    mine, theirs = await _printer(), await _printer()
    restricted = {"X-Label-Hub-Key": await _insert_key(["read"], [str(mine)])}
    for pid in (mine, theirs, None):
        body = {"printer_id": str(pid), "note": f"about {pid}"} if pid else {"note": "general"}
        assert (
            await c.post("/api/v1/sheet-types/acme-t1/log", json=body, headers=admin)
        ).is_success

    seen = await _log(c, "acme-t1", restricted)
    assert sorted(e["note"] for e in seen) == sorted([f"about {mine}", "general"])
    raw = (await c.get("/api/v1/sheet-types/acme-t1/log", headers=restricted)).text
    assert str(theirs) not in raw
    # Asking for the other printer by name gives nothing, not its entries.
    assert await _log(c, "acme-t1", restricted, printer=str(theirs)) == []


async def test_log_not_in_export_doc(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
    await _create(c, admin)
    pid = await _printer()
    marker = f"log-marker-{uuid4().hex}"
    body = {"printer_id": str(pid), "tray": "auto", "verdict": "printer", "note": marker}
    assert (await c.post("/api/v1/sheet-types/acme-t1/log", json=body, headers=admin)).is_success
    await c.patch("/api/v1/sheet-types/acme-t1", json={"verified": True}, headers=admin)
    exported = (await c.get("/api/v1/sheet-types/acme-t1/export", headers=admin)).text
    assert marker not in exported and str(pid) not in exported
    assert "log" not in json.loads(exported)["type"]


async def test_log_not_in_feedback_issue(api_client_with_seed: Any) -> None:
    """The feedback issue is pre-filled from the type detail (`buildFeedbackUrl`); the detail and
    the catalog never carry the log."""
    c = api_client_with_seed
    admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
    await _create(c, admin)
    pid = await _printer()
    marker = f"log-marker-{uuid4().hex}"
    body = {"printer_id": str(pid), "note": marker}
    assert (await c.post("/api/v1/sheet-types/acme-t1/log", json=body, headers=admin)).is_success
    for url in (
        "/api/v1/sheet-types/acme-t1",
        "/api/v1/sheet-types/acme-t1/versions",
        "/api/v1/sheet-types",
        "/api/v1/sheet-catalog",
    ):
        r = await c.get(url, headers=admin)
        assert r.status_code == 200, url
        assert marker not in r.text and str(pid) not in r.text, url


# ---------------------------------------------------------------------------- lifecycle


async def test_printer_delete_sets_printer_null(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
    await _create(c, admin)
    pid = await _printer()
    body = {"printer_id": str(pid), "tray": "auto", "note": "kept"}
    assert (await c.post("/api/v1/sheet-types/acme-t1/log", json=body, headers=admin)).is_success
    # Printers are soft-disabled in the app; a hard delete (trays first, which refuse to cascade)
    # is what design 7.8 means: the entry stays and loses its printer.
    async with _factory()() as s:
        await s.execute(delete(PrinterTray).where(col(PrinterTray.printer_id) == pid))
        await s.execute(delete(Printer).where(col(Printer.id) == pid))
        await s.commit()
    (entry,) = await _log(c, "acme-t1", admin)
    assert entry["printer"] is None and entry["note"] == "kept" and entry["tray"] == "auto"

    # And the type takes its log with it.
    assert (await c.delete("/api/v1/sheet-types/acme-t1", headers=admin)).status_code == 204
    async with _factory()() as s:
        left = (await s.execute(select(func.count()).select_from(SheetTypeLog))).scalar_one()
    assert left == 0


async def test_retention_500(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = {"X-Label-Hub-Key": await _insert_key(["admin"])}
    await _create(c, admin)
    await _create(c, admin, key="acme-t2", prefix="AT2")
    async with _factory()() as s:
        t1, t2 = await ts.get_type(s, "acme-t1"), await ts.get_type(s, "acme-t2")
        v1 = (await ts.current_version(s, t1.id)).id
        first = await type_log.log_entry(
            s, type_id=t1.id, version_id=v1, kind="note", note="the oldest", actor="t"
        )
        first_id = first.id
        await type_log.log_entry(s, type_id=t2.id, version_id=None, kind="note", actor="t")
        for n in range(type_log.LOG_RETENTION):
            await type_log.log_entry(
                s, type_id=t1.id, version_id=v1, kind="note", note=f"n{n}", actor="t"
            )
        await s.commit()
        counts = dict(
            (
                await s.execute(
                    select(col(SheetTypeLog.type_id), func.count()).group_by(
                        col(SheetTypeLog.type_id)
                    )
                )
            ).all()
        )
        assert counts == {t1.id: 500, t2.id: 1}  # another type keeps its own
        assert await s.get(SheetTypeLog, first_id) is None
        assert (await s.execute(select(SheetTypeVersion))).first() is not None
