# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""POST /api/v1/print/{printer}/batch: which cut the batch gets between its labels.

The printer's "Half cut by default" (``cut_defaults.half_cut``) decides when the request has no
``half_cut_override``; the override wins when it is set; a backend without a half cutter never
gets one. Before, the route ignored the printer's setting and half-cut every batch on a PT
printer, so a printer with the setting off (the default) still got half cuts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from app.api.error_handlers import register_error_handlers
from app.api.routes import batch as batch_route
from app.auth.dependencies import AuthContext
from app.auth.scope_deps import require_print
from app.db.session import get_session
from app.models.printer import Printer
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def make_client(db_session, monkeypatch):  # type: ignore[no-untyped-def]
    """Build a client for a PT printer with the given default and backend capability.

    ``dispatch_batch`` is replaced by a spy: the test reads the ``half_cut`` the route chose.
    """
    dispatch = AsyncMock(side_effect=lambda **_kw: [uuid4()])
    monkeypatch.setattr(batch_route, "dispatch_batch", dispatch)
    clients: list[AsyncClient] = []

    async def _make(*, default_half_cut: bool, supported: bool) -> tuple[AsyncClient, AsyncMock]:
        printer = Printer(
            name=f"P {len(clients)}",
            slug=f"p{len(clients)}",
            model="PT-P750W",
            backend="ptouch",
            connection={},
            cut_defaults_half_cut=default_half_cut,
        )
        db_session.add(printer)
        await db_session.commit()

        backend = MagicMock()
        backend.half_cut_supported = supported
        router_stub = MagicMock()
        router_stub.get = lambda _slug: backend
        router_stub.service_for = lambda _slug: MagicMock()

        app = FastAPI()
        register_error_handlers(app)
        app.include_router(batch_route.router)
        app.state.backend_router = router_stub
        ctx = AuthContext(source="api-key", scope="admin", api_key_id=uuid4(), ip="127.0.0.1")
        app.dependency_overrides[require_print] = lambda: ctx

        async def _session() -> AsyncIterator[Any]:
            yield db_session

        app.dependency_overrides[get_session] = _session
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
        clients.append(client)
        client.slug = printer.slug  # type: ignore[attr-defined]
        return client, dispatch

    try:
        yield _make
    finally:
        for client in clients:
            await client.aclose()


def _body(override: bool | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "items": [
            {"content_type": "text_one_line", "data": {"primary_id": "A-1"}},
            {"content_type": "text_one_line", "data": {"primary_id": "A-2"}},
        ]
    }
    if override is not None:
        body["half_cut_override"] = override
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("default_half_cut", "supported", "override", "expected"),
    [
        # No override: the printer's "Half cut by default" decides.
        (False, True, None, False),
        (True, True, None, True),
        # A backend without a half cutter never gets one, whatever the default says.
        (True, False, None, False),
        # The override wins over the printer's default ...
        (False, True, True, True),
        (True, True, False, False),
        # ... but not over the backend's capability.
        (False, False, True, False),
    ],
)
async def test_batch_half_cut_follows_printer_default(
    make_client: Any,
    default_half_cut: bool,
    supported: bool,
    override: bool | None,
    expected: bool,
) -> None:
    client, dispatch = await make_client(default_half_cut=default_half_cut, supported=supported)
    response = await client.post(f"/api/v1/print/{client.slug}/batch", json=_body(override))
    assert response.status_code == 202, response.text
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["half_cut"] is expected
