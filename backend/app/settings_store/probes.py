# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Test-connection probes for the integrations. Never echo the secret.

The admin types the URL, so the answer comes from wherever it points (#405). Only the status
counts: the probe asks for ``Accept-Encoding: identity``, refuses an answer that is compressed
anyway (``probe_compressed_body``), reads at most ``PROBE_MAX_BODY_BYTES`` of the body and ends
the whole exchange (connect, headers and body together) within the setting's ``timeout``, as
the integration lookups do.
"""

from __future__ import annotations

import asyncio
from typing import Final

import httpx

from app.integrations.urls import api_url

PROBE_MAX_BODY_BYTES: Final = 65_536
"""The most the connection test reads of an answer's body; only the status counts."""

_PROBES: dict[str, tuple[str, str | None]] = {
    # name: (path, header template or None); {s} is replaced with the secret
    "snipeit": ("/api/v1/hardware?limit=1", "Authorization: Bearer {s}"),
    "grocy": ("/api/system/info", "GROCY-API-KEY: {s}"),
    "spoolman": ("/api/v1/info", None),
    # Not "/api/": Paperless-ngx answers that with a 302 to the schema view for any caller.
    # The documents list answers 200 for a valid token and 401 otherwise.
    "paperless": ("/api/documents/?page_size=1", "Authorization: Token {s}"),
}

# Integrations whose users may type the API root ("/api") as part of the base URL.
_STRIP_API: frozenset[str] = frozenset({"paperless"})


async def probe_integration(
    name: str, *, url: str, secret: str, timeout: float
) -> tuple[bool, int | None, str]:
    path, header = _PROBES[name]
    headers: dict[str, str] = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if header and secret:
        hname, _, template = header.partition(": ")
        headers[hname] = template.replace("{s}", secret)
    try:
        target = api_url(url, path, strip_api=name in _STRIP_API)
    except ValueError:
        return False, None, "invalid URL"
    try:
        async with asyncio.timeout(timeout):
            status, compressed = await _answer(target, headers, timeout)
    except UnicodeError:
        return False, None, "invalid characters in the URL or secret"
    except (TimeoutError, httpx.TimeoutException):
        return False, None, "timeout"
    except httpx.HTTPError as exc:
        return False, None, f"connection failed ({type(exc).__name__})"
    if compressed:
        return False, status, "probe_compressed_body"
    if 200 <= status < 300:
        return True, status, "ok"
    return False, status, f"HTTP {status}"


async def _answer(target: str, headers: dict[str, str], timeout: float) -> tuple[int, bool]:
    """The answer's status and whether it came compressed; reads at most 64 KiB of raw body."""
    # follow_redirects=False: never forward the secret to a redirect target.
    async with (
        httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client,
        client.stream("GET", target, headers=headers) as resp,
    ):
        encoding = resp.headers.get("Content-Encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            return resp.status_code, True
        read = 0
        async for chunk in resp.aiter_raw():
            read += len(chunk)
            if read >= PROBE_MAX_BODY_BYTES:
                break
        return resp.status_code, False
