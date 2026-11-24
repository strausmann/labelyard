# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Jobs printed with a calibration history entry (#336, design 7.5, S7).

The entry's id travels in ``PagePayload.calibration_history_id`` (never in the
``CalibrationSnapshot``, which 1.7.1 reads with ``extra="forbid"``). Jobs from before 1.8.0 have no
id and are matched by their snapshot values inside the entry's time window: from the entry to the
next entry of the tray.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import app.models  # noqa: F401
import bcrypt
import pytest
from app.calibration import page as calibration_page
from app.calibration import service
from app.models.api_key import ApiKey
from app.models.calibration_run import CalibrationHistory
from app.models.job import Job
from app.models.printer import Printer
from app.models.printer_tray import PrinterTray
from app.sheets import type_service as ts
from app.sheets.geometry import Geometry
from sqlalchemy import select
from sqlmodel import col

pytestmark = pytest.mark.asyncio

G48 = Geometry("A4", 21000, 29700, 4570, 2120, 4, 12, 4820, 2120, 2130, 985, None, "rectangle")
FIRST = {"offset_x_mm": 1.2, "offset_y_mm": -0.4, "scale_x": 1.001, "scale_y": 0.999,
         "rotation_deg": 0.3}  # fmt: skip
SECOND = {"offset_x_mm": -0.41, "offset_y_mm": 0.66, "scale_x": 1.0, "scale_y": 1.0,
          "rotation_deg": 0.0}  # fmt: skip
SNAP_FIRST = {"method": "manual", "dx_hmm": 120, "dy_hmm": -40, "sx_ppm": 1_001_000,
              "sy_ppm": 999_000, "rotation_udeg": 300_000}  # fmt: skip
SNAP_SECOND = {"method": "manual", "dx_hmm": -41, "dy_hmm": 66, "sx_ppm": 1_000_000,
               "sy_ppm": 1_000_000, "rotation_udeg": 0}  # fmt: skip
T0 = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)


def _factory():  # type: ignore[no-untyped-def]
    import app.db.engine as _engine_module

    return _engine_module.async_session


