# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""#405: the calibration PDF download holds no database transaction while it draws.

The page is drawn under the render slots and can take up to ``LABELYARD_RENDER_TIMEOUT_S``. The
read transaction of ``build_payload`` is ended before that, as the other bounded renders do, so
a slow drawing never keeps a SQLite read snapshot (and its WAL growth) open.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app.api.routes import calibration as calibration_routes
from app.calibration import page as page_module
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.api.test_calibration_page_api import _factory, _key, _printer, _type

pytestmark = pytest.mark.asyncio


async def test_calibration_download_releases_the_db_session_before_render(
    api_client_with_seed: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _type(_factory(), "dl-session")
    pid = await _printer(_factory())
    seen: dict[str, Any] = {}
    real_build = page_module.build_payload
    real_render = calibration_routes.render_slots.render_bounded

    async def build(session: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        seen["session"] = session
        payload = await real_build(session, *args, **kwargs)
        seen["in_transaction_after_build"] = session.in_transaction()
        return payload

    async def render(*args: Any, **kwargs: Any) -> Any:
        seen["in_transaction_at_render"] = seen["session"].in_transaction()
        return await real_render(*args, **kwargs)

    monkeypatch.setattr(calibration_routes.page, "build_payload", build)
    monkeypatch.setattr(calibration_routes.render_slots, "render_bounded", render)
    r = await api_client_with_seed.get(
        f"/api/v1/printers/{pid}/calibration/auto/page/pdf?type=dl-session",
        headers=await _key(["read"]),
    )
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"%PDF")
    # The build read the database (the transaction began), the render saw it ended.
    assert seen["in_transaction_after_build"] is True
    assert seen["in_transaction_at_render"] is False
