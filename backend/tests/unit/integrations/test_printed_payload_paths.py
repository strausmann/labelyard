# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The sheet print and the render check use the printed payload of ``qr_payload_for`` (#247).

ADR 0033: one function decides the code of a looked-up item on every print path. The tape
print, the batch and the preview are covered through the real app in
``tests/integration/api/test_preview_item.py``; these two paths take their lookup service as an
argument, so a stand-in lookup and the bound settings are enough here.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from app.integrations import links
from app.schemas.content_type import ContentType
from app.schemas.label_data import LabelData
from app.schemas.print_request import PrintLookupRequest
from app.schemas.render_check import RenderCheckItem, RenderCheckLookup, RenderCheckRequest
from app.schemas.sheet_print import SheetItem
from app.services.render_check import check_items
from app.sheets.print_service import resolve_items

INTERNAL = "http://grocy.internal.example.invalid:9283"
WEB = "https://pantry.example.invalid"


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get(self, key: str) -> Any:
        return self._values.get(key, "")


class _Lookup:
    async def lookup(self, app: str, identifier: str) -> LabelData:
        return LabelData(
            source_app=app,
            title="Oat Flakes",
            primary_id=identifier,
            qr_payload=f"{INTERNAL}/product/{identifier}",
        )


@pytest.fixture
def bound() -> Iterator[Any]:
    def bind(web: str) -> None:
        links.bind_settings(
            _Settings({"integrations.grocy.url": INTERNAL, "integrations.grocy.web_url": web})
        )

    previous = links.bound_settings()
    yield bind
    links.bind_settings(previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("web", ["", WEB])
async def test_sheet_print_prints_the_web_payload(bound: Any, web: str) -> None:
    bound(web)
    item = SheetItem(
        content_type=ContentType.QR_TWO_LINES,
        lookup=PrintLookupRequest(app="grocy", identifier="7"),
    )
    (resolved,) = await resolve_items([item], _Lookup())  # type: ignore[arg-type]
    assert resolved.label.qr_payload == f"{web or INTERNAL}/product/7"


@pytest.mark.asyncio
async def test_render_check_checks_the_printed_payload(bound: Any) -> None:
    """A web address long enough that only the printed code is too long for the tape's QR."""
    request = RenderCheckRequest(
        tape_mm=62,
        items=[
            RenderCheckItem(
                content_type=ContentType.QR_ONLY,
                lookup=RenderCheckLookup(app="grocy", identifier="7"),
            )
        ],
    )
    bound("")
    assert (await check_items(request, _Lookup())).results[0].ok  # type: ignore[arg-type]
    bound(WEB + "/" + "p" * 600)
    verdict = (await check_items(request, _Lookup())).results[0]  # type: ignore[arg-type]
    assert (verdict.ok, verdict.error_code) == (False, "qr_payload_too_long")
