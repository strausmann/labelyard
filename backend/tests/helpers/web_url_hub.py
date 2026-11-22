# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A running hub for the web address tests (#247, ADR 0033).

The app and its lifespan run against a temp database with one tape printer (a recording
``MockPrinterBackend``); the three lookup integrations are the real plugins, their upstreams are
answered by ``respx``. Addresses, names and keys are invented. The internal base addresses are
configured through the environment, as an operator would.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import app.models  # noqa: F401
import bcrypt
import httpx
import pytest
import respx
from app.config import get_settings
from app.models.api_key import ApiKey
from app.models.printer import Printer
from app.printer_backends.mock_backend import MockPrinterBackend
from app.services.backend_router import BackendRouter
from app.services.rate_limiter import _rate_limiter
from httpx import ASGITransport, AsyncClient

SNIPE = "http://snipe.internal.example.invalid"
GROCY = "http://grocy.internal.example.invalid:9283"
SPOOL = "http://spool.internal.example.invalid:7912"
INTERNAL = {"snipeit": SNIPE, "grocy": GROCY, "spoolman": SPOOL}
WEB = {
    "snipeit": "https://inventory.example.invalid/snipe",
    "grocy": "https://pantry.example.invalid",
    "spoolman": "https://spools.example.invalid/sm/",
}
ITEM = {"snipeit": "A-1", "grocy": "7", "spoolman": "3"}
PATH = {"snipeit": "/hardware/42", "grocy": "/product/7", "spoolman": "/spool/show/3"}


def _upstream(router: respx.MockRouter) -> None:
    router.get(f"{SNIPE}/api/v1/hardware/bytag/A-1").respond(
        200, json={"id": 42, "name": "Laptop Nine", "asset_tag": "A-1"}
    )
    router.get(f"{SNIPE}/api/v1/hardware/bytag/MISSING").respond(404, json={})
    # The error text names the internal address: it must reach no answer (CWE-209).
    router.get(f"{SNIPE}/api/v1/hardware/bytag/SLOW").mock(
        side_effect=httpx.ReadTimeout(f"timed out reading {SNIPE}/api/v1/hardware/bytag/SLOW")
    )
    router.get(f"{GROCY}/api/objects/products/7").respond(200, json={"id": 7, "name": "Oat Flakes"})
    router.get(f"{GROCY}/api/batteries/4").respond(
        200, json={"battery": {"name": "Smoke Alarm"}, "charge_cycles_count": 2}
    )
    router.get(f"{GROCY}/api/chores/7").respond(200, json={"chore": {"name": "Water Plants"}})
    router.get(f"{SPOOL}/api/v1/spool/3").respond(
        200, json={"id": 3, "filament": {"name": "PLA", "vendor": {"name": "Acme"}}}
    )


@dataclass
class Keys:
    admin: str
    print: str
    read: str
    read_id: UUID
    print_id: UUID


@dataclass
class Hub:
    monkeypatch: pytest.MonkeyPatch
    backend: MockPrinterBackend
    printer_id: UUID

    def web(self, *apps: str) -> None:
        for name in apps:
            self.monkeypatch.setenv(f"LABELYARD_{name.upper()}_WEB_URL", WEB[name])
        get_settings.cache_clear()

    @asynccontextmanager
    async def running(self) -> AsyncIterator[tuple[AsyncClient, Keys]]:
        from app.integrations import (  # type: ignore[attr-defined]
            IntegrationRegistry,
            _discover_plugins,
        )
        from app.main import create_app

        if not IntegrationRegistry.names():
            _discover_plugins()
        app = create_app()
        inner = app._app
        keys = await _keys()
        with respx.mock(assert_all_called=False) as router:
            _upstream(router)
            async with (
                inner.router.lifespan_context(inner),
                AsyncClient(transport=ASGITransport(app=inner), base_url="http://t") as client,
            ):
                yield client, keys


async def _key(scopes: list[str]) -> tuple[str, UUID]:
    import app.db.engine as eng

    plaintext = f"lh_pat_{uuid4().hex}"
    hashed = bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt(rounds=4)).decode()
    row = ApiKey(
        name=f"web-url-{uuid4().hex[:6]}",
        key_hash=hashed,
        key_prefix=plaintext[:16],
        scopes=scopes,
        allowed_printer_ids=[],
        enabled=True,
        rate_limit_per_minute=600,
    )
    async with eng.async_session() as s:
        s.add(row)
        await s.commit()
        return plaintext, row.id


async def _keys() -> Keys:
    admin, _ = await _key(["admin"])
    printer, print_id = await _key(["print"])
    reader, read_id = await _key(["read"])
    return Keys(admin=admin, print=printer, read=reader, read_id=read_id, print_id=print_id)


def h(key: str) -> dict[str, str]:
    return {"X-Label-Hub-Key": key}


@pytest.fixture
async def hub(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Hub]:
    import app.db.engine as eng

    monkeypatch.setenv("LABELYARD_SNIPEIT_URL", SNIPE)
    monkeypatch.setenv("LABELYARD_SNIPEIT_API_KEY", "snipe-fake-token")
    monkeypatch.setenv("LABELYARD_GROCY_URL", GROCY)
    monkeypatch.setenv("LABELYARD_GROCY_API_KEY", "grocy-fake-key")
    monkeypatch.setenv("LABELYARD_SPOOLMAN_URL", SPOOL)
    get_settings.cache_clear()
    _rate_limiter.clear()
    backend = MockPrinterBackend()
    monkeypatch.setattr(BackendRouter, "_build_one", staticmethod(lambda _cfg: backend))
    monkeypatch.setattr(
        "app.services.printer_runtime._resolve_model_id_from_config",
        AsyncMock(return_value="PT-P750W"),
    )

    async with eng.async_session() as s:
        printer = Printer(
            name="Tape",
            slug="tape-1",
            model="PT-P750W",
            backend="ptouch",
            connection={"host": "198.51.100.10", "port": 9100},
            enabled=True,
        )
        s.add(printer)
        await s.commit()
        printer_id = printer.id
    yield Hub(monkeypatch=monkeypatch, backend=backend, printer_id=printer_id)
    _rate_limiter.clear()
    get_settings.cache_clear()


def internal_strings() -> list[str]:
    hosts = [httpx.URL(u).host for u in INTERNAL.values()]
    return [*INTERNAL.values(), *hosts]


def assert_no_internal(where: str, body: str) -> None:
    for marker in internal_strings():
        assert marker not in body, f"{where} answers an internal address"
