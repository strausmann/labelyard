# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""probe_page job kind: the way back removes probe jobs

Revision ID: 20261121p1
Revises: 20261121c1
Create Date: 2026-11-21 00:05:00.000000

#241 (design 7.3, rollback ruling P1): 1.8.0 adds the page job kind ``probe_page``. It needs no
schema change (``jobs.payload`` is JSON, ``calibration_runs.kind`` already allows it), so the
upgrade is a no-op.

The downgrade deletes every ``jobs`` row whose ``payload.kind`` is ``probe_page``: 1.7.x refuses
the kind when it reads a stored page job (its ``PageKind`` does not know it), and a probe page
holds no sheet, reservation or cell, so nothing else refers to it. The runs of kind
``probe_page`` go too (the downgrade of C1 drops the whole table right after). The migration logs
counts, never row contents.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261121p1"
down_revision: str | Sequence[str] | None = "20261121c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_log = logging.getLogger("alembic.runtime.migration")

# Frozen copy of ``app.models.job.json_at("$.kind")`` (1.8.0): SQLite before 3.45 raises on
# ``json_extract`` of an invalid document, so the guard keeps a broken payload from failing the
# downgrade.
_KIND = "CASE WHEN json_valid(payload) THEN json_extract(payload, '$.kind') END"


def upgrade() -> None:
    """No-op: the kind lives in the job payload, which is JSON."""


def downgrade() -> None:
    """Remove the probe jobs (1.7.x refuses their kind) and the probe runs."""
    bind = op.get_bind()
    jobs = bind.execute(sa.text(f"DELETE FROM jobs WHERE {_KIND} = 'probe_page'")).rowcount
    run_rows = bind.execute(
        sa.text("DELETE FROM calibration_runs WHERE kind = 'probe_page'")
    ).rowcount
    _log.info("P1 downgrade: %d probe job(s) and %d probe run(s) removed", jobs, run_rows)
