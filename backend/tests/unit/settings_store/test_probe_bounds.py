# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""#405: the connection test reads a bounded answer within one overall deadline.

The admin types the URL, so the answer comes from wherever that points. Only the status counts;
the body is read at most ``PROBE_MAX_BODY_BYTES`` (64 KiB), never decompressed, and the whole
exchange ends within the setting's ``timeout``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from app.settings_store import probes
from app.settings_store.probes import PROBE_MAX_BODY_BYTES, probe_integration

pytestmark = pytest.mark.asyncio

CHUNK = 4096
URL = "http://grocy.example.invalid"


def _streamed(data: bytes) -> AsyncIterator[bytes]:
    """A body as a real server sends it: a stream, not bytes already in memory."""

    async def gen() -> AsyncIterator[bytes]:
        yield data

    return gen()


class _Counter:
    def __init__(self) -> None:
        self.sent = 0


def _use_transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[..., Any]) -> None:
    """Every client the probe opens talks to ``handler`` instead of the network."""
    real = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(probes.httpx, "AsyncClient", client)


async def test_probe_reads_at_most_64_kib(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 10 MiB answer: the probe stops reading after 64 KiB (plus the chunk in flight)."""
    counter = _Counter()

    async def body() -> AsyncIterator[bytes]:
        for _ in range((10 * 1024 * 1024) // CHUNK):
            counter.sent += CHUNK
            yield b"x" * CHUNK

    _use_transport(monkeypatch, lambda request: httpx.Response(200, content=body()))
    ok, status, detail = await probe_integration("grocy", url=URL, secret="s", timeout=5.0)
    assert (ok, status, detail) == (True, 200, "ok")
    assert counter.sent <= PROBE_MAX_BODY_BYTES + CHUNK


async def test_probe_bound_is_64_kib() -> None:
    assert PROBE_MAX_BODY_BYTES == 65_536


async def test_probe_refuses_a_compressed_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe asked for ``identity``; an answer that is compressed anyway is refused."""
    _use_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            content=_streamed(b"\x1f\x8b not really gzip"),
        ),
    )
    ok, status, detail = await probe_integration("grocy", url=URL, secret="s", timeout=5.0)
    assert ok is False
    assert status == 200
    assert detail == "probe_compressed_body"


async def test_probe_accepts_identity_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"Content-Encoding": "identity"}, content=_streamed(b"{}")
        ),
    )
    assert await probe_integration("grocy", url=URL, secret="s", timeout=5.0) == (True, 200, "ok")


async def test_probe_sends_identity_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, content=_streamed(b"{}"))

    _use_transport(monkeypatch, handler)
    await probe_integration("grocy", url=URL, secret="s", timeout=5.0)
    assert seen["accept-encoding"] == "identity"


async def test_probe_deadline_covers_the_whole_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that trickles one small chunk after another never trips a per-read timeout;
    the overall deadline ends the probe anyway."""
    timeout = 0.4

    async def trickle() -> AsyncIterator[bytes]:
        while True:
            await asyncio.sleep(timeout / 8)
            yield b"x"

    _use_transport(monkeypatch, lambda request: httpx.Response(200, content=trickle()))
    started = time.monotonic()
    # The test's own bound: without the probe's deadline the read never ends.
    async with asyncio.timeout(10 * timeout):
        ok, status, detail = await probe_integration("grocy", url=URL, secret="s", timeout=timeout)
    elapsed = time.monotonic() - started
    assert (ok, status, detail) == (False, None, "timeout")
    assert elapsed < 1.5 * timeout


async def test_probe_non_2xx_keeps_its_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_transport(monkeypatch, lambda request: httpx.Response(401, content=_streamed(b"nope")))
    assert await probe_integration("grocy", url=URL, secret="s", timeout=5.0) == (
        False,
        401,
        "HTTP 401",
    )


async def test_probe_connection_error_names_the_class_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connect to grocy.example.invalid failed")

    _use_transport(monkeypatch, handler)
    assert await probe_integration("grocy", url=URL, secret="s", timeout=5.0) == (
        False,
        None,
        "connection failed (ConnectError)",
    )
