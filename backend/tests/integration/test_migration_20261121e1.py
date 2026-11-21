# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Migration E1 (20261121e1, #405): free-text job errors are rewritten to their code's fixed text.

A hub before 1.8.0 stored ``f"{code}: {str(exc)}"`` for a printer error; ``str(exc)`` can name
the printer's address, and the jobs routes hand ``jobs.error`` to every read key. E1 rewrites
such rows once to ``"<code>: <fixed text>"`` (the code stays) and keeps everything that is
already a fixed text, a marker, a class name or a code it does not know. Its downgrade is a
no-op (ADR 0034): the free text is gone and 1.7.x does not need it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, text

_ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
_REV = "20261121e1"
_PREV = "20261118a2"
_PRINTER = "11111111111111111111111111111111"
HOST = "printer.example.invalid"

PRINT_FAILED = "The label could not be printed."
UNREACHABLE = "The printer could not be reached."

# (stored error, expected after E1)
ROWS: dict[str, tuple[str | None, str | None]] = {
    "free_text": (f"print_failed: send to {HOST}:9100 failed", f"print_failed: {PRINT_FAILED}"),
    "free_text_offline": (
        f"printer_offline: cannot reach {HOST}",
        f"printer_offline: {UNREACHABLE}",
    ),
    "fixed_text": (f"print_failed: {PRINT_FAILED}", f"print_failed: {PRINT_FAILED}"),
    "marker_printer_interrupted": ("printer_interrupted", "printer_interrupted"),
    "marker_restart": ("restart_during_inflight", "restart_during_inflight"),
    "marker_shutdown": ("shutdown", "shutdown"),
    "marker_recovery": ("recovery_invalid_page_payload", "recovery_invalid_page_payload"),
    "marker_deferred": ("deferred_interrupted", "deferred_interrupted"),
    "class_name": ("ConnectionRefusedError", "ConnectionRefusedError"),
    "interrupted": ("interrupted: ipp_job 17", "interrupted: ipp_job 17"),
    "not_started": ("not_started: tape_empty", "not_started: tape_empty"),
    "tape_run": (
        "tape_run_interrupted: the hub restarted while this label waited",
        "tape_run_interrupted: the hub restarted while this label waited",
    ),
    "page_fixed": (f"ipp_unreachable: {UNREACHABLE}", f"ipp_unreachable: {UNREACHABLE}"),
    "page_free_text": (
        f"ipp_unreachable: connect to {HOST} failed",
        f"ipp_unreachable: {UNREACHABLE}",
    ),
    "cause_code": ("outcome_unknown: tape_empty", "outcome_unknown: tape_empty"),
    "unknown_code": ("some_future_code: text", "some_future_code: text"),
    "no_error": (None, None),
}
REWRITTEN = {"free_text", "free_text_offline", "page_free_text"}


def _cfg(db: Path) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture
def populated(tmp_path: Path) -> Iterator[tuple[Path, dict[str, str]]]:
    """A database at the revision before E1 with one job per class of error."""
    db = tmp_path / "e1.db"
    command.upgrade(_cfg(db), _PREV)
    ids = {name: uuid4().hex for name in ROWS}
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO printers (id, name, slug, model, backend, connection, enabled, "
                "queue_timeout_s, cut_defaults_half_cut, created_at, updated_at) VALUES "
                f"('{_PRINTER}', 'p', 'p', 'PT-P750W', 'ptouch', '{{}}', 1, 30, 0, "
                "'2026-11-01 00:00:00', '2026-11-01 00:00:00')"
            )
        )
        for name, (error, _) in ROWS.items():
            c.execute(
                text(
                    "INSERT INTO jobs (id, printer_id, state, payload, error, created_at, "
                    "updated_at) VALUES (:id, :p, 'failed', '{}', :e, '2026-11-01 00:00:00', "
                    "'2026-11-01 00:00:00')"
                ),
                {"id": ids[name], "p": _PRINTER, "e": error},
            )
    eng.dispose()
    yield db, ids


def _errors(db: Path, ids: dict[str, str]) -> dict[str, str | None]:
    eng = create_engine(f"sqlite:///{db}")
    try:
        with eng.connect() as c:
            rows = dict(c.execute(text("SELECT id, error FROM jobs")).all())
    finally:
        eng.dispose()
    return {name: rows[job_id] for name, job_id in ids.items()}


@pytest.fixture
def updates() -> Iterator[list[str]]:
    """Every ``UPDATE jobs`` statement any engine sends while the test runs."""
    seen: list[str] = []

    def count(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if statement.lstrip().upper().startswith("UPDATE JOBS"):
            seen.append(statement)

    event.listen(Engine, "before_cursor_execute", count)
    yield seen
    event.remove(Engine, "before_cursor_execute", count)


def _upgraded(populated: tuple[Path, dict[str, str]]) -> dict[str, str | None]:
    db, ids = populated
    command.upgrade(_cfg(db), _REV)
    return _errors(db, ids)


def _expected(*names: str) -> dict[str, str | None]:
    return {n: ROWS[n][1] for n in names}


def test_e1_rewrites_free_text_with_its_code_prefix(populated) -> None:
    after = _upgraded(populated)
    assert {n: after[n] for n in ("free_text", "free_text_offline")} == _expected(
        "free_text", "free_text_offline"
    )
    assert all(HOST not in (v or "") for v in after.values())


def test_e1_keeps_bare_markers(populated) -> None:
    after = _upgraded(populated)
    names = [n for n in ROWS if n.startswith("marker_")]
    assert {n: after[n] for n in names} == _expected(*names)


def test_e1_keeps_interrupted_not_started_and_tape_run_prefixes(populated) -> None:
    after = _upgraded(populated)
    names = ("interrupted", "not_started", "tape_run")
    assert {n: after[n] for n in names} == _expected(*names)


def test_e1_keeps_page_job_fixed_texts(populated) -> None:
    after = _upgraded(populated)
    assert after["page_fixed"] == f"ipp_unreachable: {UNREACHABLE}"
    assert after["fixed_text"] == f"print_failed: {PRINT_FAILED}"


def test_e1_keeps_bare_class_names(populated) -> None:
    assert _upgraded(populated)["class_name"] == "ConnectionRefusedError"


def test_e1_rewrites_page_job_free_text(populated) -> None:
    assert _upgraded(populated)["page_free_text"] == f"ipp_unreachable: {UNREACHABLE}"


def test_e1_keeps_rows_with_unknown_codes(populated) -> None:
    after = _upgraded(populated)
    assert after["unknown_code"] == "some_future_code: text"
    assert after["no_error"] is None


def test_e1_keeps_a_cause_code_after_its_code(populated) -> None:
    """``outcome_unknown: <cause code>`` (a group the printer may have printed) carries no free
    text; its cause stays readable."""
    assert _upgraded(populated)["cause_code"] == "outcome_unknown: tape_empty"


def test_e1_writes_only_changed_rows(populated, updates: list[str]) -> None:
    after = _upgraded(populated)
    assert len(updates) == len(REWRITTEN)
    assert after == {n: v for n, (_, v) in ROWS.items()}


def test_e1_downgrade_on_a_populated_db(populated) -> None:
    db, ids = populated
    cfg = _cfg(db)
    command.upgrade(cfg, _REV)
    scrubbed = _errors(db, ids)
    command.downgrade(cfg, _PREV)
    # The downgrade is a no-op: the free text is gone, every row is kept as E1 left it.
    assert _errors(db, ids) == scrubbed
    command.upgrade(cfg, "head")
    assert _errors(db, ids) == scrubbed
