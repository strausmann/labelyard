# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The run counter under concurrent submits (#336, ADR 0029), on the production engine settings.

``production_engine`` (Task 6a) gives the pragmas (WAL, ``busy_timeout``, foreign keys), the
``SERIALIZABLE`` isolation and the statement counter the sheet write lock checks; every allocation
gets its own session, as concurrent requests do.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import app.db.engine as engine_module
import app.models  # noqa: F401
import pytest
from app.calibration import runs
from app.models.calibration_run import CalibrationRunCounter
from app.models.printer import Printer
from app.models.printer_tray import PrinterTray

pytestmark = pytest.mark.asyncio


async def _printer() -> UUID:
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
        return p.id


async def _allocate(printer_id: UUID) -> int:
    async with engine_module.async_session() as s:
        run = await runs.allocate_run(
            s,
            printer_id=printer_id,
            tray="tray-1",
            kind="calibration_page",
            job_id=uuid4(),
            type_version_id=uuid4(),
            correction=None,
            actor="user:admin",
        )
        return run.run_no


async def test_concurrent_submits_get_distinct_numbers(production_engine) -> None:  # type: ignore[no-untyped-def]
    del production_engine
    pid = await _printer()
    numbers = await asyncio.gather(*(_allocate(pid) for _ in range(20)))
    assert sorted(numbers) == list(range(1, 21))
    async with engine_module.async_session() as s:
        counter = await s.get(CalibrationRunCounter, (pid, "tray-1"))
    assert counter is not None and counter.next_run == 21
