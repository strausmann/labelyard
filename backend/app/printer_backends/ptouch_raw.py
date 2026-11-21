# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The raw send of the tape backend (#395): the ``ptouch`` calls that put bytes on the wire.

``_ptouch_print`` and ``_ptouch_print_multi`` are synchronous (the library is) and run inside
``asyncio.to_thread``; ``_ptouch_render`` builds the same raster bytes without sending them, for
the IPP path. ``ptouch_backend`` imports these names into its own namespace and calls them from
there, so a test that patches them on ``ptouch_backend`` still takes effect. All ``ptouch``
connection errors are rewrapped as the hub's printer errors by ``_ptouch_errors``.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

import ptouch
from PIL import Image

from app.printer_backends.exceptions import PrinterOfflineError, PrintFailedError

_logger = logging.getLogger(__name__)

_PTOUCH_PRINTER_CLASSES: dict[str, type] = {
    "PT-P750W": ptouch.PTP750W,
    "PT-E550W": ptouch.PTE550W,
    "PT-P900": ptouch.PTP900,
    "PT-P900W": ptouch.PTP900W,
    "PT-P910BT": ptouch.PTP910BT,
    "PT-P950NW": ptouch.PTP950NW,
}

_PTOUCH_TAPE_CLASSES: dict[int, type] = {
    4: ptouch.Tape3_5mm,  # 3.5mm tape — closest match for "4mm" lookups
    6: ptouch.Tape6mm,
    9: ptouch.Tape9mm,
    12: ptouch.Tape12mm,
    18: ptouch.Tape18mm,
    24: ptouch.Tape24mm,
    36: ptouch.Tape36mm,
}


def _classes() -> tuple[dict[str, type], dict[int, type]]:
    """The class maps as ``ptouch_backend`` sees them at call time.

    The call sites live in ``ptouch_backend`` and so does the namespace tests patch the maps in;
    the lookup goes through it, so a patched map reaches the helpers in this module too.
    Patch these maps on ``ptouch_backend``, not on ``ptouch_raw``.
    """
    from app.printer_backends import ptouch_backend

    # Re-exported there for exactly this lookup (mypy sees the import, not an export).
    return (
        ptouch_backend._PTOUCH_PRINTER_CLASSES,  # type: ignore[attr-defined]
        ptouch_backend._PTOUCH_TAPE_CLASSES,  # type: ignore[attr-defined]
    )


def _ptouch_print(  # pragma: no cover - real-hardware-only, tests monkeypatch this
    host: str,
    port: int,
    image: Image.Image,
    tape_mm: int,
    *,
    model_id: str,
    auto_cut: bool,
    high_resolution: bool,
    half_cut: bool = False,
    last_page: bool = True,
    connection: Any = None,
) -> None:
    """Synchronous helper — module-level so tests can monkeypatch it.

    *connection* (#393): where the bytes go; ``None`` is TCP to *host*:*port*. The IPP path
    passes a ``_CaptureConnection`` and sends what it collected.

    Model-aware: uses the printer class map entry for model_id so the same code
    serves PT-P750W, PT-P900, PT-E550W, etc.

    last_page maps to ptouch-py LabelPrinter.print(feed=last_page):
      - feed=True  (last_page=True, default)  → voller Tape-Feed (~22.5mm Pre-Roll)
      - feed=False (last_page=False)           → minimaler Feed (~5mm) für Batch-Items

    Excluded from coverage: the function only runs against the real ptouch
    library against actual hardware. Every unit test in
    `tests/unit/printer_backends/test_ptouch_backend.py` replaces it via
    monkeypatch on `app.printer_backends.ptouch_backend._ptouch_print`.
    Hardware verification lives in `tests/hardware/` and the manual
    `scripts/smoke_first_print.py`.
    """
    printer_classes, tape_classes = _classes()
    try:
        tape_cls = tape_classes[tape_mm]
    except KeyError as exc:
        raise PrintFailedError(f"No ptouch tape class for {tape_mm}mm") from exc
    try:
        printer_cls = printer_classes[model_id]
    except KeyError as exc:
        raise PrintFailedError(f"No ptouch printer class for model {model_id!r}") from exc
    if connection is None:
        connection = ptouch.ConnectionNetwork(host, port=port, timeout=10.0)
    printer = printer_cls(connection=connection, high_resolution=high_resolution)
    label = ptouch.Label(image=image, tape=tape_cls)
    try:
        printer.print(
            label,
            auto_cut=auto_cut,
            high_resolution=high_resolution,
            half_cut=half_cut,
            feed=last_page,
        )
    except TypeError:
        # Älterer ptouch-Lib (<1.1) unterstützt weder half_cut NOCH feed: beide
        # werden im Fallback verworfen. half_cut → immer Voll-Cut; feed → immer
        # Default True (= 22.5mm Pre-Roll pro Print, kein 5mm-Batch-Half-Cut).
        # Phase 1i Batch-Half-Cut wird auf Legacy-ptouch stillschweigend zur
        # Pre-Phase-1i-Semantik degradiert. (Copilot PR #100)
        printer.print(label, auto_cut=auto_cut, high_resolution=high_resolution)


