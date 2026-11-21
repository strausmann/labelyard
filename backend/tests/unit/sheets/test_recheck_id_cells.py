# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""#405: ``_recheck_id_cells`` checks the ID cell of every reserved page, releases on a refusal.

``zip(strict=True)``: one sheet view per reserved page, a missing view is a bug, never a skipped
check. A stored run (a Grocy label) is released like any other: at this point its row is still the
deferred one (``grocy_sheet.py``; the payload swap comes later in ``_queue_pages``), so a
"keep the cells of a stored row" branch could never fire (fix round 1, Q3).
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


async def _recheck(factory: Any, pages: list[ReservedPage], sheets: list[Any]) -> None:
    await sps._recheck_id_cells(
        factory, pages, sheets, scan=lambda code: code, render_dpi=600, bus=None
    )


async def test_recheck_releases_every_page_when_the_id_label_does_not_fit(
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
