# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The fit tied to a run, the geometry suggestion and "apply as new version" (#240, design 7.4)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import app.models  # noqa: F401
import bcrypt
import pytest
from app.calibration import runs
from app.calibration.fit import crossing_point
from app.models.api_key import ApiKey
from app.models.job import Job
from app.models.printer import Printer
from app.models.printer_tray import PrinterTray
from app.page_render.transform import Deviation, apply_deviation, correction_mm
from app.sheets import type_service as ts
from app.sheets.geometry import Geometry

pytestmark = pytest.mark.asyncio

# The type the correction is measured on (4 x 12) and the checked one (5 x 13).
MEASURED = Geometry("A4", 21000, 29700, 4570, 2120, 4, 12, 4820, 2120, 2130, 985, None, "rectangle")
CHECKED = Geometry("A4", 21000, 29700, 3700, 2120, 5, 13, 3820, 2120, 1080, 990, None, "rectangle")
TRUE = replace(CHECKED, pitch_x=3755)  # the real sheet: pitch_x 38.20 -> 37.55 mm
PRINTER = Deviation(120, -40, 1_001_000, 999_000, 0)
CORRECTION = {
    "offset_x_mm": 1.2,
    "offset_y_mm": -0.4,
    "scale_x": 1.001,
    "scale_y": 0.999,
    "rotation_deg": 0.0,
}
SNAPSHOT = {
    "method": "manual",
    "dx_hmm": 120,
    "dy_hmm": -40,
    "sx_ppm": 1_001_000,
    "sy_ppm": 999_000,
    "rotation_udeg": 0,
}
CROSSINGS = [(1, 1), (4, 1), (1, 12), (4, 12), (2, 6), (3, 9)]


def _factory():  # type: ignore[no-untyped-def]
    import app.db.engine as _engine_module

    return _engine_module.async_session