async def _key(scopes: list[str], allowed: list[str] | None = None) -> dict[str, str]:
    plaintext = f"lh_pat_{uuid4().hex}"
    hashed = bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt(rounds=4)).decode()
    async with _factory()() as s:
        s.add(
            ApiKey(
                name=f"jcf-{uuid4().hex[:6]}",
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
        await s.commit()
        return p.id


async def _two_entries(c: Any, pid: UUID, admin: dict[str, str]) -> tuple[UUID, UUID]:
    """Two saves, dated T0 and T0 + 1 day, so the windows are known."""
    base = f"/api/v1/printers/{pid}/calibration/auto"
    assert (await c.put(base, json=FIRST, headers=admin)).status_code == 200
    assert (await c.put(base, json=SECOND, headers=admin)).status_code == 200
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
        entries[0].created_at = T0
        entries[1].created_at = T0 + timedelta(days=1)
        ids = (entries[0].id, entries[1].id)
        await s.commit()
    return ids


async def _job(
    pid: UUID,
    *,
    created_at: datetime,
    snapshot: dict[str, Any] | None,
    history_id: UUID | None = None,
    tray: str = "auto",
) -> UUID:
    payload: dict[str, Any] = {
        "kind": "sheet_page",
        "media": "A4",
        "tray": tray,
        "render_dpi": 600,
        "job_name": "Sheet",
        "calibration": snapshot,
        "detail": {},
    }
    if history_id is not None:
        payload["calibration_history_id"] = str(history_id)
    async with _factory()() as s:
        job = Job(printer_id=pid, state="done", payload=payload, created_at=created_at)
        s.add(job)
        await s.commit()
        return job.id


async def _listed(c: Any, history_id: UUID, headers: dict[str, str]) -> set[str]:
    r = await c.get(f"/api/v1/jobs?calibration={history_id}&limit=200", headers=headers)
    assert r.status_code == 200, r.text
    return {j["id"] for j in r.json()}


async def test_jobs_filtered_by_history_id(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    first, second = await _two_entries(c, pid, admin)
    late = T0 + timedelta(days=3)  # outside the first window: only the id can match it
    a = await _job(pid, created_at=late, snapshot=SNAP_FIRST, history_id=first)
    b = await _job(pid, created_at=late, snapshot=SNAP_SECOND, history_id=second)
    assert await _listed(c, first, admin) == {str(a)}
    assert await _listed(c, second, admin) == {str(b)}
    assert await _listed(c, uuid4(), admin) == set()
    # The filter adds to the others and to the printer restriction.
    assert await _listed(c, first, await _key(["read"], allowed=[str(await _printer())])) == set()
    r = await c.get(f"/api/v1/jobs?calibration={first}&state=failed", headers=admin)
    assert r.json() == []
    # The history counts the same jobs.
    hist = await c.get(f"/api/v1/printers/{pid}/calibration/auto/history", headers=admin)
    assert [e["jobs_count"] for e in hist.json()] == [1, 1]


async def test_pre_1_8_jobs_matched_by_values_and_window(api_client_with_seed: Any) -> None:
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    first, second = await _two_entries(c, pid, admin)
    inside = await _job(pid, created_at=T0 + timedelta(hours=2), snapshot=SNAP_FIRST)
    before = await _job(pid, created_at=T0 - timedelta(hours=2), snapshot=SNAP_FIRST)
    after = await _job(pid, created_at=T0 + timedelta(days=2), snapshot=SNAP_FIRST)
    other_values = await _job(pid, created_at=T0 + timedelta(hours=3), snapshot=SNAP_SECOND)
    other_tray = await _job(
        pid, created_at=T0 + timedelta(hours=4), snapshot=SNAP_FIRST, tray="tray-2"
    )
    uncorrected = await _job(pid, created_at=T0 + timedelta(hours=5), snapshot=None)
    # A job with an id belongs to its id only, even with equal values inside the window.
    named = await _job(
        pid, created_at=T0 + timedelta(hours=6), snapshot=SNAP_FIRST, history_id=second
    )
    newest_window = await _job(pid, created_at=T0 + timedelta(days=5), snapshot=SNAP_SECOND)
    assert await _listed(c, first, admin) == {str(inside)}
    assert await _listed(c, second, admin) == {str(named), str(newest_window)}
    assert str(before) not in await _listed(c, second, admin)
    del after, other_values, other_tray, uncorrected
    hist = await c.get(f"/api/v1/printers/{pid}/calibration/auto/history", headers=admin)
    assert [e["jobs_count"] for e in hist.json()] == [2, 1]


async def test_a_corrected_page_names_the_newest_history_entry(api_client_with_seed: Any) -> None:
    """The code that copies ``snapshot()`` into a payload also copies the entry's id."""
    c = api_client_with_seed
    admin = await _key(["admin"])
    pid = await _printer()
    async with _factory()() as s:
        await ts.create_type(
            s, ts.TypeFields("jcf-test", "JCF", "Acme", [], None, None), G48, actor="t"
        )
    async with _factory()() as s:
        plain = await calibration_page.build_payload(
            s, pid, "auto", type_key="jcf-test", version=None, corrected=True
        )
    assert plain.calibration_history_id is None  # no entry yet
    first, second = await _two_entries(c, pid, admin)
    async with _factory()() as s:
        corrected = await calibration_page.build_payload(
            s, pid, "auto", type_key="jcf-test", version=None, corrected=True
        )
        measuring = await calibration_page.build_payload(
            s, pid, "auto", type_key="jcf-test", version=None, corrected=False
        )
        snap, entry = await service.snapshot_with_history(s, pid, "auto")
    assert corrected.calibration_history_id == second == entry
    assert snap.dx_hmm == -41
    assert measuring.calibration is None and measuring.calibration_history_id is None
    del first
