# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Check sheets over HTTP (#240, design 7.2, ADR 0029): one sheet of labels, up to three passes.

The queue and the fake IPP printer come from ``test_calibration_runs_api`` (``env``): a check page
really goes out, and the run, the pass and its payload are read back.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import app.db.engine as engine_module
import app.models  # noqa: F401
import httpx
import pytest
from app.calibration import runs
from app.calibration.check_sheet import check_passes
from app.calibration.page import payload_layout, render_calibration_page
from app.models.calibration_run import (
    CalibrationCheckSheet,
    CalibrationCheckSheetPass,
    CalibrationRun,
)
from app.models.job import Job
from app.services.page_jobs import CalibrationSnapshot, PagePayload
from app.sheets import type_service as ts
from app.sheets.errors import CheckSheetFullError
from sqlalchemy import func, select
from sqlmodel import col

from tests.helpers import pdf_inspect as pi
from tests.integration.api import test_calibration_runs_api as base
from tests.integration.api.test_calibration_runs_api import (
    G6121,
    _Env,
    _factory,
    _key,
    _printer,
    _type,
)

pytestmark = pytest.mark.asyncio

env = base.env  # the queue and the fake printer of the run tests (a fixture)

CORRECTION = {"offset_x_mm": 0.4, "offset_y_mm": -0.6}


async def _setup(env: _Env, key: str = "chk-6121") -> None:
    async with env.factory() as s:
        await ts.create_type(
            s, ts.TypeFields(key, key.replace("-", "").upper()[:8], "Acme", [], None, None),
            G6121, actor="t",
        )  # fmt: skip
    async with env.client() as c:
        r = await c.put(
            f"/api/v1/printers/{env.printer_id}/calibration/tray-1",
            json={**CORRECTION, "measured_on": {"type_key": key, "version": 1}},
        )
    assert r.status_code == 200, r.text


async def _check(env: _Env, sheet: str, key: str = "chk-6121") -> httpx.Response:
    async with env.client() as c:
        return await c.post(
            f"/api/v1/printers/{env.printer_id}/calibration/tray-1/page",
            json={"type": key, "corrected": True, "check_sheet": sheet},
        )


async def _printed(env: _Env, sheet: str, key: str = "chk-6121") -> str:
    r = await _check(env, sheet, key)
    assert r.status_code == 202, r.text
    job_id: str = r.json()["job_id"]
    async with asyncio.timeout(10):
        await env.queue.wait_for_job(job_id, timeout_s=10)
    return job_id


async def _open(env: _Env, key: str = "chk-6121") -> Any:
    async with env.client() as c:
        r = await c.get(
            f"/api/v1/printers/{env.printer_id}/calibration/tray-1/check-sheets/open",
            params={"type": key},
        )
    assert r.status_code == 200, r.text
    return r.json()


async def _payload(env: _Env, job_id: str) -> PagePayload:
    async with env.factory() as s:
        job = await s.get(Job, UUID(job_id))
    assert job is not None
    return PagePayload.model_validate(job.payload)


async def test_new_sheet_starts_pass_1(env: _Env) -> None:
    await _setup(env)
    assert await _open(env) is None
    job_id = await _printed(env, "new")
    run = await env.run_row(job_id)
    assert (run.kind, run.pass_no) == ("check_page", 1) and run.check_sheet_id is not None
    payload = await _payload(env, job_id)
    assert payload.detail["pass_no"] == 1 and payload.detail["passes_total"] == 3
    assert payload.detail["pass_crossings"] == [list(c) for c in check_passes(5, 13)[0]]
    opened = await _open(env)
    assert opened == {
        "check_sheet_id": str(run.check_sheet_id),
        "passes_total": 3,
        "passes_used": 1,
        "next_pass": 2,
        "run_line_slots": 0,
        "passes": [{"pass_no": 1, "run_no": run.run_no, "run_id": str(run.id)}],
    }
    # Pass 1 prints the full page: its run's page codes are stored, where pass 1 draws them.
    assert run.page_codes is not None and len(run.page_codes) == 4
    assert run.page_codes == payload_layout(payload).page_code_records()
    later = [list(c) for p in check_passes(5, 13)[1:] for c in p]
    assert payload.detail["reserved_crossings"] == later


