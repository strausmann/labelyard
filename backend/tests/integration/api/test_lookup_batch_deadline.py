# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""#405: one deadline over the lookups of a whole fan-out request.

A batch print, a sheet print and a render check look up many items behind one shared bound
(``lookup_gate``, 8 at a time). Each lookup has its own limit, but the request had none: 500
slow items could hold it for minutes. ``integrations.lookup_batch_deadline_s`` bounds the
lookups of one request together; an item not looked up by then fails with ``lookup-timeout``.
An upstream error before the deadline keeps its own code.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from app.integrations.registry import IntegrationRegistry
from app.schemas.label_data import LabelData
from app.schemas.print_request import PrintLookupRequest
from app.schemas.sheet_print import SheetItem
from app.services import lookup_gate
from app.sheets import print_service as sps
from app.sheets.errors import ItemLookupFailedError

from tests.integration.test_batch_lookup_errors import (
    _Plugin,
    batch_env,  # noqa: F401  (fixture)
)

pytestmark = pytest.mark.asyncio

DEADLINE_S = 0.4


@pytest.fixture
def deadline() -> Iterator[float]:
    previous = lookup_gate.bind_batch_deadline(lambda: DEADLINE_S)
    yield DEADLINE_S
    lookup_gate.bind_batch_deadline(previous)


class _Slow(_Plugin):
    """Answers ``fast`` at once and every other identifier after three deadlines."""

    async def lookup(self, identifier: str) -> LabelData:
        if identifier != "fast":
            await asyncio.sleep(3 * DEADLINE_S)
        return await super().lookup(identifier)


def _lookup_item(identifier: str) -> dict[str, Any]:
    return {"content_type": "qr_two_lines", "lookup": {"app": "snipeit", "identifier": identifier}}


async def test_batch_lookup_stops_at_the_deadline(batch_env, deadline: float) -> None:  # noqa: F811
    client, queue, _ = batch_env
    IntegrationRegistry.register(_Slow("snipeit", None))  # type: ignore[arg-type]
    started = time.monotonic()
    response = await client.post(
        "/api/v1/print/p/batch",
        json={"items": [_lookup_item("fast"), _lookup_item("slow-1"), _lookup_item("slow-2")]},
    )
    elapsed = time.monotonic() - started
    assert response.status_code == 504, response.text
    assert response.json()["detail"]["error_code"] == "lookup-timeout"
    assert elapsed < 1.5 * deadline
    queue.enqueue_batch.assert_not_awaited()


async def test_batch_lookup_upstream_500_keeps_its_own_code(
    batch_env,  # noqa: F811
    deadline: float,
) -> None:
    client, queue, _ = batch_env
    failing = _Plugin("snipeit", RuntimeError("GET https://snipe.example.invalid/x answered 500"))
    IntegrationRegistry.register(failing)  # type: ignore[arg-type]
    response = await client.post("/api/v1/print/p/batch", json={"items": [_lookup_item("1")]})
    assert response.status_code == 502, response.text
    assert response.json()["detail"]["error_code"] == "integration_lookup_failed"
    assert "example.invalid" not in response.text
    queue.enqueue_batch.assert_not_awaited()


async def test_batch_raw_items_do_not_count_against_the_deadline(
    batch_env,  # noqa: F811
    deadline: float,
) -> None:
    """Only lookups are bounded: raw items need no integration."""
    client, _queue, _ = batch_env
    raw = {
        "content_type": "qr_two_lines",
        "data": {"primary_id": "R-1", "title": "Drill", "qr_payload": "x"},
    }
    response = await client.post("/api/v1/print/p/batch", json={"items": [raw] * 3})
    assert response.status_code == 202, response.text


class _SheetLookups:
    async def lookup(self, app: str, identifier: str) -> LabelData:
        if identifier != "fast":
            await asyncio.sleep(3 * DEADLINE_S)
        return LabelData(
            source_app=app,
            title="Cordless drill",
            primary_id=identifier,
            qr_payload="https://x.invalid/1",
        )


async def test_sheet_lookups_stop_at_the_deadline(deadline: float) -> None:
    items = [
        SheetItem(
            content_type="qr_two_lines",  # type: ignore[arg-type]
            lookup=PrintLookupRequest(app="snipeit", identifier=identifier),
        )
        for identifier in ("fast", "slow-1", "slow-2")
    ]
    started = time.monotonic()
    with pytest.raises(ItemLookupFailedError) as caught:
        await sps.resolve_items(items, _SheetLookups())  # type: ignore[arg-type]
    elapsed = time.monotonic() - started
    assert caught.value.status == 504
    assert caught.value.error_code == "lookup-timeout"
    assert caught.value.detail == {"item_index": 1}  # the lowest undone item
    assert elapsed < 1.5 * deadline


async def test_render_check_marks_undone_items_lookup_timeout(
    api_client_with_seed: Any,
    deadline: float,
) -> None:
    from tests.integration.api.test_render_check_api import _insert_key

    key = await _insert_key(["read"])

    async def lookup(app: str, identifier: str) -> LabelData:
        return await _SheetLookups().lookup(app, identifier)

    await api_client_with_seed.get("/healthz")  # the lifespan binds the setting on startup
    with patch("app.api.routes.print._lookup_service.lookup", new=lookup):
        # The test's deadline replaces the one the lifespan bound.
        previous = lookup_gate.bind_batch_deadline(lambda: DEADLINE_S)
        started = time.monotonic()
        try:
            resp = await api_client_with_seed.post(
                "/api/v1/render/check",
                json={"tape_mm": 62, "items": [_lookup_item("fast"), _lookup_item("slow")]},
                headers={"X-Label-Hub-Key": key},
            )
        finally:
            lookup_gate.bind_batch_deadline(previous)
    elapsed = time.monotonic() - started
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False
    assert results[1]["error_code"] == "lookup-timeout"
    assert elapsed < 1.5 * deadline


async def test_the_default_deadline_is_the_setting() -> None:
    from app.settings_store.registry import SETTINGS

    setting = SETTINGS["integrations.lookup_batch_deadline_s"]
    assert (setting.kind, setting.default, setting.env_field) == (
        "float",
        30.0,
        "lookup_batch_deadline_s",
    )
