# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The sheet preview draws no internal address for a key that cannot print (#247, ADR 0033).

Same rule as the tape preview (``links.preview_qr_for``): a print key sees the code that
prints; a read key sees the web form, or a stand-in code flagged ``qr_withheld`` while the
integration has no web address. The QR codes are decoded from the preview picture.
"""

from __future__ import annotations

import base64
from typing import Any

import app.models  # noqa: F401
import httpx
import pytest
from app.schemas.label_data import LabelData

from tests.helpers.qr_decode import decode_qr
from tests.integration.api.test_sheet_preview_api import _insert_key, _printer, _sheet, _url

pytestmark = pytest.mark.asyncio

INTERNAL = "http://grocy.internal.example.invalid:9283"


class _Lookup:
    async def lookup(self, source_app: str, identifier: str) -> LabelData:
        return LabelData(
            source_app=source_app,
            title=f"Product {identifier}",
            primary_id=identifier,
            qr_payload=f"{INTERNAL}/product/{identifier}",
        )


@pytest.fixture(autouse=True)
def _grocy_base(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Grocy's base address is the internal one the stand-in lookup answers under."""
    from app.config import get_settings

    monkeypatch.setenv("LABELYARD_GROCY_URL", INTERNAL)
    monkeypatch.setenv("LABELYARD_GROCY_API_KEY", "grocy-fake-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _preview(client: httpx.AsyncClient, scopes: list[str]) -> dict[str, Any]:
    from app.api.routes.sheet_print import get_lookup_service

    pid, _ = await _printer()
    code = await _sheet()
    await client.get("/healthz")  # the lifespan starts on the first request
    transport_app = client._transport.app  # type: ignore[attr-defined]
    fastapi_app = getattr(transport_app, "_app", transport_app)
    fastapi_app.dependency_overrides[get_lookup_service] = _Lookup
    try:
        resp = await client.post(
            _url(pid),
            json={
                "items": [
                    {"content_type": "qr_two_lines", "lookup": {"app": "grocy", "identifier": "7"}}
                ],
                "placements": [{"sheet": code}],
            },
            headers={"X-Label-Hub-Key": await _insert_key(scopes)},
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_lookup_service, None)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _codes(body: dict[str, Any]) -> list[str]:
    (page,) = body["pages"]
    return decode_qr(base64.b64decode(page["png_base64"]))


def _new_cells(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in body["pages"][0]["cells"] if c["state"] == "new"]


async def test_read_key_sheet_preview_carries_no_internal_base_url(
    api_client_with_seed: httpx.AsyncClient,
) -> None:
    body = await _preview(api_client_with_seed, ["read"])
    codes = _codes(body)
    assert codes, "no QR found in the preview picture"
    assert not any("internal" in c for c in codes)
    assert "internal" not in str(body)
    assert [c["qr_withheld"] for c in _new_cells(body)] == [True]


async def test_print_key_sheet_preview_shows_the_printed_code(
    api_client_with_seed: httpx.AsyncClient,
) -> None:
    body = await _preview(api_client_with_seed, ["print"])
    assert f"{INTERNAL}/product/7" in _codes(body)
    assert [c["qr_withheld"] for c in _new_cells(body)] == [False]