async def _key(scopes: list[str]) -> dict[str, str]:
    plaintext = f"lh_pat_{uuid4().hex}"
    hashed = bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt(rounds=4)).decode()
    async with _factory()() as s:
        s.add(
            ApiKey(
                name=f"fitrun-{uuid4().hex[:6]}",
                key_hash=hashed,
                key_prefix=plaintext[:16],
                scopes=scopes,
                allowed_printer_ids=[],
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


async def _types() -> tuple[str, str]:
    suffix = uuid4().hex[:6]
    measured, checked = f"fit-m-{suffix}", f"fit-c-{suffix}"
    async with _factory()() as s:
        await ts.create_type(
            s, ts.TypeFields(measured, f"FM{suffix[:4].upper()}", "Acme", [], None, None),
            MEASURED, actor="t",
        )  # fmt: skip
        await ts.create_type(
            s, ts.TypeFields(checked, f"FC{suffix[:4].upper()}", "Acme", [], None, None),
            CHECKED, actor="t",
        )  # fmt: skip
    return measured, checked


async def _version_id(key: str) -> UUID:
    async with _factory()() as s:
        return (await ts.current_version(s, (await ts.get_type(s, key)).id)).id


async def _run(
    printer_id: UUID, tray: str, key: str, *, kind: str = "check_page", job_id: UUID | None = None
) -> UUID:
    version_id = await _version_id(key)
    async with _factory()() as s:
        run = await runs.allocate_run(
            s,
            printer_id=printer_id,
            tray=tray,
            kind=kind,  # type: ignore[arg-type]
            job_id=job_id or uuid4(),
            type_version_id=version_id,
            correction=SNAPSHOT if kind == "check_page" else None,
            actor="user:admin",
        )
        return run.id


def _check_readings() -> list[dict[str, Any]]:
    out = []
    for col_gap, row_gap in CROSSINGS:
        drawn = correction_mm(PRINTER, "A4").apply(crossing_point(CHECKED, col_gap, row_gap))
        ink = apply_deviation(PRINTER, "A4", drawn)
        paper = crossing_point(TRUE, col_gap, row_gap)
        out.append(
            {"col_gap": col_gap, "row_gap": row_gap,
             "across_mm": ink[0] - paper[0], "down_mm": ink[1] - paper[1]}
        )  # fmt: skip
    return out


async def _save(c: Any, pid: UUID, measured_on: str, admin: dict[str, str]) -> None:
    r = await c.put(
        f"/api/v1/printers/{pid}/calibration/auto",
        json={**CORRECTION, "measured_on": {"type_key": measured_on, "version": 1}},
        headers=admin,
    )
    assert r.status_code == 200, r.text


async def test_fit_with_run_returns_the_suggestion(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin, read = await _key(["admin"]), await _key(["read"])
    pid = await _printer()
    measured, checked = await _types()
    await _save(c, pid, measured, admin)
    run_id = await _run(pid, "auto", checked)
    url = f"/api/v1/printers/{pid}/calibration/auto/fit"
    body = {"type": checked, "readings": _check_readings()}

    r = await c.post(url, json={**body, "run_id": str(run_id)}, headers=read)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["suggestion_unavailable"] is None
    suggestion = out["suggestion"]
    assert suggestion["pitch_x_hmm"] == pytest.approx(3755, abs=5)
    assert suggestion["pitch_y_hmm"] is None and suggestion["margin_top_hmm"] is None
    assert suggestion["uneven"] is False

    # Without a run the fit is the plain fit, as before.
    plain = (await c.post(url, json=body, headers=read)).json()
    assert plain["suggestion"] is None and plain["suggestion_unavailable"] is None
    assert plain["values"] == out["values"]

    # A calibration page is no check print.
    cal = await _run(pid, "auto", checked, kind="calibration_page")
    r = await c.post(url, json={**body, "run_id": str(cal)}, headers=read)
    assert r.status_code == 200, r.text
    assert r.json()["suggestion"] is None
    assert r.json()["suggestion_unavailable"] == "no_check_run"

    # A correction measured on the checked type itself cannot tell the geometry apart.
    await _save(c, pid, checked, admin)
    same = await _run(pid, "auto", checked)
    r = await c.post(url, json={**body, "run_id": str(same)}, headers=read)
    assert r.status_code == 200, r.text
    assert r.json()["suggestion"] is None
    assert r.json()["suggestion_unavailable"] == "same_type"


async def test_the_job_names_the_correction_the_check_was_printed_with(
    api_client_with_seed: Any,
) -> None:
    """The job's `calibration_history_id` wins over "the newest entry before the run": a save
    between the snapshot and the run must not change which correction the readings checked."""
    c = api_client_with_seed
    admin, read = await _key(["admin"]), await _key(["read"])
    pid = await _printer()
    measured, checked = await _types()
    await _save(c, pid, measured, admin)
    history_url = f"/api/v1/printers/{pid}/calibration/auto/history"
    (entry,) = (await c.get(history_url, headers=admin)).json()
    await _save(c, pid, checked, admin)  # newer, measured on the checked type
    job_id = uuid4()
    async with _factory()() as s:
        s.add(Job(id=job_id, printer_id=pid, payload={"calibration_history_id": entry["id"]},
                  state="done"))  # fmt: skip
        await s.commit()
    run_id = await _run(pid, "auto", checked, job_id=job_id)
    r = await c.post(
        f"/api/v1/printers/{pid}/calibration/auto/fit",
        json={"type": checked, "readings": _check_readings(), "run_id": str(run_id)},
        headers=read,
    )
    assert r.status_code == 200, r.text
    assert r.json()["suggestion_unavailable"] is None
    assert r.json()["suggestion"]["pitch_x_hmm"] == pytest.approx(3755, abs=5)


async def test_a_correction_of_unknown_origin_gives_no_suggestion(
    api_client_with_seed: Any,
) -> None:
    """Typed in by hand, or its history entry gone: the correction may hold this type's own
    geometry error, so nothing is suggested (Codex review on #448)."""
    c = api_client_with_seed
    admin, read = await _key(["admin"]), await _key(["read"])
    _measured, checked = await _types()
    body = {"type": checked, "readings": _check_readings()}

    no_history = await _printer()  # a run whose correction has no entry at all
    run_id = await _run(no_history, "auto", checked)
    r = await c.post(
        f"/api/v1/printers/{no_history}/calibration/auto/fit",
        json={**body, "run_id": str(run_id)},
        headers=read,
    )
    assert r.status_code == 200, r.text
    assert r.json()["suggestion"] is None
    assert r.json()["suggestion_unavailable"] == "unknown_correction"

    by_hand = await _printer()
    saved = await c.put(
        f"/api/v1/printers/{by_hand}/calibration/auto", json=CORRECTION, headers=admin
    )
    assert saved.status_code == 200, saved.text
    run_id = await _run(by_hand, "auto", checked)
    r = await c.post(
        f"/api/v1/printers/{by_hand}/calibration/auto/fit",
        json={**body, "run_id": str(run_id)},
        headers=read,
    )
    assert r.status_code == 200, r.text
    assert r.json()["suggestion_unavailable"] == "unknown_correction"


async def test_fit_with_a_run_of_another_printer_is_422(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    read = await _key(["read"])
    pid, other = await _printer(), await _printer()
    _measured, checked = await _types()
    url = f"/api/v1/printers/{pid}/calibration/auto/fit"
    body = {"type": checked, "readings": _check_readings()}
    for run_id in (
        await _run(other, "auto", checked),
        await _run(pid, "tray-2", checked),
        uuid4(),
    ):
        r = await c.post(url, json={**body, "run_id": str(run_id)}, headers=read)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error_code"] == "run_not_for_this_tray"


async def test_apply_suggestion_adds_a_version_admin_only(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin, read, prn = await _key(["admin"]), await _key(["read"]), await _key(["print"])
    pid = await _printer()
    measured, checked = await _types()
    run_id = await _run(pid, "auto", checked)
    url = f"/api/v1/sheet-types/{checked}/versions/from-suggestion"
    body = {"run_id": str(run_id), "pitch_x_mm": 37.55}

    for headers in (read, prn):
        r = await c.post(url, json=body, headers=headers)
        assert r.status_code == 403, r.text

    r = await c.post(url, json=body, headers=admin)
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["version"] == 2
    assert v["geometry"]["pitch_x"] == 37.55
    assert v["geometry"]["margin_left"] == 9.9  # untouched
    assert "#1" in v["change_note"] and "suggestion" in v["change_note"]

    # The run belongs to another type: refused, nothing added.
    r = await c.post(
        f"/api/v1/sheet-types/{measured}/versions/from-suggestion", json=body, headers=admin
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error_code"] == "run_not_for_this_type"

    # The run was printed on version 1, the type is at version 2 now.
    r = await c.post(url, json={**body, "pitch_x_mm": 37.5}, headers=admin)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error_code"] == "run_version_not_current"

    # A calibration page suggests nothing.
    cal = await _run(pid, "auto", checked, kind="calibration_page")
    r = await c.post(url, json={**body, "run_id": str(cal)}, headers=admin)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error_code"] == "run_not_a_check_print"

    # No value at all is no suggestion.
    fresh = await _run(pid, "auto", checked)
    r = await c.post(url, json={"run_id": str(fresh)}, headers=admin)
    assert r.status_code == 422, r.text

    # A margin of 0 is a valid suggestion (the geometry rules allow it), a pitch of 0 is not.
    r = await c.post(url, json={"run_id": str(fresh), "pitch_x_mm": 0}, headers=admin)
    assert r.status_code == 422, r.text
    r = await c.post(url, json={"run_id": str(fresh), "margin_left_mm": 0}, headers=admin)
    assert r.status_code == 201, r.text
    assert r.json()["geometry"]["margin_left"] == 0
