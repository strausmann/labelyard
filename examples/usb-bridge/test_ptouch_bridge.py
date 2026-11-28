# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests of the USB network bridge, standard library only.

Run from the repository root: ``python3 -m unittest discover -s examples/usb-bridge``
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

_STATE = tempfile.TemporaryDirectory()
os.environ["STATE_DIRECTORY"] = _STATE.name
sys.path.insert(0, str(Path(__file__).resolve().parent))
ptb: Any = importlib.import_module("ptouch_bridge")

HR_STATUS, HR_ERRORS = ptb.HR_PRINTER_STATUS, ptb.HR_PRINTER_ERROR_STATE
MEDIA, COUNTER = ptb.PRT_INPUT_MEDIA_NAME, ptb.PRT_MARKER_LIFE_COUNT


def status_block(
    *,
    err1: int = 0,
    err2: int = 0,
    width: int = 24,
    media: int = 0x01,
    kind: int = 0,
    phase: int = 0,
) -> bytes:
    """A 32-byte status block as a PT printer sends it."""
    b = bytearray(32)
    b[0:3] = b"\x80\x20\x42"
    b[3], b[4], b[5] = 0x30, 0x6A, 0x30
    b[8], b[9], b[10], b[11] = err1, err2, width, media
    b[18], b[19] = kind, phase
    return bytes(b)


class FakePrinter:
    """Stands in for ``Printer`` behind the SNMP agent."""

    def __init__(self, status: bytes | None, *, printing: bool = False, counter: bool = True):
        self.product, self.serial, self.count = "PT-D600", "D5G123456", 7
        self._status, self._printing, self._counter = status, printing, counter

    @property
    def counter_offered(self) -> bool:
        return self._counter

    def current_status(self) -> tuple[bytes | None, bool]:
        return self._status, self._printing


def request(
    oids: list[tuple[int, ...]], *, pdu: int = 0xA0, version: int = 1, community: bytes = b"public"
) -> bytes:
    vbl = b"".join(ptb.tlv(0x30, ptb.enc_oid(o) + b"\x05\x00") for o in oids)
    body = ptb.enc_int(4242) + ptb.enc_int(0) + ptb.enc_int(0) + ptb.tlv(0x30, vbl)
    return ptb.tlv(0x30, ptb.enc_int(version) + ptb.enc_str(community) + ptb.tlv(pdu, body))


def response(packet: bytes) -> tuple[int, int, list[tuple[tuple[int, ...], int, bytes]]]:
    """(request id, error status, [(oid, tag, value)]) of a GetResponse."""
    _, msg, _ = ptb.dec(packet, 0)
    _, _, p = ptb.dec(msg, 0)
    _, _, p = ptb.dec(msg, p)
    tag, pdu, _ = ptb.dec(msg, p)
    assert tag == 0xA2
    _, rid, q = ptb.dec(pdu, 0)
    _, err, q = ptb.dec(pdu, q)
    _, _, q = ptb.dec(pdu, q)
    _, vbl, _ = ptb.dec(pdu, q)
    out, pos = [], 0
    while pos < len(vbl):
        _, vb, pos = ptb.dec(vbl, pos)
        _, oid, r = ptb.dec(vb, 0)
        vtag, value, _ = ptb.dec(vb, r)
        out.append((ptb.dec_oid(oid), vtag, value))
    return int.from_bytes(rid, "big"), int.from_bytes(err, "big"), out


