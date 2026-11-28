#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""USB network bridge: makes a USB-only Brother P-touch look like a network P-touch to Labelyard.

Runs on a small Linux machine (for example a Raspberry Pi) the printer is plugged into, and
offers what the hub uses on a network printer:

- **TCP 9100**, the raw print port. One client at a time; the bytes go both ways between the
  client and the printer's ``usblp`` device, status replies included. A plain port forwarder such
  as socat does not work here: the printer answers an empty USB read when it has nothing to say,
  and socat takes that for the end of the connection.
- **UDP 161**, a minimal SNMPv1/v2c agent (Get, GetNext, GetBulk) with the values the hub reads
  before and after every print: ``hrPrinterStatus``, ``hrPrinterDetectedErrorState``,
  ``prtInputMediaName`` (the loaded tape), ``prtMarkerLifeCount`` (the label counter) and the
  Brother PJL id. They come from the printer's own 32-byte status block (``ESC i S``) and from
  the notifications it sends while it prints.

Only the Python standard library is used. Settings are environment variables (``PTB_*``, see
below). Tested with a PT-D600. See https://labelyard.app/install/printers/ for the setup.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Protocol

DEVICE = os.environ.get("PTB_DEVICE", "/dev/usb/lp0")
BIND = os.environ.get("PTB_BIND", "0.0.0.0")  # a network bridge listens on the LAN
RAW_PORT = int(os.environ.get("PTB_RAW_PORT", "9100"))
SNMP_PORT = int(os.environ.get("PTB_SNMP_PORT", "161"))
COMMUNITY = os.environ.get("PTB_COMMUNITY", "public").encode()
NET_IF = os.environ.get("PTB_NET_IF", "wlan0")
# The label counter counts the printer's "printing completed" notifications, one per label.
# auto: offered once the printer has sent one (a printer that never does would make the hub wait
# for labels that never count); on: always; off: never (the hub then counts a sent job as printed).
LABEL_COUNTER = os.environ.get("PTB_LABEL_COUNTER", "auto").strip().lower()
STATE_DIR = os.environ.get("STATE_DIRECTORY", "/var/lib/ptouch-bridge")

RAW_IDLE_TIMEOUT_S = 30.0
# Pages sent but not reported as printed count as "printing"; a page the printer never reports
# (power cut) stops counting after this long without any notification.
PRINTING_STALE_S = 120.0
STATUS_MAX_AGE_S = 1.0
STATUS_TIMEOUT_S = 1.5

log = logging.getLogger("ptouch-bridge")

ESC_I_S = b"\x1biS"
RESET = b"\x00" * 200 + b"\x1b@"
STATUS_HEAD = b"\x80\x20\x42"

# Status block bytes (Brother raster command reference, "status information").
KIND_REPLY, KIND_COMPLETED, KIND_ERROR, KIND_PHASE = 0x00, 0x01, 0x02, 0x06
PHASE_PRINTING = 0x01


# --------------------------------------------------------------------------- raster stream


class CommandStream:
    """Counts the pages of a raster job (its print commands) without changing a byte.

    The raster data of ``G``/``g`` commands is skipped by its length, so a data byte that looks
    like a print command is not counted. On an unknown command the count stops for the rest of
    the job (``lost``).
    """

    _ESC_I_LENGTHS: ClassVar[dict[int, int]] = {
        0x61: 4,
        0x4D: 4,
        0x4B: 4,
        0x41: 4,
        0x21: 4,
        0x53: 3,
        0x7A: 13,
        0x64: 5,
    }

    def __init__(self, on_page: Callable[[], None] | None = None) -> None:
        self._on_page = on_page
        self._buf = b""
        self.lost = False
        self.pages = 0

    def feed(self, data: bytes) -> None:
        if self.lost:
            return
        buf = self._buf + data
        i, n = 0, len(buf)
        while i < n:
            length = self._length(buf, i)
            if length == 0:  # incomplete: wait for more data
                break
            if length < 0:
                log.warning("unknown printer command 0x%02x: pages of this job not counted", buf[i])
                self.lost = True
                self._buf = b""
                return
            if i + length > n:
                break
            if buf[i] in (0x0C, 0x1A):  # print, print and feed: one page
                self.pages += 1
                if self._on_page is not None:
                    self._on_page()
            i += length
        self._buf = buf[i:]

    def _length(self, buf: bytes, i: int) -> int:
        """The command's length at *i*; 0 when more bytes are needed, -1 when it is unknown."""
        n, b = len(buf), buf[i]
        if b in (0x00, 0x5A, 0x0C, 0x1A):  # invalidate, Z (empty line), print, print and feed
            return 1
        if b == 0x4D:  # M: compression mode
            return 2
        if b == 0x1B:
            if i + 2 >= n:
                return 0
            if buf[i + 1] == 0x40:  # ESC @: initialize
                return 2
            if buf[i + 1] == 0x69:  # ESC i ...
                return self._ESC_I_LENGTHS.get(buf[i + 2], -1)
            return -1
        if b == 0x47:  # G n1 n2 data: one compressed raster line
            return 0 if i + 3 > n else 3 + int.from_bytes(buf[i + 1 : i + 3], "little")
        if b == 0x67:  # g 0x00 n data: one uncompressed raster line
            return 0 if i + 3 > n else 3 + buf[i + 2]
        return -1


