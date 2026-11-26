# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""sheet type log

Revision ID: 20261121c2
Revises: 20261121p1
Create Date: 2026-11-21 00:06:00.000000

#351 (design 7.6): a new table ``sheet_type_log`` with the checks, verdicts and changes of each
sheet type. An entry goes with its type (``ON DELETE CASCADE``) and loses its printer when the
printer is deleted (``ON DELETE SET NULL``); the type version, the calibration history entry and
the run are referenced without a foreign key. No existing row is touched; the log starts empty.

The downgrade drops the table (ADR 0034): 1.7.x has no log, and nothing else refers to it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261121c2"
down_revision: str | Sequence[str] | None = "20261121p1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "sheet_type_log",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "type_id",
            sa.Uuid,
            sa.ForeignKey("sheet_types.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type_version_id", sa.Uuid, nullable=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column(
            "printer_id",
            sa.Uuid,
            sa.ForeignKey("printers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tray", sa.String, nullable=True),
        sa.Column("history_id", sa.Uuid, nullable=True),
        sa.Column("run_id", sa.Uuid, nullable=True),
        sa.Column("readings", sa.JSON, nullable=True),
        sa.Column("verdict", sa.String, nullable=False, server_default="none"),
        sa.Column("note", sa.String, nullable=True),
        sa.Column("actor", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('check_print','verified','new_version','catalog_update','note')",
            name="ck_sheet_type_log_kind",
        ),
        sa.CheckConstraint(
            "verdict IN ('fits','sheet_geometry','feed_variance','printer','none')",
            name="ck_sheet_type_log_verdict",
        ),
    )
    op.create_index("ix_sheet_type_log_type_created", "sheet_type_log", ["type_id", "created_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_sheet_type_log_type_created", table_name="sheet_type_log")
    op.drop_table("sheet_type_log")