class BerTest(unittest.TestCase):
    def test_integers_round_trip(self) -> None:
        for v in (0, 1, 127, 128, 255, 256, -1, -128, -129, 2**31 - 1):
            _, body, _ = ptb.dec(ptb.enc_int(v), 0)
            self.assertEqual(int.from_bytes(body, "big", signed=True), v)

    def test_unsigned_keeps_the_high_bit_positive(self) -> None:
        _, body, _ = ptb.dec(ptb.enc_uint(0xFFFFFFFF, 0x41), 0)
        self.assertEqual(body, b"\x00\xff\xff\xff\xff")

    def test_oids_round_trip(self) -> None:
        for oid in (ptb.BROTHER_PJL, ptb.SYS_DESCR, (1, 3, 6, 1, 4, 1, 2435, 128, 16384)):
            _, body, _ = ptb.dec(ptb.enc_oid(oid), 0)
            self.assertEqual(ptb.dec_oid(body), oid)

    def test_long_lengths(self) -> None:
        blob = b"x" * 300
        tag, value, end = ptb.dec(ptb.tlv(0x04, blob), 0)
        self.assertEqual((tag, value, end), (0x04, blob, 304))

    def test_truncated_input_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            ptb.dec(b"\x04\x05abc", 0)


class StatusMappingTest(unittest.TestCase):
    def test_ready_printer_has_no_error_bits(self) -> None:
        self.assertEqual(ptb.error_bits(status_block()), b"\x00\x00")

    def test_cover_open(self) -> None:
        # With the cover open the printer reports no tape either.
        self.assertEqual(ptb.error_bits(status_block(err2=0x10, width=0, media=0)), b"\x48\x00")

    def test_end_of_tape_and_cutter_jam(self) -> None:
        self.assertEqual(ptb.error_bits(status_block(err1=0x02)), b"\x40\x00")
        self.assertEqual(ptb.error_bits(status_block(err1=0x04)), b"\x04\x00")

    def test_weak_batteries_is_a_warning_only(self) -> None:
        self.assertEqual(ptb.error_bits(status_block(err1=0x08)), b"\x80\x00")

    def test_incompatible_tape(self) -> None:
        self.assertEqual(ptb.error_bits(status_block(media=0xFF))[1], 0x20)

    def test_media_name(self) -> None:
        self.assertEqual(ptb.media_name(status_block(width=24)), '24mm(0.94")')
        self.assertEqual(ptb.media_name(status_block(width=12)), '12mm(0.47")')
        self.assertEqual(ptb.media_name(status_block(width=0, media=0)), "")


class SnmpTest(unittest.TestCase):
    preflight: ClassVar[list[tuple[int, ...]]] = [HR_STATUS, HR_ERRORS, MEDIA, COUNTER]

    def test_preflight_read(self) -> None:
        reply = ptb.handle_snmp(request(self.preflight), FakePrinter(status_block(width=12)))
        rid, err, binds = response(reply)
        self.assertEqual((rid, err), (4242, 0))
        self.assertEqual(binds[0], (HR_STATUS, 0x02, b"\x03"))  # idle
        self.assertEqual(binds[1], (HR_ERRORS, 0x04, b"\x00\x00"))
        self.assertEqual(binds[2], (MEDIA, 0x04, b'12mm(0.47")'))
        self.assertEqual(binds[3], (COUNTER, 0x41, b"\x07"))

    def test_printing(self) -> None:
        reply = ptb.handle_snmp(request([HR_STATUS]), FakePrinter(status_block(), printing=True))
        self.assertEqual(response(reply)[2][0][2], b"\x04")

    def test_counter_not_offered_is_no_such_object(self) -> None:
        reply = ptb.handle_snmp(request(self.preflight), FakePrinter(status_block(), counter=False))
        self.assertEqual(response(reply)[2][3][1], 0x80)

    def test_offline_printer_gets_no_answer_for_its_status(self) -> None:
        self.assertIsNone(ptb.handle_snmp(request(self.preflight), FakePrinter(None)))

    def test_identity_answers_without_the_printer(self) -> None:
        reply = ptb.handle_snmp(request([ptb.PRT_SERIAL, ptb.BROTHER_PJL]), FakePrinter(None))
        binds = response(reply)[2]
        self.assertEqual(binds[0][2], b"D5G123456")
        self.assertEqual(binds[1][2], b"MFG:Brother;CMD:PT-CBP;MDL:PT-D600;CLS:PRINTER;")

    def test_wrong_community_gets_no_answer(self) -> None:
        packet = request([HR_STATUS], community=b"private")
        self.assertIsNone(ptb.handle_snmp(packet, FakePrinter(status_block())))

    def test_snmpv1_unknown_oid_is_no_such_name(self) -> None:
        reply = ptb.handle_snmp(
            request([(1, 3, 6, 1, 9, 9)], version=0), FakePrinter(status_block())
        )
        self.assertEqual(response(reply)[1], 2)

    def test_get_next_walks_in_order(self) -> None:
        reply = ptb.handle_snmp(request([ptb.SYS_DESCR], pdu=0xA1), FakePrinter(status_block()))
        self.assertEqual(response(reply)[2][0][0], ptb.SYS_OBJECT_ID)

    def test_malformed_packet_raises_value_error(self) -> None:
        with self.assertRaises((ValueError, IndexError)):
            ptb.handle_snmp(b"\x30\x05\x02\x01", FakePrinter(status_block()))


