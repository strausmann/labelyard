# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The ``Link`` header of a bounded list (ADR 0021, amendment 2026-11-22; RFC 8288).

A bounded list keeps its body a plain JSON array; the next page is named in a
``Link: <…>; rel="next"`` header. The target is a relative reference (path and query), so it
stays right behind a reverse proxy that rewrites the host.
"""

from __future__ import annotations

from urllib.parse import urlencode

from starlette.requests import Request


def next_link(request: Request, *, limit: int, offset: int, has_more: bool) -> dict[str, str]:
    """The ``Link`` header to the page after this one, or ``{}`` on the last page.

    Every other query parameter of the request is kept as it was; ``limit`` and ``offset`` are
    set to the next page's (``offset + limit``).
    """
    if not has_more:
        return {}
    kept = [(k, v) for k, v in request.query_params.multi_items() if k not in ("limit", "offset")]
    query = urlencode([*kept, ("limit", str(limit)), ("offset", str(offset + limit))])
    return {"Link": f'<{request.url.path}?{query}>; rel="next"'}