# --------------------------------------------------------------------------- printer side


class Printer:
    """Sole owner of the usblp device: the kernel lets only one process open it."""

    def __init__(self) -> None:
        self.fd: int | None = None
        self.write_lock = threading.Lock()  # guards self.fd and writes to it
        self.op_lock = threading.Lock()  # a raw session or a status query owns the printer
        self.cond = threading.Condition()  # guards everything below
        self.status: bytes | None = None
        self.status_time = 0.0
        self.status_seq = 0
        self.sink: socket.socket | None = None
        self.session_active = False
        self.outstanding = 0  # pages sent that the printer has not reported as printed
        self.last_note = 0.0
        self.count = int(self._load("label-count") or 0)
        self.counter_seen = self._load("counter-seen") == "1"
        self.product = ""
        self.serial = ""
        self._buf = b""
        self._last_open_err: int | None = None
        self._last_logged: tuple[int, ...] | None = None

    # ---- device

    def _read_identity(self) -> None:
        usbdev = Path(f"/sys/class/usbmisc/{Path(DEVICE).name}/device").resolve().parent
        with contextlib.suppress(OSError):
            self.product = (usbdev / "product").read_text().strip()
            self.serial = (usbdev / "serial").read_text().strip()

    def _ensure_open(self) -> int | None:
        if self.fd is not None:
            return self.fd
        try:
            fd = os.open(DEVICE, os.O_RDWR | os.O_CLOEXEC)
        except OSError as exc:
            if self._last_open_err != exc.errno:
                log.warning("printer not available (%s: %s)", DEVICE, exc.strerror)
                self._last_open_err = exc.errno
            return None
        self._last_open_err = None
        self._read_identity()
        with self.write_lock:
            self.fd = fd
        log.info("printer connected: %s", self.product or DEVICE)
        return fd

    def _close(self) -> None:
        with self.write_lock:
            fd, self.fd = self.fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with self.cond:
            self.status = None
            self.outstanding = 0
            self.cond.notify_all()
        log.warning("printer disconnected")

    @property
    def connected(self) -> bool:
        return self.fd is not None

    def write(self, data: bytes) -> None:
        with self.write_lock:
            if self.fd is None:
                raise OSError(errno.ENODEV, "printer not connected")
            view = memoryview(data)
            while view:
                view = view[os.write(self.fd, view) :]

    def reader_loop(self) -> None:
        while True:
            fd = self._ensure_open()
            if fd is None:
                time.sleep(2)
                continue
            try:
                data = os.read(fd, 1024)
            except OSError as exc:
                log.warning("read from printer failed: %s", exc.strerror)
                self._close()
                time.sleep(1)
                continue
            if not data:
                time.sleep(0.05)  # an empty USB read: the printer has nothing to say
                continue
            self.on_data(data)

    # ---- data from the printer

    def on_data(self, data: bytes) -> None:
        sink = self.sink
        if sink is not None:
            with contextlib.suppress(OSError):
                sink.sendall(data)
        self._buf += data
        while True:
            i = self._buf.find(STATUS_HEAD)
            if i < 0:
                self._buf = self._buf[-2:]
                return
            if len(self._buf) - i < 32:
                self._buf = self._buf[i:]
                return
            block, self._buf = self._buf[i : i + 32], self._buf[i + 32 :]
            self.on_status(block)

    def on_status(self, b: bytes) -> None:
        kind, phase = b[18], b[19]
        now = time.monotonic()
        with self.cond:
            self.status, self.status_time = b, now
            self.status_seq += 1
            if kind != KIND_REPLY:  # a notification the printer sent by itself
                self.last_note = now
            if kind == KIND_COMPLETED:  # one page printed
                self.count += 1
                self._save("label-count", str(self.count))
                if not self.counter_seen:
                    self.counter_seen = True
                    self._save("counter-seen", "1")
                    log.info("the printer reports printed labels: label counter offered")
                self.outstanding = max(0, self.outstanding - 1)
            elif kind == KIND_ERROR:  # the rest of the job will not print
                self.outstanding = 0
            self.cond.notify_all()
        key = (b[8], b[9], b[10], b[11], kind, phase)
        if kind != KIND_REPLY or key != self._last_logged:
            log.info(
                "status: type=%02x phase=%02x err=%02x/%02x tape=%dmm media=%02x count=%d",
                kind,
                phase,
                b[8],
                b[9],
                b[10],
                b[11],
                self.count,
            )
        self._last_logged = key

    # ---- small persistent state

    def _load(self, name: str) -> str:
        try:
            return (Path(STATE_DIR) / name).read_text().strip()
        except OSError:
            return ""

    def _save(self, name: str, value: str) -> None:
        path = Path(STATE_DIR) / name
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(value)
            tmp.replace(path)
        except OSError as exc:
            log.warning("%s not saved: %s", name, exc)

    @property
    def counter_offered(self) -> bool:
        if LABEL_COUNTER == "on":
            return True
        if LABEL_COUNTER == "off":
            return False
        return self.counter_seen

    # ---- status for SNMP

    def _pages_open_locked(self) -> bool:
        if self.outstanding and time.monotonic() - self.last_note > PRINTING_STALE_S:
            log.warning("%d page(s) never reported as printed; no longer waiting", self.outstanding)
            self.outstanding = 0
        return self.outstanding > 0

    def _printing_locked(self) -> bool:
        if self.session_active or self._pages_open_locked():
            return True
        return self.status is not None and self.status[19] == PHASE_PRINTING

    def page_sent(self) -> None:
        with self.cond:
            self.outstanding += 1
            self.last_note = time.monotonic()

    def current_status(self) -> tuple[bytes | None, bool]:
        """(status block, printing). A fresh ``ESC i S``, unless a job is running (the printer
        is then not asked; its notifications keep the block current) or the block is new."""
        if not self.connected:
            return None, False
        with self.cond:
            fresh = (
                self.status is not None and time.monotonic() - self.status_time < STATUS_MAX_AGE_S
            )
            busy = self.session_active or self._pages_open_locked()
            if fresh or busy:
                return self.status, self._printing_locked()
        if not self.op_lock.acquire(blocking=False):
            with self.cond:
                return self.status, True
        try:
            with self.cond:
                seq = self.status_seq
            try:
                self.write(ESC_I_S)
            except OSError:
                return None, False
            deadline = time.monotonic() + STATUS_TIMEOUT_S
            with self.cond:
                while self.status_seq == seq and (left := deadline - time.monotonic()) > 0:
                    self.cond.wait(left)
                if self.status_seq == seq:
                    log.warning("printer did not answer the status request")
                    return None, False
                return self.status, self._printing_locked()
        finally:
            self.op_lock.release()

    # ---- raw sessions

    def handle_raw(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        peer = addr[0]
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(RAW_IDLE_TIMEOUT_S)
        if not self.op_lock.acquire(timeout=60):
            log.warning("%s: printer busy, connection refused", peer)
            conn.close()
            return
        total, last = 0, b""
        stream = CommandStream(on_page=self.page_sent)
        try:
            if not self.connected:
                log.warning("%s: printer not connected, connection closed", peer)
                return
            with self.cond:
                self.session_active, self.sink = True, conn
            while True:
                try:
                    data = conn.recv(65536)
                except TimeoutError:
                    log.warning(
                        "%s: no data for %.0f s, connection closed", peer, RAW_IDLE_TIMEOUT_S
                    )
                    break
                except OSError:
                    break
                if not data:
                    break
                total, last = total + len(data), data[-1:]
                stream.feed(data)
                try:
                    self.write(data)
                except OSError as exc:
                    log.warning("%s: write to printer failed: %s", peer, exc.strerror)
                    break
        finally:
            job = total > 64 and last in (b"\x1a", b"\x0c")
            with self.cond:
                self.sink, self.session_active = None, False
                if job and stream.lost and not stream.pages:
                    self.outstanding = max(self.outstanding, 1)  # pages unknown: at least one
                    self.last_note = time.monotonic()
            if total > 64 and not job:
                # An incomplete job leaves the printer waiting for raster data: reset it.
                log.warning("%s: job ended without a print command, printer reset", peer)
                with contextlib.suppress(OSError):
                    self.write(RESET)
            self.op_lock.release()
            with contextlib.suppress(OSError):
                conn.close()
            if total:
                pages = f"{stream.pages} page(s)" if stream.pages else ("job" if job else "no job")
                log.info("%s: %d bytes sent (%s)", peer, total, pages)


def raw_server(printer: Printer) -> None:
    srv = socket.create_server((BIND, RAW_PORT), backlog=8)
    log.info("raw print port listening on %s:%d", BIND, RAW_PORT)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=printer.handle_raw, args=(conn, addr), daemon=True).start()