def raster_job(pages: int) -> bytes:
    """A raster job as ptouch sends it; one raster line carries print-command bytes as data."""
    job = b"\x00" * 200 + b"\x1b@"
    for page in range(pages):
        job += b"\x1bia\x01" + b"\x1biz\x86\x01\x18\x00\x10\x00\x00\x00\x00\x00"
        job += b"\x1biM\x40" + b"\x1biA\x01" + b"\x1biK\x08" + b"\x1bid\x0e\x00" + b"M\x02"
        job += b"G\x03\x00\x02\x1a\x0c" + b"Z" + b"g\x00\x02\x0c\x1a"
        job += b"\x1a" if page == pages - 1 else b"\x0c"
    return job


class CommandStreamTest(unittest.TestCase):
    def test_counts_pages_not_data_bytes(self) -> None:
        stream = ptb.CommandStream()
        stream.feed(raster_job(3))
        self.assertEqual((stream.pages, stream.lost), (3, False))

    def test_chunk_boundaries_do_not_matter(self) -> None:
        job, calls = raster_job(2), []
        stream = ptb.CommandStream(on_page=lambda: calls.append(1))
        for i in range(len(job)):
            stream.feed(job[i : i + 1])
        self.assertEqual((stream.pages, len(calls)), (2, 2))

    def test_status_request_is_no_page(self) -> None:
        stream = ptb.CommandStream()
        stream.feed(b"\x1biS")
        self.assertEqual(stream.pages, 0)

    def test_unknown_command_stops_counting(self) -> None:
        stream = ptb.CommandStream()
        stream.feed(b"\x1b@\x99" + raster_job(1))
        self.assertEqual((stream.pages, stream.lost), (0, True))


class PrinterStateTest(unittest.TestCase):
    def test_completed_pages_count_and_close_the_job(self) -> None:
        printer = ptb.Printer()
        start = printer.count
        printer.page_sent()
        printer.page_sent()
        with printer.cond:
            self.assertTrue(printer._printing_locked())
        printer.on_data(status_block(kind=0x06, phase=0x01) + status_block(kind=0x01, phase=0x01))
        printer.on_data(status_block(kind=0x01, phase=0x01)[:10])  # split across two reads
        printer.on_data(status_block(kind=0x01, phase=0x01)[10:])
        printer.on_data(status_block(kind=0x06, phase=0x00))
        self.assertEqual(printer.count, start + 2)
        with printer.cond:
            self.assertFalse(printer._printing_locked())
        self.assertTrue(printer.counter_seen)

    def test_an_error_ends_the_job(self) -> None:
        printer = ptb.Printer()
        printer.page_sent()
        printer.on_status(status_block(kind=0x02, err2=0x10, width=0, media=0))
        self.assertEqual(printer.outstanding, 0)


if __name__ == "__main__":
    unittest.main()
