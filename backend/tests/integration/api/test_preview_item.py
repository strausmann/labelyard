# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The preview of an integration item draws the code that prints (#247, ADR 0033).

With ``item`` the server looks the item up and draws ``qr_payload_for``; the QR is decoded from the
PNG and compared with the payload of the job the same item prints as. Harness:
``tests/helpers/web_url_hub.py``.
"""

from __future__ import annotations

import json
from typing import Any

import app.models  # noqa: F401
import pytest

from tests.helpers.qr_decode import decode_qr
from tests.helpers.web_url_hub import (
    INTERNAL,
    ITEM,
    PATH,
    WEB,
    Hub,
    assert_no_internal,
    h,
    hub,
)

pytestmark = pytest.mark.asyncio
__all__ = ["hub"]


# -- preview by item --------------------------------------------------------------------


def _preview_body(app_name: str, **extra: Any) -> dict[str, Any]:
    return {
        "content_type": "qr_two_lines",
        "tape_mm": 24,
        "data": {"title": "Shown", "primary_id": ITEM[app_name], **extra},
        "item": {"app": app_name, "id": ITEM[app_name]},
    }


@pytest.mark.parametrize("app_name", ["snipeit", "grocy", "spoolman"])
@pytest.mark.parametrize("with_web", [False, True])
async def test_preview_qr_equals_the_printed_payload(
    hub: Hub, app_name: str, with_web: bool
) -> None:
    if with_web:
        hub.web(app_name)
    async with hub.running() as (client, keys):
        preview = await client.post(
            "/api/v1/render/preview", json=_preview_body(app_name), headers=h(keys.admin)
        )
        assert preview.status_code == 200, preview.text
        printed = await client.post(
            "/api/v1/print",
            json={
                "content_type": "qr_two_lines",
                "lookup": {"app": app_name, "identifier": ITEM[app_name]},
            },
            headers=h(keys.admin),
        )
        assert printed.status_code == 202, printed.text
        job = await client.get(f"/api/v1/jobs/{printed.json()['job_id']}", headers=h(keys.admin))
    stored = job.json()["payload"]["label_data"]["qr_payload"]
    expected = (WEB[app_name].rstrip("/") if with_web else INTERNAL[app_name]) + PATH[app_name]
    assert stored == expected
    assert decode_qr(preview.content) == [stored]


async def test_preview_ignores_client_qr_payload_with_item(hub: Hub) -> None:
    hub.web("snipeit")
    async with hub.running() as (client, keys):
        r = await client.post(
            "/api/v1/render/preview",
            json=_preview_body("snipeit", qr_payload="https://attacker.example.invalid/x"),
            headers=h(keys.read),
        )
    assert r.status_code == 200, r.text
    assert decode_qr(r.content) == [WEB["snipeit"] + PATH["snipeit"]]


async def test_preview_without_item_keeps_client_payload(hub: Hub) -> None:
    """Guard: a manual label draws the payload the client sends."""
    async with hub.running() as (client, keys):
        r = await client.post(
            "/api/v1/render/preview",
            json={
                "content_type": "qr_two_lines",
                "tape_mm": 24,
                "data": {
                    "title": "Manual",
                    "primary_id": "M-1",
                    "qr_payload": "https://example.invalid/m",
                },
            },
            headers=h(keys.read),
        )
    assert r.status_code == 200, r.text
    assert decode_qr(r.content) == ["https://example.invalid/m"]


async def test_preview_item_upstream_timeout_uses_the_lookup_timeout_code(hub: Hub) -> None:
    async with hub.running() as (client, keys):
        lookup = await client.get("/api/v1/lookup/snipeit/SLOW", headers=h(keys.read))
        body = _preview_body("snipeit")
        body["item"]["id"] = "SLOW"
        preview = await client.post("/api/v1/render/preview", json=body, headers=h(keys.read))
    assert lookup.status_code == 502, lookup.text
    assert (preview.status_code, preview.json()["detail"]["error_code"]) == (
        lookup.status_code,
        lookup.json()["detail"]["error_code"],
    )
    assert_no_internal("preview timeout", preview.text)


async def test_preview_item_upstream_404_is_the_lookups_404(hub: Hub) -> None:
    async with hub.running() as (client, keys):
        lookup = await client.get("/api/v1/lookup/snipeit/MISSING", headers=h(keys.read))
        body = _preview_body("snipeit")
        body["item"]["id"] = "MISSING"
        preview = await client.post("/api/v1/render/preview", json=body, headers=h(keys.read))
    assert lookup.status_code == 404, lookup.text
    assert preview.status_code == 404, preview.text
    assert json.loads(preview.text) == json.loads(lookup.text)


@pytest.mark.parametrize("app_name", ["snipeit", "grocy", "spoolman"])
async def test_read_key_preview_carries_no_internal_base_url(hub: Hub, app_name: str) -> None:
    """A caller that cannot print gets a stand-in code while there is no web address."""
    async with hub.running() as (client, keys):
        read = await client.post(
            "/api/v1/render/preview", json=_preview_body(app_name), headers=h(keys.read)
        )
        printer = await client.post(
            "/api/v1/render/preview", json=_preview_body(app_name), headers=h(keys.print)
        )
    assert read.status_code == 200, read.text
    (code,) = decode_qr(read.content)
    assert_no_internal("read preview QR", code)
    assert code == "x" * len(INTERNAL[app_name] + PATH[app_name])
    assert read.headers["x-labelyard-qr"] == "withheld"
    # A print key can print the item and read the paper: it sees what prints.
    assert decode_qr(printer.content) == [INTERNAL[app_name] + PATH[app_name]]
    assert "x-labelyard-qr" not in printer.headers
    # The stand-in keeps the picture the same size as the one of the printed code.
    import io

    from PIL import Image

    assert Image.open(io.BytesIO(read.content)).size == Image.open(io.BytesIO(printer.content)).size


async def test_read_key_preview_with_web_url_shows_the_printed_code(hub: Hub) -> None:
    hub.web("grocy")
    async with hub.running() as (client, keys):
        r = await client.post(
            "/api/v1/render/preview", json=_preview_body("grocy"), headers=h(keys.read)
        )
    assert r.status_code == 200, r.text
    assert decode_qr(r.content) == [WEB["grocy"] + PATH["grocy"]]
    assert "x-labelyard-qr" not in r.headers