# --------------------------------------------------------------------------- SNMP (BER)

OID = tuple[int, ...]


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _len(len(body)) + body


def enc_int(v: int, tag: int = 0x02) -> bytes:
    body = v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big", signed=True)
    while len(body) > 1 and (
        (body[0] == 0x00 and body[1] < 0x80) or (body[0] == 0xFF and body[1] >= 0x80)
    ):
        body = body[1:]
    return tlv(tag, body)


def enc_uint(v: int, tag: int) -> bytes:
    """Counter32 (0x41), Gauge32 (0x42), TimeTicks (0x43): unsigned 32 bit."""
    return enc_int(v & 0xFFFFFFFF, tag)


def enc_str(s: str | bytes) -> bytes:
    return tlv(0x04, s.encode() if isinstance(s, str) else s)


def enc_oid(oid: OID) -> bytes:
    body = bytearray([oid[0] * 40 + oid[1]])
    for n in oid[2:]:
        chunk = [n & 0x7F]
        n >>= 7
        while n:
            chunk.append(0x80 | (n & 0x7F))
            n >>= 7
        body.extend(reversed(chunk))
    return tlv(0x06, bytes(body))


def dec(data: bytes, pos: int) -> tuple[int, bytes, int]:
    """One TLV at *pos*: (tag, value, position after it). ``ValueError`` when malformed."""
    if pos + 2 > len(data):
        raise ValueError("truncated")
    tag, n = data[pos], data[pos + 1]
    pos += 2
    if n & 0x80:
        k = n & 0x7F
        if not 0 < k <= 4:
            raise ValueError("bad length")
        n = int.from_bytes(data[pos : pos + k], "big")
        pos += k
    end = pos + n
    if end > len(data):
        raise ValueError("truncated")
    return tag, data[pos:end], end


