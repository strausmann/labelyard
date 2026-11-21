# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""#405: a failed job stores a fixed text per error code, never the exception's own message.

The message of a backend error can name the printer's address (``PrintFailedError`` wraps a
socket error), and a job row is readable with a read key (#234). The cause goes to the log.
"""

from __future__ import annotations

import logging

import pytest
from app.printer_backends.exceptions import (
    LabelsNotConfirmedError,
    PrinterOfflineError,
    PrintFailedError,
    StatusQueryFailedError,
    TapeMismatchError,
)
from app.services.print_queue import (
    _ERROR_CODE_MAP,
    _PRINTER_ERROR_TEXTS,
    _UNREACHABLE_TEXT,
    _printer_error_to_record,
    fixed_error_text,
)

HOST = "printer.example.invalid"


def test_printer_error_record_has_the_fixed_text(caplog: pytest.LogCaptureFixture) -> None:
    exc = PrintFailedError(f"send to {HOST}:9100 failed: connection reset")
    with caplog.at_level(logging.WARNING, logger="app.services.print_queue"):
        record = _printer_error_to_record(exc)
    assert record == ("print_failed", _PRINTER_ERROR_TEXTS["print_failed"], None)
    assert HOST not in record[1]
    # The cause is not lost: it goes to the hub's own log.
    assert HOST in caplog.text


def test_every_error_code_has_a_fixed_text() -> None:
    """Guard: a new error class mapped to a code must bring its text."""
    missing = sorted(set(_ERROR_CODE_MAP.values()) - set(_PRINTER_ERROR_TEXTS))
    assert missing == []
    assert all(text.strip() for text in _PRINTER_ERROR_TEXTS.values())


def test_the_unreachable_texts_stay_the_same() -> None:
    """The two texts stored since 1.7.x are kept, so rows already written stay fixed texts."""
    for code, text in _UNREACHABLE_TEXT.items():
        assert _PRINTER_ERROR_TEXTS[code] == text
    assert _printer_error_to_record(PrinterOfflineError(f"cannot reach {HOST}"))[1] == (
        "The printer could not be reached."
    )
    assert _printer_error_to_record(StatusQueryFailedError(f"bad reply from {HOST}"))[1] == (
        "The printer's status could not be read."
    )


def test_structured_detail_stays_with_the_record() -> None:
    """The numbers the message used to carry are in the detail."""
    code, text, detail = _printer_error_to_record(TapeMismatchError(expected_mm=12, loaded_mm=9))
    assert (code, text) == ("tape_mismatch", _PRINTER_ERROR_TEXTS["tape_mismatch"])
    assert detail == {"expected_mm": 12, "loaded_mm": 9}
    code, text, detail = _printer_error_to_record(LabelsNotConfirmedError(1, 3))
    assert text == fixed_error_text("labels_not_confirmed")
    assert detail == {"printed": 1, "expected": 3}


def test_an_unknown_code_has_the_generic_text() -> None:
    assert fixed_error_text("some_future_code") == _PRINTER_ERROR_TEXTS["print_failed"]
