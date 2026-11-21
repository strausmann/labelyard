# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The printer read says whether the hub could wire the printer (#401, ADR 0024).

Runs the real lifespan (the ``live`` fixture of the live-wiring tests). The wiring failure is
made with a patched model resolution, so no network is touched. The reason is a fixed code:
the text of the error, which names the device address, never reaches a response.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from tests.integration.api import test_printer_live_wiring as _live_wiring
from tests.integration.api.test_printer_live_wiring import ADMIN, Live

pytestmark = pytest.mark.asyncio

live = _live_wiring.live  # the fixture of the live-wiring tests: the real lifespan, an admin caller

_RESOLVE = "app.services.printer_runtime._resolve_model_id_from_config"
_PUBLIC = "/api/v1/printers"
_HOST = "printer.example.invalid"


def _tape_body(slug: str = "shelf-tape") -> dict[str, Any]:
    return {
        "name": f"Tape {slug}",
        "slug": slug,
        "backend": "ptouch",
        "model": "PT-P750W",
        "connection": {"host": "192.0.2.50", "port": 9100, "snmp": {"discover": False}},
    }


async def _create_unwired(live: Live, slug: str = "shelf-tape") -> UUID:
    with patch(_RESOLVE, AsyncMock(side_effect=OSError(f"cannot reach {_HOST}"))):
        resp = await live.client.post(ADMIN, json=_tape_body(slug))
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


async def test_printer_read_reports_not_wired_without_the_exception_text(live: Live) -> None:
    pid = await _create_unwired(live)

    for url in (f"{_PUBLIC}/{pid}", f"{ADMIN}/shelf-tape"):
        resp = await live.client.get(url)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["wiring"] == "not_wired"
        assert body["wiring_reason"] == "discovery_failed"
        assert body["wiring_at"] is not None
        assert _HOST not in resp.text  # CWE-209: the text of the error names the address

    listed = (await live.client.get(_PUBLIC)).json()
    row = next(p for p in listed if p["id"] == str(pid))
    assert (row["wiring"], row["wiring_reason"]) == ("not_wired", "discovery_failed")
    assert _HOST not in (await live.client.get(_PUBLIC)).text
    assert _HOST not in (await live.client.get(ADMIN)).text


async def test_the_create_response_already_names_the_reason(live: Live) -> None:
    with patch(_RESOLVE, AsyncMock(side_effect=OSError(f"cannot reach {_HOST}"))):
        resp = await live.client.post(ADMIN, json=_tape_body("shelf-two"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["wiring"] == "not_wired"
    assert resp.json()["wiring_reason"] == "discovery_failed"
    assert _HOST not in resp.text


async def test_saving_again_wires_the_printer_and_clears_the_reason(live: Live) -> None:
    pid = await _create_unwired(live)

    resp = await live.client.put(f"{ADMIN}/shelf-tape", json={})  # saved again, now reachable
    assert resp.status_code == 200, resp.text
    assert resp.json()["wiring"] == "wired"
    assert resp.json()["wiring_reason"] is None
    assert resp.json()["wiring_at"] is None

    public = (await live.client.get(f"{_PUBLIC}/{pid}")).json()
    assert (public["wiring"], public["wiring_reason"], public["wiring_at"]) == ("wired", None, None)


async def test_a_wired_printer_reads_wired(live: Live) -> None:
    resp = await live.client.post(ADMIN, json=_tape_body("shelf-ok"))
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]

    body = (await live.client.get(f"{_PUBLIC}/{pid}")).json()
    assert (body["wiring"], body["wiring_reason"]) == ("wired", None)


async def test_disabled_printer_reports_disabled(live: Live) -> None:
    await _create_unwired(live)

    resp = await live.client.post(f"{ADMIN}/shelf-tape/disable")
    assert resp.status_code == 200, resp.text
    assert resp.json()["wiring"] == "disabled"
    assert resp.json()["wiring_reason"] is None
    assert resp.json()["wiring_at"] is None