async def test_next_pass_on_the_same_sheet_links_the_run(env: _Env) -> None:
    await _setup(env)
    first = await env.run_row(await _printed(env, "new"))
    second_id = await _printed(env, "next")
    second = await env.run_row(second_id)
    assert second.check_sheet_id == first.check_sheet_id and second.pass_no == 2
    assert second.run_no == first.run_no + 1
    assert second.page_codes == []  # pass 2 prints no codes
    async with env.factory() as s:
        rows = (
            (
                await s.execute(
                    select(CalibrationCheckSheetPass).where(
                        col(CalibrationCheckSheetPass.check_sheet_id) == first.check_sheet_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {(p.pass_no, p.run_id) for p in rows} == {(1, first.id), (2, second.id)}
    listed = {r["run_no"]: r for r in await env.runs()}
    assert listed[second.run_no]["pass_no"] == 2
    assert listed[second.run_no]["check_sheet_id"] == str(first.check_sheet_id)


async def test_full_check_sheet_is_409(env: _Env) -> None:
    await _setup(env)
    await _printed(env, "new")
    await _printed(env, "next")
    third = await env.run_row(await _printed(env, "next"))
    assert third.pass_no == 3
    async with env.factory() as s:
        sheet = await s.get(CalibrationCheckSheet, third.check_sheet_id)
    assert sheet is not None and sheet.closed_at is not None  # all passes used
    assert await _open(env) is None
    before = [r["run_no"] for r in await env.runs()]
    r = await _check(env, "next")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error_code"] == "check_sheet_full"
    assert [x["run_no"] for x in await env.runs()] == before  # no run taken
    # A new sheet starts again at pass 1.
    fresh = await env.run_row(await _printed(env, "new"))
    assert fresh.pass_no == 1 and fresh.check_sheet_id != third.check_sheet_id


async def test_next_without_an_open_sheet_is_409(env: _Env) -> None:
    await _setup(env)
    r = await _check(env, "next")
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "check_sheet_full"


async def test_check_sheet_needs_the_corrected_page(env: _Env) -> None:
    await _setup(env)
    async with env.client() as c:
        r = await c.post(
            f"/api/v1/printers/{env.printer_id}/calibration/tray-1/page",
            json={"type": "chk-6121", "check_sheet": "new"},
        )
    assert r.status_code == 422, r.text


async def test_a_check_page_without_a_sheet_prints_the_full_page_as_before(env: _Env) -> None:
    await _setup(env)
    job_id = await env.post_page(type="chk-6121", corrected=True)
    run = await env.run_row(job_id)
    assert run.check_sheet_id is None and run.pass_no is None
    payload = await _payload(env, job_id)
    assert "pass_no" not in payload.detail
    texts = pi.texts(render_calibration_page(payload, compress=False))
    assert "S1" in texts and "K1" not in texts


async def test_line_less_passes_are_recorded(env: _Env) -> None:
    """The 6121 on a printer without reported margins (5 mm) has no room for run-line slots:
    passes 2 and 3 print their crosses only and are still recorded with their runs."""
    await _setup(env)
    await _printed(env, "new")
    for pass_no in (2, 3):
        job_id = await _printed(env, "next")
        run = await env.run_row(job_id)
        assert run.pass_no == pass_no
        texts = pi.texts(render_calibration_page(await _payload(env, job_id), compress=False))
        assert set(texts) == {f"K{pass_no}"}
    async with env.factory() as s:
        assert (
            await s.execute(select(func.count()).select_from(CalibrationCheckSheetPass))
        ).scalar_one() == 3


async def test_a_grid_without_passes_takes_a_one_pass_sheet(env: _Env) -> None:
    # 2 x 3 labels: two crossings, no passes; the check print is the full page, as before.
    from app.sheets.geometry import Geometry

    small = Geometry("A4", 21000, 29700, 9000, 9000, 2, 3, 9500, 9500, 1000, 1000, None,
                     "rectangle")  # fmt: skip
    async with env.factory() as s:
        await ts.create_type(
            s, ts.TypeFields("chk-small", "CHKSMALL", "Acme", [], None, None), small, actor="t"
        )
    await _setup(env, "chk-6121")
    run = await env.run_row(await _printed(env, "new", "chk-small"))
    assert run.pass_no == 1
    payload = await _payload(env, str(run.job_id))
    assert "pass_crossings" not in payload.detail
    assert await _open(env, "chk-small") is None  # closed: one pass, used
    r = await _check(env, "next", "chk-small")
    assert r.status_code == 409


async def test_failed_submit_frees_the_pass(env: _Env) -> None:
    await _setup(env)
    first = await env.run_row(await _printed(env, "new"))
    real = env.queue.submit_page_job

    async def broken(*args: Any, **kwargs: Any) -> UUID:
        del args, kwargs
        raise RuntimeError("queue broke")

    env.queue.submit_page_job = broken  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            await _check(env, "next")
    finally:
        env.queue.submit_page_job = real  # type: ignore[method-assign]
    opened = await _open(env)
    assert opened["passes_used"] == 1 and opened["next_pass"] == 2
    again = await env.run_row(await _printed(env, "next"))
    assert again.pass_no == 2 and again.check_sheet_id == first.check_sheet_id
    assert again.run_no == first.run_no + 2  # the failed run left a gap


async def test_a_failed_first_pass_removes_its_new_sheet(env: _Env) -> None:
    await _setup(env)
    real = env.queue.submit_page_job

    async def broken(*args: Any, **kwargs: Any) -> UUID:
        del args, kwargs
        raise RuntimeError("queue broke")

    env.queue.submit_page_job = broken  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            await _check(env, "new")
    finally:
        env.queue.submit_page_job = real  # type: ignore[method-assign]
    async with env.factory() as s:
        assert (
            await s.execute(select(func.count()).select_from(CalibrationCheckSheet))
        ).scalar_one() == 0
    assert await _open(env) is None


async def test_open_check_sheet_restricted_key_other_printer_403(
    api_client_with_seed: httpx.AsyncClient,
) -> None:
    c = api_client_with_seed
    pid, other = await _printer(_factory()), await _printer(_factory())
    await _type(_factory(), f"chk-{uuid4().hex[:6]}")
    bound = await _key(["read"], allowed=[str(other)])
    path = "/api/v1/printers/{}/calibration/tray-1/check-sheets/open?type=cal-x"
    r = await c.get(path.format(pid), headers=bound)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error_code"] == "printer_not_allowed"
    own = await c.get(path.format(other), headers=bound)
    assert own.status_code == 404  # past the restriction: the type is unknown
    assert (await c.get(path.format(other))).status_code == 401


# ---------------------------------------------------------------------------- concurrency


async def _sheet_and_printer() -> tuple[UUID, UUID]:
    from app.models.printer import Printer
    from app.models.printer_tray import PrinterTray

    async with engine_module.async_session() as s:
        p = Printer(
            id=uuid4(),
            name=f"Office {uuid4().hex[:4]}",
            slug=f"p-{uuid4().hex[:6]}",
            model="m",
            backend="ipp",
            connection={"uri": "ipp://printer.example.invalid/ipp/print"},
        )
        s.add(p)
        s.add(PrinterTray(printer_id=p.id, tray_key="tray-1", label="Tray 1", is_default=True))
        await s.commit()
    return p.id, uuid4()


PASSES = check_passes(5, 13)


async def _take(printer_id: UUID, version_id: UUID, mode: str) -> CalibrationRun | str:
    async with engine_module.async_session() as s:
        try:
            return await runs.allocate_run(
                s,
                printer_id=printer_id,
                tray="tray-1",
                kind="check_page",
                job_id=uuid4(),
                type_version_id=version_id,
                correction=CalibrationSnapshot(method="manual").model_dump(),
                actor="user:admin",
                check_sheet=runs.CheckSheetChoice(mode, version_id, PASSES),  # type: ignore[arg-type]
            )
        except CheckSheetFullError as e:
            return e.error_code


async def test_concurrent_next_pass_gives_two_passes_or_one_409(production_engine) -> None:  # type: ignore[no-untyped-def]
    del production_engine
    pid, version = await _sheet_and_printer()
    first = await _take(pid, version, "new")
    assert isinstance(first, CalibrationRun) and first.pass_no == 1
    # Four requests race for the two free passes: two get one each, the others are refused.
    results = await asyncio.gather(*(_take(pid, version, "next") for _ in range(4)))
    taken = [r for r in results if isinstance(r, CalibrationRun)]
    refused = [r for r in results if not isinstance(r, CalibrationRun)]
    assert refused == ["check_sheet_full"] * 2, refused
    assert sorted(r.pass_no or 0 for r in taken) == [2, 3]  # never a shared pass
    assert {r.check_sheet_id for r in taken} == {first.check_sheet_id}
    async with engine_module.async_session() as s:
        rows = (await s.execute(select(CalibrationCheckSheetPass))).scalars().all()
        sheet = await s.get(CalibrationCheckSheet, first.check_sheet_id)
    assert sorted(p.pass_no for p in rows) == [1, 2, 3]
    assert sheet is not None and sheet.closed_at is not None


async def test_a_failed_last_pass_opens_the_sheet_again(env: _Env) -> None:
    await _setup(env)
    first = await env.run_row(await _printed(env, "new"))
    await _printed(env, "next")
    real = env.queue.submit_page_job

    async def broken(*args: Any, **kwargs: Any) -> UUID:
        del args, kwargs
        raise RuntimeError("queue broke")

    env.queue.submit_page_job = broken  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            await _check(env, "next")  # pass 3 closed the sheet, then its submit failed
    finally:
        env.queue.submit_page_job = real  # type: ignore[method-assign]
    opened = await _open(env)
    assert opened is not None and opened["next_pass"] == 3
    third = await env.run_row(await _printed(env, "next"))
    assert third.pass_no == 3 and third.check_sheet_id == first.check_sheet_id


async def test_a_pruned_pass_closes_its_sheet(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex on #449: retention drops the run of pass 1 and its pass row; the pass must not look
    free again (pass 1 would print the full page over the earlier ink): the sheet is closed."""
    await _setup(env)
    monkeypatch.setattr(runs, "RUN_RETENTION", 2)
    await env.run_row(await _printed(env, "new"))
    await env.post_page(type="chk-6121")
    await env.post_page(type="chk-6121")  # pass 1's run falls out of the newest two
    assert await _open(env) is None
    r = await _check(env, "next")
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "check_sheet_full"


async def test_a_later_run_line_lands_in_pass_1s_slot_after_the_correction_moved(
    env: _Env,
) -> None:
    """Codex on #449: the correction changes by 2 mm between pass 1 and pass 2 (L4736, 5 mm
    margins). Pass 2's run line lands on paper where pass 1 reserved its slot, never on pass 1's
    printed margin text."""
    from app.page_render.transform import correction_mm
    from app.services.page_jobs import snapshot_to_deviation

    async with env.client() as c:
        r = await c.put(
            f"/api/v1/printers/{env.printer_id}/calibration/tray-1",
            json={"offset_x_mm": 0, "offset_y_mm": 2.0},
        )
    assert r.status_code == 200, r.text
    first = await _payload(env, await _printed(env, "new", "cal-run"))
    async with env.client() as c:
        r = await c.put(
            f"/api/v1/printers/{env.printer_id}/calibration/tray-1",
            json={"offset_x_mm": 0, "offset_y_mm": 0},
        )
    assert r.status_code == 200, r.text
    opened = await _open(env, "cal-run")
    assert opened["run_line_slots"] == 1 and opened["next_pass"] == 2
    second = await _payload(env, await _printed(env, "next", "cal-run"))
    layout1, layout2 = payload_layout(first), payload_layout(second)
    assert layout1.run_line_slots and layout2.run_line is not None

    def on_paper(payload: PagePayload, r: tuple[float, float, float, float]) -> tuple[float, ...]:
        c = correction_mm(snapshot_to_deviation(payload.calibration), payload.media)
        pts = [c.apply(p) for p in ((r[0], r[1]), (r[2], r[1]), (r[0], r[3]), (r[2], r[3]))]
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts),
                max(p[1] for p in pts))  # fmt: skip

    slot = on_paper(first, layout1.run_line_slots[0].rect_mm())
    line = on_paper(second, layout2.run_line.rect_mm())
    assert all(abs(a - b) < 0.02 for a, b in zip(slot, line, strict=True)), (slot, line)
    for block in layout1.margin_blocks:
        b = on_paper(first, block.box.rect_mm())
        assert not (line[0] < b[2] and b[0] < line[2] and line[1] < b[3] and b[1] < line[3])
    texts = pi.texts(render_calibration_page(second, compress=False))
    assert any(t.startswith("#") and "· K2 ·" in t for t in texts)


# ---------------------------------------------------------------------------- fix round 1


async def _sheet_of(run: CalibrationRun) -> CalibrationCheckSheet | None:
    async with engine_module.async_session() as s:
        return await s.get(CalibrationCheckSheet, run.check_sheet_id)


async def _prune(printer_id: UUID, keep: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retention keeping the newest ``keep`` runs; every run counts as settled (window 0 days)."""
    monkeypatch.setattr(runs, "RUN_RETENTION", keep)
    async with engine_module.async_session() as s:
        await runs.prune_runs(s, printer_id, "tray-1", retention_days=0)


async def _discard(run: CalibrationRun) -> None:
    async with engine_module.async_session() as s:
        await runs._discard_unsubmitted(s, run)


async def test_a_sheet_retention_closed_stays_closed_when_a_later_pass_fails(
    production_engine, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """Review on #449: passes 2 and 3 are taken, retention drops pass 1 and closes the sheet,
    then pass 3's submit fails. The sheet must stay closed: reopened, `next` would print pass 1
    (the full page) over the earlier ink."""
    del production_engine
    pid, version = await _sheet_and_printer()
    first = await _take(pid, version, "new")
    second = await _take(pid, version, "next")
    third = await _take(pid, version, "next")
    assert all(isinstance(r, CalibrationRun) for r in (first, second, third))
    await _prune(pid, 2, monkeypatch)  # keeps passes 2 and 3, drops pass 1
    sheet = await _sheet_of(first)  # type: ignore[arg-type]
    assert sheet is not None and sheet.closed_at is not None
    await _discard(third)  # type: ignore[arg-type]
    sheet = await _sheet_of(first)  # type: ignore[arg-type]
    assert sheet is not None and sheet.closed_at is not None
    assert await _take(pid, version, "next") == "check_sheet_full"


async def test_a_failed_first_pass_racing_a_next_never_frees_pass_1(production_engine) -> None:  # type: ignore[no-untyped-def]
    """Review on #449: `next` took pass 2 while pass 1 of the new sheet was still in flight, then
    pass 1's submit failed. Pass 1 must not become free: the paper may carry pass 2 already."""
    del production_engine
    pid, version = await _sheet_and_printer()
    first = await _take(pid, version, "new")
    second = await _take(pid, version, "next")
    assert isinstance(first, CalibrationRun) and isinstance(second, CalibrationRun)
    await _discard(first)
    taken = await _take(pid, version, "next")
    assert not (isinstance(taken, CalibrationRun) and taken.pass_no == 1), taken


async def test_retention_deletes_a_sheet_it_emptied_and_closes_one_it_did_not(
    production_engine, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """Review on #449: a sheet whose every pass retention dropped has nothing left to show or to
    print on; its row goes. A sheet that keeps a pass stays, closed."""
    del production_engine
    pid, version = await _sheet_and_printer()
    old = await _take(pid, version, "new")  # sheet A: pass 1 only
    kept = await _take(pid, version, "new")  # sheet B: pass 1 and 2
    later = await _take(pid, version, "next")
    assert all(isinstance(r, CalibrationRun) for r in (old, kept, later))
    await _prune(pid, 2, monkeypatch)  # drops sheet A's only run
    assert await _sheet_of(old) is None  # type: ignore[arg-type]
    await _prune(pid, 1, monkeypatch)  # drops sheet B's pass 1, keeps its pass 2
    b = await _sheet_of(kept)  # type: ignore[arg-type]
    assert b is not None and b.closed_at is not None


async def test_the_layout_summary_describes_the_page_that_would_print(env: _Env) -> None:
    """Review on #449: without `check_sheet` a check page is the single full page (one pass);
    with it, pass 1 of a sheet. The summary describes exactly that page."""
    await _setup(env)
    base = f"/api/v1/printers/{env.printer_id}/calibration/tray-1/page/layout"
    async with env.client() as c:
        single = (await c.get(base, params={"type": "chk-6121", "kind": "check_page"})).json()
        sheet = (
            await c.get(
                base, params={"type": "chk-6121", "kind": "check_page", "check_sheet": "new"}
            )
        ).json()
    full = payload_layout(await _payload(env, await env.post_page(type="chk-6121", corrected=True)))
    first = payload_layout(await _payload(env, await _printed(env, "new")))

    def summary(layout: Any, passes: int) -> dict[str, Any]:
        return {
            "page_codes": len(layout.page_codes),
            "run_line_slots": len(layout.run_line_slots),
            "passes_total": passes,
            "margin_text": dict(layout.margin_text),
        }

    assert single == summary(full, 1)
    assert sheet == summary(first, 3)
