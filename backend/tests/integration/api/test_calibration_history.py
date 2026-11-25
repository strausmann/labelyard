# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Calibration history, the server fit of a save and restore (#336, design 7.5, ADR 0029).

Every save, reset and restore of a tray's correction is a history entry written in the transaction
that writes ``printer_calibrations``; with readings the stored values are the server's fit.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import app.models  # noqa: F401
import bcrypt
import pytest
from app.calibration import history, runs, service
from app.calibration.fit import crossing_point
from app.calibration.model import CalibrationIn
from app.models.api_key import ApiKey
from app.models.calibration_run import CalibrationHistory
from app.models.printer import Printer
from app.models.printer_calibration import PrinterCalibration
from app.models.printer_tray import PrinterTray
from app.page_render.transform import Deviation, apply_deviation
from app.sheets import type_service as ts
from app.sheets.geometry import Geometry
from sqlalchemy import func, select
from sqlmodel import col

pytestmark = pytest.mark.asyncio

G48 = Geometry("A4", 21000, 29700, 4570, 2120, 4, 12, 4820, 2120, 2130, 985, None, "rectangle")
BODY = {
    "offset_x_mm": 1.2,
    "offset_y_mm": -0.4,
    "scale_x": 1.001,
    "scale_y": 0.999,
    "rotation_deg": 0.3,
}
OTHER = {
    "offset_x_mm": -0.41,
    "offset_y_mm": 0.66,
    "scale_x": 1.0,
    "scale_y": 1.0,
    "rotation_deg": 0.0,
}
VALUE_KEYS = ("offset_x_mm", "offset_y_mm", "scale_x", "scale_y", "rotation_deg")


def _factory():  # type: ignore[no-untyped-def]
    import app.db.engine as _engine_module

    return _engine_module.async_session


async def _key(scopes: list[str], allowed: list[str] | None = None) -> dict[str, str]:
    plaintext = f"lh_pat_{uuid4().hex}"
    hashed = bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt(rounds=4)).decode()
    async with _factory()() as s:
        s.add(
            ApiKey(
                name=f"calh-{uuid4().hex[:6]}",
                key_hash=hashed,
                key_prefix=plaintext[:16],
                scopes=scopes,
                allowed_printer_ids=allowed or [],
                enabled=True,
                rate_limit_per_minute=6000,
            )
        )
        await s.commit()
    return {"X-Label-Hub-Key": plaintext}


async def _printer() -> UUID:
    async with _factory()() as s:
        p = Printer(
            name=f"p-{uuid4().hex[:6]}",
            slug=f"p-{uuid4().hex[:6]}",
            model="m",
            backend="ipp",
            connection={"uri": "ipp://printer.example.invalid/ipp/print"},
        )
        s.add(p)
        s.add(PrinterTray(printer_id=p.id, tray_key="auto", label="Auto", is_default=True))
        s.add(PrinterTray(printer_id=p.id, tray_key="tray-2", label="Tray 2"))
        await s.commit()
        return p.id


async def _type(key: str = "calh-test", prefix: str = "CALH") -> None:
    async with _factory()() as s:
        await ts.create_type(s, ts.TypeFields(key, prefix, "Acme", [], None, None), G48, actor="t")


def _readings(dev: Deviation) -> list[dict[str, Any]]:
    out = []
    for col_gap, row_gap in [(1, 1), (3, 1), (1, 11), (3, 11), (2, 6)]:
        qx, qy = crossing_point(G48, col_gap, row_gap)
        px, py = apply_deviation(dev, "A4", (qx, qy))
        out.append(
            {"col_gap": col_gap, "row_gap": row_gap, "across_mm": px - qx, "down_mm": py - qy}
        )
    return out


