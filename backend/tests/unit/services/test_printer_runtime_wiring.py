# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A printer that could not be wired says so (#401, ADR 0024).

The runtime keeps the last wiring failure per printer as a fixed reason code and a time, in
memory. The exception text (which names the device address) is never kept.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from app.printer_backends.exceptions import SnmpDiscoveryError
from app.printer_models.registry import ModelNotFoundError
from app.services.printer_runtime import wiring_reason_of
from pyasn1.error import PyAsn1Error
from pysnmp.error import PySnmpError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.services.test_printer_runtime import Env, _make_env, _tape

_RESOLVE = "app.services.printer_runtime._resolve_model_id_from_config"


@pytest_asyncio.fixture
async def env(async_session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[Env]:
    e = await _make_env(async_session_factory)
    try:
        yield e
    finally:
        await e.runtime.stop()


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (ModelNotFoundError("no driver"), "model_unknown"),
        (SnmpDiscoveryError("no answer"), "discovery_failed"),
        (PySnmpError("timeout"), "discovery_failed"),
        (PyAsn1Error("malformed"), "discovery_failed"),
        (OSError("unreachable"), "discovery_failed"),
        (TimeoutError("slow"), "discovery_failed"),
        (ValueError("bad port"), "invalid_config"),
        (TypeError("bug"), "unexpected"),
    ],
)
async def test_failed_attach_records_the_reason(env: Env, exc: Exception, reason: str) -> None:
    printer = _tape(discover=True)
    with patch(_RESOLVE, AsyncMock(side_effect=exc)):
        await env.runtime.attach(printer)  # does not raise

    assert env.runtime.wiring_of(printer.id, enabled=True) == ("not_wired", reason)
    assert env.runtime.wiring_failed_at(printer.id) is not None
    assert wiring_reason_of(exc) == reason


async def test_a_stored_row_that_cannot_be_read_is_invalid_config(env: Env) -> None:
    printer = _tape()
    printer.connection = {"host": "192.0.2.10", "port": "not-a-number"}  # a malformed stored port
    await env.runtime.attach(printer)

    assert env.runtime.wiring_of(printer.id, enabled=True) == ("not_wired", "invalid_config")


async def test_successful_attach_clears_the_failure(env: Env) -> None:
    printer = _tape()
    with patch(_RESOLVE, AsyncMock(side_effect=ModelNotFoundError("no driver"))):
        await env.runtime.attach(printer)
    assert env.runtime.wiring_of(printer.id, enabled=True)[0] == "not_wired"

    await env.runtime.attach(printer)  # the address was fixed and the printer saved again

    assert env.runtime.wiring_of(printer.id, enabled=True) == ("wired", None)
    assert env.runtime.wiring_failed_at(printer.id) is None


async def test_disabled_printer_reports_disabled(env: Env) -> None:
    printer = _tape()
    with patch(_RESOLVE, AsyncMock(side_effect=OSError("unreachable"))):
        await env.runtime.attach(printer)

    # Switched off on purpose: not a failure, whatever the last attempt said.
    assert env.runtime.wiring_of(printer.id, enabled=False) == ("disabled", None)


async def test_the_failure_keeps_no_exception_text(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    printer = _tape(discover=True)
    secret = "cannot reach printer.example.invalid"
    with (
        patch(_RESOLVE, AsyncMock(side_effect=OSError(secret))),
        caplog.at_level(logging.DEBUG, logger="app.services.printer_runtime"),
    ):
        await env.runtime.attach(printer)

    stored: Any = env.runtime._failures[printer.id]
    assert "example.invalid" not in repr(stored)
    assert all("example.invalid" not in r.getMessage() for r in caplog.records)
    assert any("OSError" in r.getMessage() for r in caplog.records)


async def test_a_deleted_printer_leaves_no_failure_behind(env: Env) -> None:
    printer = _tape()
    with patch(_RESOLVE, AsyncMock(side_effect=ModelNotFoundError("no driver"))):
        await env.runtime.attach(printer)

    await env.runtime.detach(printer.id)

    assert env.runtime.wiring_failed_at(printer.id) is None
