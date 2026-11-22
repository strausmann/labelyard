# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""An imported ``new_version`` resets ``verified`` (#204 note 5, 1.8.0).

A person checked the geometry of version N against paper. A document from somewhere else adds
version N+1 with other numbers: nobody has checked those, so the type no longer counts as
verified. Importing the same geometry again changes nothing, and so does the flag.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest_asyncio
from app.sheets import exchange
from app.sheets import type_service as ts
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.sheets.test_exchange import G, _changed, _doc_dict, _fields, _raw


@pytest_asyncio.fixture
async def session(async_session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[Any]:
    async with async_session_factory() as s:
        yield s


async def test_import_new_version_resets_verified(session: AsyncSession) -> None:
    await ts.create_type(session, replace(_fields(), verified=True), G, actor="tester")
    doc = exchange.parse_import(_raw(_changed(await _base_doc_of_existing(session))))

    result = await exchange.import_type(session, doc, mode="new_version", actor="importer")

    assert (result.outcome, result.version) == ("version_added", 2)
    assert (await ts.get_type(session, "avery-l4736")).verified is False


async def test_import_with_the_same_geometry_keeps_verified(session: AsyncSession) -> None:
    await ts.create_type(session, replace(_fields(), verified=True), G, actor="tester")
    doc = exchange.parse_import(_raw(await _base_doc_of_existing(session)))

    result = await exchange.import_type(session, doc, mode="new_version", actor="importer")

    assert result.outcome == "unchanged"
    assert (await ts.get_type(session, "avery-l4736")).verified is True


async def test_other_import_modes_leave_the_type_verified(session: AsyncSession) -> None:
    await ts.create_type(session, replace(_fields(), verified=True), G, actor="tester")
    doc = exchange.parse_import(_raw(_changed(await _base_doc_of_existing(session))))

    copy = await exchange.import_type(
        session, doc, mode="as_new_key", new_key="my-l4736", new_prefix="ML4736", actor="importer"
    )

    assert copy.outcome == "created_as_new_key"
    assert (await ts.get_type(session, "avery-l4736")).verified is True
    assert (await ts.get_type(session, "my-l4736")).verified is False


async def _base_doc_of_existing(session: AsyncSession) -> dict[str, Any]:
    return _doc_dict(await exchange.export_type(session, "avery-l4736", None))