async def _history(c: Any, pid: UUID, tray: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    r = await c.get(f"/api/v1/printers/{pid}/calibration/{tray}/history", headers=headers)
    assert r.status_code == 200, r.text
    return list(r.json())


def _values(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: entry["values"][k] for k in VALUE_KEYS}


# ---------------------------------------------------------------------------- saves


async def test_save_writes_a_history_entry_in_the_same_transaction(
    api_client_with_seed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    put = await c.put(f"/api/v1/printers/{pid}/calibration/auto", json=BODY, headers=admin)
    assert put.status_code == 200, put.text
    (entry,) = await _history(c, pid, "auto", admin)
    assert entry["action"] == "save" and entry["seq"] == 1 and entry["source"] == "manual"
    assert _values(entry) == BODY and entry["previous"] is None

    # The history write fails: nothing of the save is kept (one transaction).
    async def broken(*_a: Any, **_k: Any) -> CalibrationHistory:
        raise RuntimeError("history write failed")

    monkeypatch.setattr(history, "record", broken)
    async with _factory()() as s:
        with pytest.raises(RuntimeError):
            await service.put(
                s, pid, "auto", CalibrationIn(**OTHER), measured_on=None, actor="user:admin"
            )
    async with _factory()() as s:
        row = await s.get(PrinterCalibration, (pid, "auto"))
        assert row is not None and row.offset_x_hmm == 120  # still BODY
        count = (
            await s.execute(select(func.count()).where(col(CalibrationHistory.printer_id) == pid))
        ).scalar_one()
    assert count == 1


async def test_reset_writes_a_reset_entry(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    await c.put(f"/api/v1/printers/{pid}/calibration/auto", json=BODY, headers=admin)
    gone = await c.delete(f"/api/v1/printers/{pid}/calibration/auto", headers=admin)
    assert gone.status_code == 204
    newest, first = await _history(c, pid, "auto", admin)
    assert newest["action"] == "reset" and newest["seq"] == 2
    assert all(newest["values"][k] is None for k in VALUE_KEYS)
    assert {k: newest["previous"][k] for k in VALUE_KEYS} == BODY
    assert newest["diff"]["offset_x_mm"] == {"old": 1.2, "new": None}
    # A tray without a calibration has nothing to reset: no entry.
    again = await c.delete(f"/api/v1/printers/{pid}/calibration/auto", headers=admin)
    assert again.status_code == 204
    assert len(await _history(c, pid, "auto", admin)) == 2
    assert first["action"] == "save"


async def test_history_seq_increments_per_tray(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    base = f"/api/v1/printers/{pid}/calibration"
    assert (await c.put(f"{base}/auto", json=BODY, headers=admin)).status_code == 200
    assert (await c.put(f"{base}/tray-2", json=OTHER, headers=admin)).status_code == 200
    assert (await c.put(f"{base}/auto", json=OTHER, headers=admin)).status_code == 200
    auto = await _history(c, pid, "auto", admin)
    assert [e["seq"] for e in auto] == [2, 1]  # newest first
    assert {k: auto[0]["previous"][k] for k in VALUE_KEYS} == BODY
    assert auto[0]["diff"]["offset_x_mm"] == {"old": 1.2, "new": -0.41}
    assert "scale_x" in auto[0]["diff"] and "scale_y" in auto[0]["diff"]
    assert [e["seq"] for e in await _history(c, pid, "tray-2", admin)] == [1]
    paged = await c.get(f"{base}/auto/history?limit=1&offset=1", headers=admin)
    assert [e["seq"] for e in paged.json()] == [1]
    first_page = await c.get(f"{base}/auto/history?limit=1", headers=admin)
    assert 'rel="next"' in first_page.headers["link"]
    too_many = await c.get(f"{base}/auto/history?limit=101", headers=admin)
    assert too_many.status_code == 422


# ---------------------------------------------------------------------------- the server fit


DEV = Deviation(120, -40, 1_001_000, 999_000, 300_000)


async def _fitted(c: Any, pid: UUID, headers: dict[str, str]) -> dict[str, float]:
    fit = await c.post(
        f"/api/v1/printers/{pid}/calibration/auto/fit",
        json={"type": "calh-test", "readings": _readings(DEV)},
        headers=headers,
    )
    assert fit.status_code == 200, fit.text
    return dict(fit.json()["values"])


async def test_readings_with_matching_values_stored_without_note(
    api_client_with_seed: Any,
) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    await _type()
    values = await _fitted(c, pid, admin)
    put = await c.put(
        f"/api/v1/printers/{pid}/calibration/auto",
        json={
            **values,
            "measured_on": {"type_key": "calh-test", "version": 1},
            "readings": _readings(DEV),
            "note": "first try",
        },
        headers=admin,
    )
    assert put.status_code == 200, put.text
    out = put.json()
    assert out["notes"] == [] and out["ignored_fields"] == []
    assert {k: out[k] for k in VALUE_KEYS} == values
    (entry,) = await _history(c, pid, "auto", admin)
    assert entry["source"] == "fit" and entry["note"] == "first try"
    assert len(entry["readings"]) == 5 and len(entry["residuals_mm"]) == 5
    assert entry["measured_on"] == {"type_key": "calh-test", "version": 1}


async def test_values_off_by_0_06_mm_store_the_server_fit_with_client_values_ignored(
    api_client_with_seed: Any,
) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    await _type()
    values = await _fitted(c, pid, admin)
    sent = {**values, "offset_x_mm": round(values["offset_x_mm"] + 0.06, 2)}
    put = await c.put(
        f"/api/v1/printers/{pid}/calibration/auto",
        json={
            **sent,
            "measured_on": {"type_key": "calh-test", "version": 1},
            "readings": _readings(DEV),
        },
        headers=admin,
    )
    assert put.status_code == 200, put.text
    out = put.json()
    assert out["notes"] == ["client_values_ignored"]
    assert out["ignored_fields"] == ["offset_x_mm"]
    assert {k: out[k] for k in VALUE_KEYS} == values  # the server's fit, not the sent value
    (entry,) = await _history(c, pid, "auto", admin)
    assert _values(entry) == values and entry["source"] == "fit"
    # A scale that moves the far corner by more than 0.05 mm is named too.
    scaled = {**values, "scale_x": round(values["scale_x"] + 0.000_02, 6)}
    put = await c.put(
        f"/api/v1/printers/{pid}/calibration/auto",
        json={
            **scaled,
            "measured_on": {"type_key": "calh-test", "version": 1},
            "readings": _readings(DEV),
        },
        headers=admin,
    )
    assert put.json()["ignored_fields"] == [], "0.00002 x 105 mm is 0.002 mm, inside"
    scaled = {**values, "scale_x": round(values["scale_x"] + 0.001, 6)}
    put = await c.put(
        f"/api/v1/printers/{pid}/calibration/auto",
        json={
            **scaled,
            "measured_on": {"type_key": "calh-test", "version": 1},
            "readings": _readings(DEV),
        },
        headers=admin,
    )
    assert put.json()["ignored_fields"] == ["scale_x"]


async def test_values_without_readings_stored_as_sent_source_manual(
    api_client_with_seed: Any,
) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    await _type()
    put = await c.put(
        f"/api/v1/printers/{pid}/calibration/auto",
        json={**BODY, "measured_on": {"type_key": "calh-test", "version": 1}},
        headers=admin,
    )
    assert put.status_code == 200, put.text
    assert {k: put.json()[k] for k in VALUE_KEYS} == BODY and put.json()["notes"] == []
    (entry,) = await _history(c, pid, "auto", admin)
    assert entry["source"] == "manual" and entry["readings"] is None
    assert _values(entry) == BODY


async def test_readings_without_a_measured_on_version_are_refused(
    api_client_with_seed: Any,
) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    put = await c.put(
        f"/api/v1/printers/{pid}/calibration/auto",
        json={**BODY, "readings": _readings(DEV)},
        headers=admin,
    )
    assert put.status_code == 422
    assert put.json()["detail"]["error_detail"]["field"] == "measured_on"
    long_note = await c.put(
        f"/api/v1/printers/{pid}/calibration/auto",
        json={**BODY, "note": "x" * 501},
        headers=admin,
    )
    assert long_note.status_code == 422
    too_many = await c.put(
        f"/api/v1/printers/{pid}/calibration/auto",
        json={**BODY, "readings": _readings(DEV) * 41},
        headers=admin,
    )
    assert too_many.status_code == 422


async def _run(printer_id: UUID, tray: str) -> UUID:
    async with _factory()() as s:
        run = await runs.allocate_run(
            s,
            printer_id=printer_id,
            tray=tray,
            kind="calibration_page",
            job_id=uuid4(),
            type_version_id=uuid4(),
            correction=None,
            actor="user:admin",
        )
        return run.id


async def test_put_with_a_run_of_another_printer_is_422(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid, other = await _printer(), await _printer()
    url = f"/api/v1/printers/{pid}/calibration/auto"
    for run_id in (await _run(other, "auto"), await _run(pid, "tray-2"), uuid4()):
        r = await c.put(url, json={**BODY, "run_id": str(run_id)}, headers=admin)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error_code"] == "run_not_for_this_tray"
    async with _factory()() as s:
        assert await s.get(PrinterCalibration, (pid, "auto")) is None  # nothing stored
    own = await _run(pid, "auto")
    ok = await c.put(url, json={**BODY, "run_id": str(own)}, headers=admin)
    assert ok.status_code == 200, ok.text
    (entry,) = await _history(c, pid, "auto", admin)
    assert entry["run_id"] == str(own) and entry["run_no"] == 1


# ---------------------------------------------------------------------------- restore


async def _two_saves(c: Any, pid: UUID, admin: dict[str, str]) -> list[dict[str, Any]]:
    base = f"/api/v1/printers/{pid}/calibration/auto"
    assert (await c.put(base, json=BODY, headers=admin)).status_code == 200
    assert (await c.put(base, json=OTHER, headers=admin)).status_code == 200
    return await _history(c, pid, "auto", admin)


async def test_restore_writes_a_revert_entry_with_the_old_values(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    _second, first = await _two_saves(c, pid, admin)
    r = await c.post(
        f"/api/v1/printers/{pid}/calibration/auto/history/{first['id']}/restore", headers=admin
    )
    assert r.status_code == 200, r.text
    assert {k: r.json()[k] for k in VALUE_KEYS} == BODY and r.json()["notes"] == []
    entries = await _history(c, pid, "auto", admin)
    assert len(entries) == 3  # nothing deleted
    revert = entries[0]
    assert revert["action"] == "revert" and revert["reverted_from_id"] == first["id"]
    assert _values(revert) == BODY
    assert {k: revert["previous"][k] for k in VALUE_KEYS} == OTHER
    got = await c.get(f"/api/v1/printers/{pid}/calibration", headers=admin)
    assert got.json()[0]["offset_x_mm"] == 1.2
    missing = await c.post(
        f"/api/v1/printers/{pid}/calibration/auto/history/{uuid4()}/restore", headers=admin
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["error_code"] == "calibration_history_not_found"
    wrong_tray = await c.post(
        f"/api/v1/printers/{pid}/calibration/tray-2/history/{first['id']}/restore", headers=admin
    )
    assert wrong_tray.status_code == 404


async def test_restore_keeps_the_note_cleaned(api_client_with_seed: Any) -> None:
    """The restore confirmation asks for an optional note (M7); it lands on the revert entry."""
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    _second, first = await _two_saves(c, pid, admin)
    url = f"/api/v1/printers/{pid}/calibration/auto/history/{first['id']}/restore"
    r = await c.post(url, headers=admin, json={"note": "  Back to the values of 11-15\x07 "})
    assert r.status_code == 200, r.text
    newest = (await _history(c, pid, "auto", admin))[0]
    assert newest["action"] == "revert" and newest["note"] == "Back to the values of 11-15"
    # Without a body, and with an empty note, the entry has no note.
    assert (await c.post(url, headers=admin)).status_code == 200
    assert (await _history(c, pid, "auto", admin))[0]["note"] is None
    assert (await c.post(url, headers=admin, json={"note": "   "})).status_code == 200
    assert (await _history(c, pid, "auto", admin))[0]["note"] is None
    too_long = await c.post(url, headers=admin, json={"note": "x" * 501})
    assert too_long.status_code == 422


async def test_restore_twice_writes_two_entries_with_equal_values(
    api_client_with_seed: Any,
) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    _second, first = await _two_saves(c, pid, admin)
    url = f"/api/v1/printers/{pid}/calibration/auto/history/{first['id']}/restore"
    assert (await c.post(url, headers=admin)).status_code == 200
    assert (await c.post(url, headers=admin)).status_code == 200
    newest, older, *_ = await _history(c, pid, "auto", admin)
    assert newest["action"] == older["action"] == "revert"
    assert _values(newest) == _values(older) == BODY
    assert newest["seq"] == 4 and older["seq"] == 3
    assert newest["diff"] == {}


async def test_restore_of_a_reset_entry_clears_the_tray(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    base = f"/api/v1/printers/{pid}/calibration/auto"
    await c.put(base, json=BODY, headers=admin)
    await c.delete(base, headers=admin)
    await c.put(base, json=OTHER, headers=admin)
    reset = (await _history(c, pid, "auto", admin))[1]
    r = await c.post(f"{base}/history/{reset['id']}/restore", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["method"] is None and r.json()["offset_x_mm"] is None
    async with _factory()() as s:
        assert await s.get(PrinterCalibration, (pid, "auto")) is None
    assert (await _history(c, pid, "auto", admin))[0]["action"] == "revert"


async def test_restore_of_entry_on_deleted_version_sets_measured_on_null(
    api_client_with_seed: Any,
) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    await _type()
    base = f"/api/v1/printers/{pid}/calibration/auto"
    on_type = {**BODY, "measured_on": {"type_key": "calh-test", "version": 1}}
    assert (await c.put(base, json=on_type, headers=admin)).status_code == 200
    assert (await c.put(base, json=OTHER, headers=admin)).status_code == 200
    gone = await c.delete("/api/v1/sheet-types/calh-test", headers=admin)
    assert gone.status_code == 204, gone.text
    first = (await _history(c, pid, "auto", admin))[1]
    assert first["measured_on"] is None and first["measured_on_deleted"] is True
    r = await c.post(f"{base}/history/{first['id']}/restore", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["measured_on"] is None and r.json()["notes"] == ["measured_on_deleted"]
    async with _factory()() as s:
        row = await s.get(PrinterCalibration, (pid, "auto"))
    assert row is not None and row.measured_on_type_version_id is None


async def test_restore_keeps_everything_when_the_history_write_fails(
    api_client_with_seed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    _second, first = await _two_saves(c, pid, admin)

    async def broken(*_a: Any, **_k: Any) -> CalibrationHistory:
        raise RuntimeError("history write failed")

    monkeypatch.setattr(history, "record", broken)
    async with _factory()() as s:
        with pytest.raises(RuntimeError):
            await history.restore(s, pid, "auto", UUID(first["id"]), actor="user:admin")
    async with _factory()() as s:
        row = await s.get(PrinterCalibration, (pid, "auto"))
    assert row is not None and row.offset_x_hmm == -41  # still OTHER


async def test_restore_needs_admin(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    _second, first = await _two_saves(c, pid, admin)
    url = f"/api/v1/printers/{pid}/calibration/auto/history/{first['id']}/restore"
    for scopes in (["read"], ["print"]):
        r = await c.post(url, headers=await _key(scopes))
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["error_code"] == "insufficient_scope"
    assert (await c.post(url)).status_code == 401
    assert len(await _history(c, pid, "auto", admin)) == 2


# ---------------------------------------------------------------------------- access, retention


async def test_history_of_another_printer_is_403_for_a_restricted_key(
    api_client_with_seed: Any,
) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid, other = await _printer(), await _printer()
    await c.put(f"/api/v1/printers/{pid}/calibration/auto", json=BODY, headers=admin)
    bound = await _key(["read"], allowed=[str(other)])
    r = await c.get(f"/api/v1/printers/{pid}/calibration/auto/history", headers=bound)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error_code"] == "printer_not_allowed"
    own = await c.get(f"/api/v1/printers/{other}/calibration/auto/history", headers=bound)
    assert own.status_code == 200 and own.json() == []
    unknown = await c.get(
        f"/api/v1/printers/{uuid4()}/calibration/auto/history", headers=await _key(["read"])
    )
    assert unknown.status_code == 404
    assert (await c.get(f"/api/v1/printers/{pid}/calibration/auto/history")).status_code == 401


async def test_history_retention_500(api_client_with_seed: Any) -> None:
    del api_client_with_seed
    pid = await _printer()
    async with _factory()() as s:
        for i in range(505):
            await history.record(
                s,
                printer_id=pid,
                tray="auto",
                action="save",
                new=Deviation(i, 0, 1_000_000, 1_000_000, 0),
                previous=None,
                method="manual",
                measured_on_version_id=None,
                run_id=None,
                readings=None,
                residuals=None,
                reverted_from_id=None,
                actor="user:admin",
                note=None,
                source="manual",
            )
        await s.commit()
    async with _factory()() as s:
        seqs = (
            (
                await s.execute(
                    select(col(CalibrationHistory.seq)).where(
                        col(CalibrationHistory.printer_id) == pid
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(seqs) == 500 and min(seqs) == 6 and max(seqs) == 505


async def test_two_concurrent_saves_are_two_entries_in_commit_order(production_engine) -> None:  # type: ignore[no-untyped-def]
    del production_engine
    pid = await _printer()

    async def save(values: dict[str, float]) -> None:
        async with _factory()() as s:
            await service.put(
                s, pid, "auto", CalibrationIn(**values), measured_on=None, actor="user:admin"
            )

    await asyncio.gather(save(BODY), save(OTHER))
    async with _factory()() as s:
        entries = (
            (
                await s.execute(
                    select(CalibrationHistory)
                    .where(col(CalibrationHistory.printer_id) == pid)
                    .order_by(col(CalibrationHistory.seq))
                )
            )
            .scalars()
            .all()
        )
        row = await s.get(PrinterCalibration, (pid, "auto"))
    assert [e.seq for e in entries] == [1, 2]
    first, second = entries
    assert first.previous is None
    assert second.previous == {
        "offset_x_hmm": first.offset_x_hmm,
        "offset_y_hmm": first.offset_y_hmm,
        "scale_x_ppm": first.scale_x_ppm,
        "scale_y_ppm": first.scale_y_ppm,
        "rotation_udeg": first.rotation_udeg,
    }
    assert row is not None and row.offset_x_hmm == second.offset_x_hmm


# ---------------------------------------------------------------------------- review round 1 (#442)


async def test_snapshot_and_history_id_are_read_in_one_statement(
    api_client_with_seed: Any,
) -> None:
    """Codex on #442: a save committed between two reads would pair the old snapshot with the
    new entry's id. Both come from one SELECT, which SQLite answers from one snapshot.

    Statement guard (what the test can and cannot see): it records the SQL the call runs and
    asserts that the one statement reading ``printer_calibrations`` also reads
    ``calibration_history``. It cannot tell a comment or an alias apart; a deterministic
    interleaving is not possible because no await point lies inside one statement.
    """
    from sqlalchemy import event

    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    assert (
        await c.put(f"/api/v1/printers/{pid}/calibration/auto", json=BODY, headers=admin)
    ).status_code == 200
    seen: list[str] = []

    def capture(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        seen.append(statement)

    engine = _factory()().bind
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with _factory()() as s:
            snap, entry = await service.snapshot_with_history(s, pid, "auto")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    reading_row = [q for q in seen if "printer_calibrations" in q]
    assert len(reading_row) == 1, seen
    assert "calibration_history" in reading_row[0]
    assert snap.dx_hmm == 120 and entry is not None


async def test_removing_a_calibrated_tray_writes_a_reset_entry(api_client_with_seed: Any) -> None:
    """Codex on #442: the admin tray list drops the calibration of a removed tray; its history
    must say so, or a re-added tray's prints count against the old correction."""
    from app.schemas.printer_admin import PrinterUpdatePayload, TrayIn
    from app.services.printer_admin_service import PrinterAdminService

    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    await c.put(f"/api/v1/printers/{pid}/calibration/tray-2", json=BODY, headers=admin)
    await c.put(f"/api/v1/printers/{pid}/calibration/auto", json=OTHER, headers=admin)
    async with _factory()() as s:
        printer = await s.get(Printer, pid)
        assert printer is not None
        only_auto = [TrayIn(key="auto", label="Auto", is_default=True)]
        await PrinterAdminService(
            s, "api-key:not-for-history", history_actor="user:admin"
        ).update_printer(printer.slug, PrinterUpdatePayload(trays=only_auto))
    reset, saved = await _history(c, pid, "tray-2", admin)
    assert reset["action"] == "reset" and reset["actor"] == "user:admin"
    assert {k: reset["previous"][k] for k in VALUE_KEYS} == BODY
    assert [e["action"] for e in await _history(c, pid, "auto", admin)] == ["save"]
    assert saved["action"] == "save"
    async with _factory()() as s:  # re-added (directly: a new key would probe the printer)
        s.add(PrinterTray(printer_id=pid, tray_key="tray-2", label="Tray 2"))
        await s.commit()
        snap, entry = await service.snapshot_with_history(s, pid, "tray-2")
    assert snap.method == "none" and entry == UUID(reset["id"])


# ---------------------------------------------------------------------------- task review round 1


async def test_a_history_page_is_one_query(api_client_with_seed: Any) -> None:
    """F3: the window of an entry ends at its neighbour and ``jobs_count`` is a correlated count;
    no per-entry queries. Counts every statement ``list_entries`` sends."""
    from sqlalchemy import event

    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    base = f"/api/v1/printers/{pid}/calibration/auto"
    for body in (BODY, OTHER, BODY):
        assert (await c.put(base, json=body, headers=admin)).status_code == 200
    seen: list[str] = []

    def capture(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        seen.append(statement)

    engine = _factory()().bind
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with _factory()() as s:
            entries, more = await history.list_entries(s, pid, "auto", limit=2, offset=1)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert len(seen) == 1, seen
    assert [e.seq for e in entries] == [2, 1] and more is False


async def test_delete_keeps_the_row_when_the_reset_entry_fails(
    api_client_with_seed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5: the reset entry and the row delete are one transaction."""
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    await c.put(f"/api/v1/printers/{pid}/calibration/auto", json=BODY, headers=admin)

    async def broken(*_a: Any, **_k: Any) -> CalibrationHistory:
        raise RuntimeError("history write failed")

    monkeypatch.setattr(history, "record", broken)
    async with _factory()() as s:
        with pytest.raises(RuntimeError):
            await service.delete(s, pid, "auto", actor="user:admin")
    async with _factory()() as s:
        assert await s.get(PrinterCalibration, (pid, "auto")) is not None


async def test_replace_trays_keeps_trays_and_calibrations_when_the_reset_fails(
    api_client_with_seed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5: a failing reset entry rolls back the whole tray update."""
    from app.schemas.printer_admin import PrinterUpdatePayload, TrayIn
    from app.services.printer_admin_service import PrinterAdminService

    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    await c.put(f"/api/v1/printers/{pid}/calibration/tray-2", json=BODY, headers=admin)

    async def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("history write failed")

    monkeypatch.setattr(history, "record_resets", broken)
    async with _factory()() as s:
        printer = await s.get(Printer, pid)
        assert printer is not None
        with pytest.raises(RuntimeError):
            await PrinterAdminService(s, "t", history_actor="user:admin").update_printer(
                printer.slug,
                PrinterUpdatePayload(trays=[TrayIn(key="auto", label="Auto", is_default=True)]),
            )
    async with _factory()() as s:
        assert await s.get(PrinterCalibration, (pid, "tray-2")) is not None
        assert await s.get(PrinterTray, (pid, "tray-2")) is not None


async def test_put_with_a_run_of_another_version_is_422(api_client_with_seed: Any) -> None:
    """F7: the entry may not name a run and a measured-on version that disagree."""
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    await _type()
    run = await _run(pid, "auto")  # printed for a version that is not calh-test v1
    r = await c.put(
        f"/api/v1/printers/{pid}/calibration/auto",
        json={**BODY, "run_id": str(run), "measured_on": {"type_key": "calh-test", "version": 1}},
        headers=admin,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error_code"] == "run_version_mismatch"
    async with _factory()() as s:
        assert await s.get(PrinterCalibration, (pid, "auto")) is None
        version = await ts.get_version(s, "calh-test", 1)
        own = await runs.allocate_run(
            s,
            printer_id=pid,
            tray="auto",
            kind="calibration_page",
            job_id=uuid4(),
            type_version_id=version.id,
            correction=None,
            actor="user:admin",
        )
    ok = await c.put(
        f"/api/v1/printers/{pid}/calibration/auto",
        json={
            **BODY,
            "run_id": str(own.id),
            "measured_on": {"type_key": "calh-test", "version": 1},
        },
        headers=admin,
    )
    assert ok.status_code == 200, ok.text


async def test_fewer_than_three_readings_are_refused_by_the_schema(
    api_client_with_seed: Any,
) -> None:
    """F8: ``readings`` takes 3 to 200, as the fit does; OpenAPI shows the bound."""
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    await _type()
    for readings in ([], _readings(DEV)[:2]):
        r = await c.put(
            f"/api/v1/printers/{pid}/calibration/auto",
            json={**BODY, "measured_on": {"type_key": "calh-test", "version": 1},
                  "readings": readings},
            headers=admin,
        )  # fmt: skip
        assert r.status_code == 422, r.text
        assert r.json()["detail"][0]["loc"][-1] == "readings"
    schema = (await c.get("/openapi.json")).json()["components"]["schemas"]["CalibrationPut"]
    branch = next(b for b in schema["properties"]["readings"]["anyOf"] if b.get("type") == "array")
    assert branch["minItems"] == 3 and branch["maxItems"] == 200
