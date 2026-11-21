# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""#405: ``_recheck_id_cells`` releases what never reached the queue, and nothing else.

A stored run (a Grocy label printed as its stored ``queued`` row, ``print_sheets(job_id=...)``)
keeps its cells when its row already is a queued page: that row is printed onto them by the next
recovery (T7-1, R-281-16), as ``_undo_partial_print`` decides for the same row. A row that is
still deferred, or a run without a stored row, is released as before.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import app.models  # noqa: F401  (registers every table)
import pytest
import pytest_asyncio
from app.models.job import Job
from app.sheets import print_service as sps
from app.sheets.errors import IdCellContentMismatchError
from app.sheets.reservation import CellAssignment, ReservedPage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _page(job_id: UUID) -> ReservedPage:
    return ReservedPage(
        job_id=job_id,
        sheet_id=uuid4(),
        sheet_code="S-0001",
        type_version_id=uuid4(),
        assignments=[CellAssignment(cell=1, item_index=None, source="hub", purpose="id", ref=None)],
        id_cell=1,
        margin_id=False,
        hw_margin_cells=[],
    )


_SHEET = SimpleNamespace(geometry=None, allow_qr=True)


def _refuse(*_args: Any, **_kwargs: Any) -> None:
    raise IdCellContentMismatchError(("qr_payload",))


async def _store(factory: async_sessionmaker[AsyncSession], job_id: UUID, payload: Any) -> None:
    async with factory() as session:
        session.add(Job(id=job_id, printer_id=uuid4(), state="queued", payload=payload))
        await session.commit()


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch) -> list[UUID]:
    freed: list[UUID] = []

    async def release_all(_factory: Any, job_ids: Any, _bus: Any) -> None:
        freed.extend(job_ids)

    monkeypatch.setattr(sps, "_release_all", release_all)
    monkeypatch.setattr(sps, "check_id_cell_fits", _refuse)
    return freed


async def _recheck(factory: Any, pages: list[ReservedPage], sheets: list[Any], **kw: Any) -> None:
    await sps._recheck_id_cells(
        factory, pages, sheets, scan=lambda code: code, render_dpi=600, bus=None, **kw
    )


async def test_recheck_keeps_reservations_of_a_stored_retry(
    factory: async_sessionmaker[AsyncSession], released: list[UUID]
) -> None:
    job_id = uuid4()
    await _store(factory, job_id, {"kind": "sheet_page", "detail": {}})
    with pytest.raises(IdCellContentMismatchError):
        await _recheck(factory, [_page(job_id)], [_SHEET], stored=True)
    assert released == []


async def test_recheck_releases_a_stored_row_that_is_still_deferred(
    factory: async_sessionmaker[AsyncSession], released: list[UUID]
) -> None:
    job_id = uuid4()
    await _store(factory, job_id, {"deferred": True, "deferred_kind": "sheet_page"})
    with pytest.raises(IdCellContentMismatchError):
        await _recheck(factory, [_page(job_id)], [_SHEET], stored=True)
    assert released == [job_id]


async def test_recheck_releases_a_run_without_a_stored_row(
    factory: async_sessionmaker[AsyncSession], released: list[UUID]
) -> None:
    job_id = uuid4()
    await _store(factory, job_id, {"kind": "sheet_page", "detail": {}})
    with pytest.raises(IdCellContentMismatchError):
        await _recheck(factory, [_page(job_id)], [_SHEET])
    assert released == [job_id]


async def test_recheck_length_mismatch_raises(
    factory: async_sessionmaker[AsyncSession],
    released: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(guard) One sheet view per reserved page: a missing one is a bug, not a silent skip."""
    monkeypatch.setattr(sps, "check_id_cell_fits", lambda *_a, **_k: None)
    job_id = uuid4()
    with pytest.raises(ValueError, match="zip"):
        await _recheck(factory, [_page(job_id), _page(uuid4())], [_SHEET])
    assert job_id in released
