# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""free-text job errors become the fixed text of their code

Revision ID: 20261121e1
Revises: 20261118a2
Create Date: 2026-11-21 00:01:00.000000

#405: a hub before 1.8.0 stored ``f"{code}: {str(exc)}"`` for a printer error, and ``str(exc)``
can name the printer's address (a socket error); the jobs routes hand ``jobs.error`` to every
read key. 1.8.0 stores a fixed text per code (``print_queue._PRINTER_ERROR_TEXTS``); this
migration rewrites the rows an older hub left behind, once.

Per ``jobs.error`` row, in this order:

* **kept**: a bare marker of ``_MARKERS``, or a bare class name (``^[A-Z][A-Za-z0-9]*$``);
* **kept**: it starts with ``interrupted:``, ``not_started:`` or ``tape_run_interrupted:``;
* **kept**: ``<code>: <rest>`` where ``<rest>`` is a text of one of the frozen tables below
  (the queue's, the page runner's and the deferred tape label's), or a cause code
  (``_CAUSE_CODES``: ``outcome_unknown: tape_empty`` names the cause of a group the printer may
  have printed, no free text);
* **rewritten**: ``<code>: <free text>`` with ``<code>`` a code of ``_PRINTER_ERROR_TEXTS`` (to
  that text) or of ``_PAGE_MESSAGES`` (to that text); the code prefix stays;
* **kept**: everything else, ``<unknown code>: <text>`` included.

Only rows classified as rewritten are written (one ``UPDATE`` each). The migration logs counts,
never row contents. The downgrade is a no-op (ADR 0034): the scrub removed only free text, which
cannot be restored and which 1.7.x does not need.

The tables (``_PRINTER_ERROR_TEXTS``, ``_UNREACHABLE_TEXT``, ``_PAGE_MESSAGES``,
``_DEFERRED_FAILURE_MESSAGES`` with its fallback) are frozen copies of the code at this
revision, not imports: a later change of the live tables must not change what this migration
does.
"""

import logging
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261121e1"
down_revision: str | Sequence[str] | None = "20261118a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_log = logging.getLogger("alembic.runtime.migration")

# Frozen copy of ``app.services.print_queue._PRINTER_ERROR_TEXTS`` (1.8.0).
_PRINTER_ERROR_TEXTS: dict[str, str] = {
    "tape_mismatch": "The loaded tape does not match the tape the label needs.",
    "tape_empty": "The printer has no tape left.",
    "printer_cover_open": "The printer's cover is open.",
    "printer_offline": "The printer could not be reached.",
    "printer_status_unavailable": "The printer's status could not be read.",
    "print_failed": "The label could not be printed.",
    "media_type_mismatch": "The loaded roll is die-cut labels; the hub prints on continuous "
    "tape only.",
    "printer_error": "The printer reports an error state.",
    "labels_not_confirmed": "The printer did not confirm every label that was sent.",
    "outcome_unknown": "The printer did not confirm the labels in time and may still print "
    "them; check the printer before you reprint.",
    "tape_label_too_long": "The label is longer than this tape allows.",
}

# Frozen copy of ``app.services.print_queue._UNREACHABLE_TEXT`` (1.7.x and 1.8.0).
_UNREACHABLE_TEXT: dict[str, str] = {
    "printer_offline": "The printer could not be reached.",
    "printer_status_unavailable": "The printer's status could not be read.",
}

# Frozen copy of ``app.services.page_job_runner._MESSAGES`` (1.7.x and 1.8.0).
_PAGE_MESSAGES: dict[str, str] = {
    "render_failed": "The page could not be drawn.",
    "render_timeout": "The page took too long to draw.",
    "render_unavailable": "No render slot became free in time; the page was not drawn.",
    "ipp_unreachable": "The printer could not be reached.",
    "ipp_busy": "The printer was busy and did not take the job.",
    "ipp_rejected": "The printer refused the document.",
    "ipp_request_invalid": "The print request could not be built.",
    "interrupted": "The printer may have printed the page; its outcome is unknown.",
    "ipp_aborted": "The printer aborted the job before printing.",
    "printer-identity-mismatch": "The device behind this printer changed; the job was not printed.",
}

# Frozen copy of ``app.services.print_service._DEFERRED_FAILURE_MESSAGES`` and its fallback
# (1.7.x and 1.8.0): a deferred tape label stores ``<code>: <its text>``, a fixed text too.
_DEFERRED_FAILURE_MESSAGES: dict[str, str] = {
    "printer_disabled": "The printer is disabled.",
    "printer_offline": "The printer could not be reached.",
    "printer_cover_open": "The printer cover is open.",
    "no_tape_loaded": "No tape is loaded in the printer.",
    "tape_empty": "The tape is empty.",
    "tape_mismatch": "The loaded tape does not fit the label.",
    "media_type_mismatch": "The loaded roll is die-cut labels; only continuous tape prints.",
    "printer_error": "The printer reports an error state.",
    "unsupported_tape": "The loaded tape is not supported.",
    "content_type_data_mismatch": "The label data does not fit the content type.",
    "qr_payload_too_long": "The QR code content is too long for the loaded tape.",
    "tape_label_too_long": "The label is longer than the loaded tape allows.",
    "snmp_query_failed": "The printer did not answer the status query.",
}
_DEFERRED_FAILURE_FALLBACK = "The label could not be printed."

_MARKERS: frozenset[str] = frozenset(
    {
        "printer_interrupted",
        "restart_during_inflight",
        "shutdown",
        "recovery_invalid_page_payload",
        "deferred_interrupted",
    }
)
_KEPT_PREFIXES: tuple[str, ...] = ("interrupted:", "not_started:", "tape_run_interrupted:")
_FIXED_TEXTS: frozenset[str] = frozenset(
    {
        *_PRINTER_ERROR_TEXTS.values(),
        *_UNREACHABLE_TEXT.values(),
        *_PAGE_MESSAGES.values(),
        *_DEFERRED_FAILURE_MESSAGES.values(),
        _DEFERRED_FAILURE_FALLBACK,
    }
)
_CAUSE_CODES: frozenset[str] = frozenset({*_PRINTER_ERROR_TEXTS, "batch_failed"})
"""What ``print_queue`` writes after ``outcome_unknown:`` (the error code of the cause)."""
_CLASS_NAME = re.compile(r"[A-Z][A-Za-z0-9]*")
_CODED = re.compile(r"([a-z][a-z0-9_-]*): (.*)", re.DOTALL)


def scrubbed(error: str) -> str | None:
    """The fixed text ``error`` is rewritten to, or ``None`` when the row is kept."""
    if error in _MARKERS or _CLASS_NAME.fullmatch(error):
        return None
    if error.startswith(_KEPT_PREFIXES):
        return None
    m = _CODED.fullmatch(error)
    if m is None:
        return None
    code, rest = m.group(1), m.group(2)
    if rest in _FIXED_TEXTS or rest in _CAUSE_CODES:
        return None
    if code in _PRINTER_ERROR_TEXTS:
        return f"{code}: {_PRINTER_ERROR_TEXTS[code]}"
    if code in _PAGE_MESSAGES:
        return f"{code}: {_PAGE_MESSAGES[code]}"
    return None


def upgrade() -> None:
    """Rewrite the free-text job errors; the counts go to the log, never the contents."""
    bind = op.get_bind()
    jobs = sa.table("jobs", sa.column("id"), sa.column("error"))
    rows = bind.execute(sa.select(jobs.c.id, jobs.c.error).where(jobs.c.error.is_not(None)))
    rewrites = [(job_id, new) for job_id, error in rows if (new := scrubbed(error)) is not None]
    for job_id, new in rewrites:
        bind.execute(sa.update(jobs).where(jobs.c.id == job_id).values(error=new))
    _log.info("E1: %d free-text job error(s) rewritten to the fixed text", len(rewrites))


def downgrade() -> None:
    """No-op (ADR 0034): the upgrade removed only free text, which cannot be restored."""