def _ptouch_print_multi(  # pragma: no cover - real-hardware-only, tests monkeypatch this
    host: str,
    port: int,
    images: list[Image.Image],
    tape_mm: int,
    *,
    model_id: str,
    auto_cut: bool,
    high_resolution: bool,
    half_cut: bool,
    connection: Any = None,
) -> None:
    """Synchronous helper for batch printing via ptouch.LabelPrinter.print_multi.

    *connection* (#393): as in ``_ptouch_print``.

    ptouch-py 1.1.0: LabelPrinter.print_multi(labels, margin_mm=None,
    high_resolution=None, half_cut=True) — 1 Connection, 1 Init (=1 Pre-Roll),
    5mm Half-Cut zwischen Labels, voller Cut am Ende.

    Excluded from coverage: real-hardware-only. Tests monkeypatch this module-level
    function. Hardware verification per scripts/smoke_first_print_batch.py (Task 11).
    """
    printer_classes, tape_classes = _classes()
    try:
        tape_cls = tape_classes[tape_mm]
    except KeyError as exc:
        raise PrintFailedError(f"No ptouch tape class for {tape_mm}mm") from exc
    try:
        printer_cls = printer_classes[model_id]
    except KeyError as exc:
        raise PrintFailedError(f"No ptouch printer class for model {model_id!r}") from exc

    if connection is None:
        connection = ptouch.ConnectionNetwork(host, port=port, timeout=10.0)
    printer = printer_cls(connection=connection, high_resolution=high_resolution)
    labels = [ptouch.Label(image=img, tape=tape_cls) for img in images]
    try:
        printer.print_multi(
            labels,
            high_resolution=high_resolution,
            half_cut=half_cut,
        )
    except TypeError:
        # Älterer ptouch-Lib (<1.1) hat kein print_multi — Fallback: per-Item-Loop
        # mit print(). Degraded zu Phase-1i-Verhalten (22.5mm Pre-Roll pro Item).
        for i, label in enumerate(labels):
            is_last = i == len(labels) - 1
            try:
                printer.print(
                    label,
                    auto_cut=auto_cut,
                    high_resolution=high_resolution,
                    half_cut=half_cut and not is_last,
                    feed=is_last,
                )
            except TypeError:
                printer.print(label, auto_cut=auto_cut, high_resolution=high_resolution)


# The helpers as written, for ``_ptouch_render``: a test that replaces the network helpers keeps
# the real byte building of the IPP path.
_WRITE_ONE = _ptouch_print
_WRITE_MULTI = _ptouch_print_multi


class _CaptureConnection(ptouch.Connection):  # type: ignore[misc]
    """A ptouch connection that sends nothing and keeps every byte written (#393).

    ``ptouch`` builds the raster stream by writing to its connection (the initialisation in the
    printer's constructor, then each label). Collected here, the same bytes the 9100 path sends
    go out as one IPP Print-Job.
    """

    def __init__(self) -> None:
        self.buf = bytearray()

    def connect(self, printer: Any) -> None:  # noqa: ARG002
        return None

    def write(self, payload: bytes) -> None:
        self.buf += payload

    def close(self) -> None:
        return None


def _ptouch_render(
    images: list[Image.Image],
    tape_mm: int,
    *,
    model_id: str,
    auto_cut: bool,
    high_resolution: bool,
    half_cut: bool,
    last_page: bool = True,
    multi: bool,
) -> bytes:
    """The raster bytes ``_ptouch_print`` (one image) or ``_ptouch_print_multi`` would send."""
    capture = _CaptureConnection()
    if multi:
        _WRITE_MULTI(
            "",
            0,
            images,
            tape_mm,
            model_id=model_id,
            auto_cut=auto_cut,
            high_resolution=high_resolution,
            half_cut=half_cut,
            connection=capture,
        )
    else:
        _WRITE_ONE(
            "",
            0,
            images[0],
            tape_mm,
            model_id=model_id,
            auto_cut=auto_cut,
            high_resolution=high_resolution,
            half_cut=half_cut,
            last_page=last_page,
            connection=capture,
        )
    return bytes(capture.buf)


def _require_tape_class(tape_mm: int) -> None:
    """The check ``_ptouch_print_multi`` would fail on, made before anything is sent (#277)."""
    if tape_mm not in _classes()[1]:
        raise PrintFailedError(f"No ptouch tape class for {tape_mm}mm")


_UNREACHABLE_TEXT = "The printer could not be reached."
_WRITE_FAILED_TEXT = "Writing to the printer failed."


@contextlib.contextmanager
def _ptouch_errors() -> Iterator[None]:
    """The ptouch connection errors as the hub's printer errors.

    ptouch's messages name the printer's address (``Connection to printer at <host>:<port>
    timed out``, ``Cannot resolve hostname '<host>'``, ``Failed to write to printer at
    <host>:<port>``, ...) and the job record is readable with a read key (#234): the hub's
    error carries a fixed text, the library's message goes to the server log.
    """
    try:
        yield
    except (ptouch.PrinterWriteError, ptouch.PrinterPermissionError) as exc:
        # These are subclasses of PrinterConnectionError — must be caught first.
        _logger.warning("ptouch write failed (%s): %s", type(exc).__name__, exc)
        raise PrintFailedError(_WRITE_FAILED_TEXT) from exc
    except (
        ptouch.PrinterNetworkError,
        ptouch.PrinterTimeoutError,
        ptouch.PrinterNotFoundError,
        ptouch.PrinterConnectionError,
    ) as exc:
        _logger.warning("ptouch connection failed (%s): %s", type(exc).__name__, exc)
        raise PrinterOfflineError(_UNREACHABLE_TEXT) from exc
