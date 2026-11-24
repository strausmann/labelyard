# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The tray a job was printed from, as the printer reports it (#336, ADR 0029).

``get_job(..., media_source=True)`` asks for ``media-col-actual`` (PWG 5100.7) and the plain
``media-source``; the polls do not. The answer is printer input: bounded, never interpreted. The
hand-assembled responses do not go through the codec's encoder, so a bug the encoder and decoder
share cannot cancel out here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from app.printer_backends.ipp.client import MAX_JOB_TEXT_LENGTH, IppClient
from app.printer_backends.ipp.codec import decode

from tests.helpers.fake_ipp_printer import FakeIppPrinter
from tests.unit.printer_backends.ipp.fixtures import assemble as wire

pytestmark = pytest.mark.asyncio

URI = "ipp://printer.example.invalid/ipp/print"
PDF = b"%PDF-1.4\n% synthetic test document\n%%EOF\n"


class _Body(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body


class _Canned(httpx.AsyncBaseTransport):
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests: list[bytes] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(await request.aread())
        return httpx.Response(
            200, headers={"content-type": "application/ipp"}, stream=_Body(self.body)
        )


def _job_response(extra: bytes) -> bytes:
    return (
        bytes([0x01, 0x01])
        + wire.u16(0x0000)
        + wire.i32(1)
        + bytes([0x01])
        + wire.attr(0x47, "attributes-charset", b"utf-8")
        + wire.attr(0x48, "attributes-natural-language", b"en")
        + bytes([0x02])
        + wire.attr(0x21, "job-id", wire.i32(9))
        + wire.attr(0x23, "job-state", wire.i32(9))
        + extra
        + wire.END_OF_ATTRIBUTES
    )


def _media_col_actual(source: bytes) -> bytes:
    """``media-col-actual`` = one collection with ``media-type`` and ``media-source``."""
    return (
        wire.attr(0x34, "media-col-actual", b"")
        + wire.member("media-type")
        + wire.more(0x44, b"labels")
        + wire.member("media-source")
        + wire.more(0x44, source)
        + wire.END_COLLECTION
    )


def _requested(body: bytes) -> list[object]:
    from app.printer_backends.ipp.codec import Tag

    ops = decode(body)[0].attrs(Tag.OPERATION)
    return list(ops.get("requested-attributes", []))


async def test_ipp_job_media_source_parsed() -> None:
    transport = _Canned(_job_response(_media_col_actual(b"tray-2")))
    async with IppClient(URI, transport=transport) as client:
        info = await client.get_job(9, media_source=True)
    assert info.media_source == "tray-2"
    requested = _requested(transport.requests[0])
    assert "media-col-actual" in requested and "media-source" in requested


async def test_plain_media_source_is_the_fallback() -> None:
    transport = _Canned(_job_response(wire.attr(0x44, "media-source", b"bypass-tray")))
    async with IppClient(URI, transport=transport) as client:
        info = await client.get_job(9, media_source=True)
    assert info.media_source == "bypass-tray"


async def test_media_col_actual_wins_over_media_source() -> None:
    extra = wire.attr(0x44, "media-source", b"auto") + _media_col_actual(b"tray-1")
    async with IppClient(URI, transport=_Canned(_job_response(extra))) as client:
        info = await client.get_job(9, media_source=True)
    assert info.media_source == "tray-1"


async def test_a_job_without_media_source_reports_none() -> None:
    transport = _Canned(_job_response(b""))
    async with IppClient(URI, transport=transport) as client:
        info = await client.get_job(9, media_source=True)
    assert info.media_source is None


async def test_a_media_source_that_is_not_text_is_none() -> None:
    extra = (
        wire.attr(0x34, "media-col-actual", b"")
        + wire.member("media-source")
        + wire.more(0x21, wire.i32(3))
        + wire.END_COLLECTION
    )
    async with IppClient(URI, transport=_Canned(_job_response(extra))) as client:
        info = await client.get_job(9, media_source=True)
    assert info.media_source is None


async def test_a_long_media_source_is_bounded() -> None:
    extra = _media_col_actual(b"t" * 1000)
    async with IppClient(URI, transport=_Canned(_job_response(extra))) as client:
        info = await client.get_job(9, media_source=True)
    assert info.media_source is not None and len(info.media_source) == MAX_JOB_TEXT_LENGTH


async def test_the_polls_do_not_ask_for_the_media_source() -> None:
    transport = _Canned(_job_response(_media_col_actual(b"tray-2")))
    async with IppClient(URI, transport=transport) as client:
        await client.get_job(9)
    requested = _requested(transport.requests[0])
    assert "media-col-actual" not in requested and "media-source" not in requested


async def test_fake_printer_answers_media_col_actual_when_asked() -> None:
    fake = FakeIppPrinter(job_script=[9], job_media_source="tray-2")
    async with IppClient(URI, transport=httpx.ASGITransport(app=fake)) as client:
        accepted = await client.print_job(PDF, [], [])
        assert (await client.get_job(accepted.job_id)).media_source is None
        assert (await client.get_job(accepted.job_id, media_source=True)).media_source == "tray-2"
