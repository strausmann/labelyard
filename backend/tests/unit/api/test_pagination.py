# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""``next_link``: the ``Link`` header of a bounded list (ADR 0021, D31)."""

from __future__ import annotations

from app.api.pagination import next_link
from starlette.requests import Request


def _request(query: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/sheets",
            "query_string": query.encode(),
            "headers": [],
            "server": ("hub.example.invalid", 443),
            "scheme": "https",
            "root_path": "",
        }
    )


def test_last_page_has_no_link() -> None:
    assert next_link(_request("limit=5"), limit=5, offset=0, has_more=False) == {}


def test_next_page_keeps_other_parameters_and_moves_the_offset() -> None:
    link = next_link(
        _request("q=a%26b&offset=10&limit=5&state=all"), limit=5, offset=10, has_more=True
    )
    assert link == {"Link": '</api/v1/sheets?q=a%26b&state=all&limit=5&offset=15>; rel="next"'}


def test_repeated_parameters_are_kept() -> None:
    link = next_link(_request("x=1&x=2"), limit=500, offset=0, has_more=True)
    assert link["Link"] == '</api/v1/sheets?x=1&x=2&limit=500&offset=500>; rel="next"'