def dec_oid(body: bytes) -> OID:
    if not body:
        raise ValueError("empty oid")
    first = min(body[0] // 40, 2)
    oid = [first, body[0] - 40 * first]
    n = 0
    for b in body[1:]:
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            oid.append(n)
            n = 0
    return tuple(oid)


def oid_of(s: str) -> OID:
    return tuple(int(x) for x in s.split("."))


SYS_DESCR = oid_of("1.3.6.1.2.1.1.1.0")
SYS_OBJECT_ID = oid_of("1.3.6.1.2.1.1.2.0")
SYS_UPTIME = oid_of("1.3.6.1.2.1.1.3.0")
SYS_NAME = oid_of("1.3.6.1.2.1.1.5.0")
IF_PHYS_ADDRESS = oid_of("1.3.6.1.2.1.2.2.1.6.1")
HR_DEVICE_DESCR = oid_of("1.3.6.1.2.1.25.3.2.1.3.1")
HR_PRINTER_STATUS = oid_of("1.3.6.1.2.1.25.3.5.1.1.1")
HR_PRINTER_ERROR_STATE = oid_of("1.3.6.1.2.1.25.3.5.1.2.1")
PRT_SERIAL = oid_of("1.3.6.1.2.1.43.5.1.1.17.1")
PRT_INPUT_MEDIA_NAME = oid_of("1.3.6.1.2.1.43.8.2.1.12.1.1")
PRT_MARKER_LIFE_COUNT = oid_of("1.3.6.1.2.1.43.10.2.1.4.1.1")
BROTHER_PJL = oid_of("1.3.6.1.4.1.2435.2.3.9.1.1.7.0")
PRINTER_OIDS = {
    HR_PRINTER_STATUS,
    HR_PRINTER_ERROR_STATE,
    PRT_INPUT_MEDIA_NAME,
    PRT_MARKER_LIFE_COUNT,
}

NO_SUCH_OBJECT = b"\x80\x00"
END_OF_MIB_VIEW = b"\x82\x00"
START = time.monotonic()


def error_bits(b: bytes) -> bytes:
    """The status block's error bytes as RFC 3805 ``hrPrinterDetectedErrorState`` (2 octets)."""
    err1, err2, width, media = b[8], b[9], b[10], b[11]
    o0 = o1 = 0
    if err1 & 0x03 or width == 0 or media == 0x00:  # no media, end of media
        o0 |= 0x40  # noPaper
    if err2 & 0x10:  # cover open
        o0 |= 0x08  # doorOpen
    if err1 & 0x04:  # cutter jam
        o0 |= 0x04  # jammed
    if err1 & 0x80 or err2 & 0xA4:  # fan, overheating, system or transmission error
        o0 |= 0x01  # serviceRequested
    if err2 & 0x01 or media == 0xFF:  # replace media, incompatible tape
        o1 |= 0x20  # markerSupplyMissing
    if err1 & 0x08:  # weak batteries: a warning, it never refuses a job
        o0 |= 0x80  # lowPaper
    return bytes([o0, o1])


def media_name(b: bytes) -> str:
    """``prtInputMediaName`` as Brother network printers report it, e.g. ``12mm(0.47")``."""
    width, media = b[10], b[11]
    if width == 0 or media in (0x00, 0xFF) or b[8] & 0x03:
        return ""
    return f'{width}mm({width / 25.4:.2f}")'


def mac_bytes() -> bytes:
    try:
        return bytes.fromhex(
            Path(f"/sys/class/net/{NET_IF}/address").read_text().strip().replace(":", "")
        )
    except (OSError, ValueError):
        return b""


class StatusSource(Protocol):
    """What ``build_mib`` reads; ``Printer`` provides it (a test passes a stand-in)."""

    product: str
    serial: str
    count: int

    @property
    def counter_offered(self) -> bool: ...

    def current_status(self) -> tuple[bytes | None, bool]: ...


def build_mib(printer: StatusSource, want_printer: bool) -> dict[OID, bytes] | None:
    product = printer.product or "P-touch"
    mib: dict[OID, bytes] = {
        SYS_DESCR: enc_str(f"Brother {product} (USB network bridge)"),
        SYS_OBJECT_ID: enc_oid(oid_of("1.3.6.1.4.1.2435.2.3.9.1")),
        SYS_UPTIME: enc_uint(int((time.monotonic() - START) * 100), 0x43),
        SYS_NAME: enc_str(socket.gethostname()),
        IF_PHYS_ADDRESS: enc_str(mac_bytes()),
        HR_DEVICE_DESCR: enc_str(f"Brother {product}"),
        BROTHER_PJL: enc_str(f"MFG:Brother;CMD:PT-CBP;MDL:{product};CLS:PRINTER;"),
    }
    if printer.serial:
        mib[PRT_SERIAL] = enc_str(printer.serial)
    if want_printer:
        status, printing = printer.current_status()
        if status is None:
            return None  # no answer: the hub reports the printer as offline
        mib[HR_PRINTER_STATUS] = enc_int(4 if printing else 3)  # printing, idle
        mib[HR_PRINTER_ERROR_STATE] = enc_str(error_bits(status))
        mib[PRT_INPUT_MEDIA_NAME] = enc_str(media_name(status))
        if printer.counter_offered:
            mib[PRT_MARKER_LIFE_COUNT] = enc_uint(printer.count, 0x41)
    return mib


def handle_snmp(packet: bytes, printer: StatusSource) -> bytes | None:
    """The response to one SNMP request, or ``None`` to send nothing."""
    tag, msg, _ = dec(packet, 0)
    if tag != 0x30:
        return None
    _, v, p = dec(msg, 0)
    version = int.from_bytes(v, "big", signed=True)
    _, community, p = dec(msg, p)
    if version not in (0, 1) or community != COMMUNITY:
        return None
    pdu_tag, pdu, _ = dec(msg, p)
    if pdu_tag not in (0xA0, 0xA1) and not (pdu_tag == 0xA5 and version == 1):
        return None
    _, rid, q = dec(pdu, 0)
    _, f1, q = dec(pdu, q)
    _, f2, q = dec(pdu, q)
    _, vbl, _ = dec(pdu, q)
    oids: list[OID] = []
    pos = 0
    while pos < len(vbl):
        _, vb, pos = dec(vbl, pos)
        _, ob, _ = dec(vb, 0)
        oids.append(dec_oid(ob))

    getnext = pdu_tag != 0xA0
    mib = build_mib(printer, getnext or any(o in PRINTER_OIDS for o in oids))
    if mib is None:
        return None
    ordered = sorted(mib)

    def next_of(oid: OID) -> tuple[OID, bytes]:
        for cand in ordered:
            if cand > oid:
                return cand, mib[cand]
        return oid, END_OF_MIB_VIEW

    results: list[tuple[OID, bytes]] = []
    err_status = err_index = 0
    if pdu_tag == 0xA5:  # GetBulk
        non_rep = max(0, int.from_bytes(f1, "big", signed=True))
        max_rep = min(max(0, int.from_bytes(f2, "big", signed=True)), 32)
        results.extend(next_of(oid) for oid in oids[:non_rep])
        cursors = oids[non_rep:]
        for _ in range(max_rep if cursors else 0):
            row = [next_of(c) for c in cursors]
            results.extend(row)
            cursors = [r[0] for r in row]
            if all(r[1] == END_OF_MIB_VIEW for r in row):
                break
    else:
        for i, oid in enumerate(oids, 1):
            found = next_of(oid) if getnext else (oid, mib.get(oid, NO_SUCH_OBJECT))
            if version == 0 and found[1] in (NO_SUCH_OBJECT, END_OF_MIB_VIEW):
                err_status, err_index = 2, i  # SNMPv1 noSuchName: the request comes back
                results = [(o, b"\x05\x00") for o in oids]
                break
            results.append(found)

    body = b"".join(tlv(0x30, enc_oid(o) + val) for o, val in results)
    resp = tlv(0x02, rid) + enc_int(err_status) + enc_int(err_index) + tlv(0x30, body)
    return tlv(0x30, enc_int(version) + enc_str(community) + tlv(0xA2, resp))


def snmp_server(printer: Printer) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((BIND, SNMP_PORT))
    log.info("SNMP agent listening on %s:%d/udp", BIND, SNMP_PORT)
    while True:
        packet, addr = sock.recvfrom(4096)
        try:
            reply = handle_snmp(packet, printer)
        except (ValueError, IndexError) as exc:
            log.debug("%s: malformed SNMP packet (%s)", addr[0], exc)
            continue
        if reply is not None:
            sock.sendto(reply, addr)


# --------------------------------------------------------------------------- main


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("PTB_LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s"
    )
    if LABEL_COUNTER not in ("auto", "on", "off"):
        raise SystemExit(f"PTB_LABEL_COUNTER must be auto, on or off, not {LABEL_COUNTER!r}")
    Path(STATE_DIR).mkdir(parents=True, exist_ok=True)
    printer = Printer()
    threading.Thread(target=printer.reader_loop, name="usb-reader", daemon=True).start()
    threading.Thread(target=raw_server, args=(printer,), name="raw", daemon=True).start()
    snmp_server(printer)


if __name__ == "__main__":
    main()
