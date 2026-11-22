# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A fresh installation starts with no sheet types (ADR 0028, spec 6.1.4, #335).

Since 1.8.0 the hub inserts nothing on start: types come from the catalog. The other
integration tests run as an upgraded installation (``conftest.py`` seeds the 1.7.x types before
the startup step); the ``fresh_install`` marker turns that off.
"""

from __future__ import annotations

import pytest
from app.catalog.upgrade import MIGRATED_KEY
from app.models.app_setting import AppSetting
from app.models.backing_design import BackingDesign
from app.models.sheet import SheetType
from httpx import AsyncClient
from sqlalchemy import func, select


@pytest.mark.fresh_install
async def test_fresh_install_has_no_types(client: AsyncClient) -> None:
    import app.db.engine as eng

    response = await client.get("/api/v1/sheet-types")  # the first request starts the lifespan
    assert response.status_code == 200
    assert response.json() == []
    async with eng.async_session() as s:
        assert await s.scalar(select(func.count()).select_from(SheetType)) == 0
        assert await s.scalar(select(func.count()).select_from(BackingDesign)) == 0
        # the step ran (nothing to stamp) and does not run again
        assert await s.get(AppSetting, MIGRATED_KEY) is not None
