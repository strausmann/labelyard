# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""calibration runs, run counters, calibration history and check sheets

Revision ID: 20261121c1
Revises: 20261121a1
Create Date: 2026-11-21 00:04:00.000000

#336, #240 (ADR 0029): five new tables. ``calibration_runs`` numbers every calibration, check and
probe page per printer and tray from ``calibration_run_counters``; ``calibration_history`` holds a
tray's saved corrections; ``calibration_check_sheets`` and ``calibration_check_sheet_passes`` hold
check sheets printed in several passes. Every table goes with its printer (``ON DELETE CASCADE``);
sheet type versions are referenced without a foreign key.

Data: each existing ``printer_calibrations`` row becomes the first history entry of its tray
(``seq`` 1, action ``imported``) with its values, ``measured_on_type_version_id``, ``updated_at``
and ``updated_by``; no readings. Calibration pages printed before have no run, so every counter
starts at 1.

The downgrade drops the five tables; ``printer_calibrations`` is not touched in either direction,
so a 1.7.x hub keeps rendering with the stored correction. Stored page jobs keep their
``detail.run_no``/``time``/``run_id`` keys, which 1.7.x ignores (``detail`` is a plain dict,
ADR 0034).
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261121c1"
down_revision: str | Sequence[str] | None = "20261121a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _printer_id() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "printer_id", sa.Uuid, sa.ForeignKey("printers.id", ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "calibration_runs",
        sa.Column("id", sa.Uuid, primary_key=True),
        _printer_id(),
        sa.Column("tray", sa.String, nullable=False),
        sa.Column("run_no", sa.Integer, nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("job_id", sa.Uuid, nullable=False),
        sa.Column("type_version_id", sa.Uuid, nullable=False),
        sa.Column("correction", sa.JSON, nullable=True),
        sa.Column("check_sheet_id", sa.Uuid, nullable=True),
        sa.Column("pass_no", sa.Integer, nullable=True),
        sa.Column("requested_tray", sa.String, nullable=False),
        sa.Column("actual_media_source", sa.String, nullable=True),
        sa.Column("media_source_reported", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("page_codes", sa.JSON, nullable=True),
        sa.Column("actor", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("printer_id", "tray", "run_no", name="uq_calibration_runs_run_no"),
        sa.CheckConstraint(
            "kind IN ('calibration_page','check_page','probe_page')",
            name="ck_calibration_runs_kind",
        ),
        sa.CheckConstraint("run_no >= 1", name="ck_calibration_runs_run_no"),
    )
    op.create_table(
        "calibration_run_counters",
        sa.Column(
            "printer_id",
            sa.Uuid,
            sa.ForeignKey("printers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tray", sa.String, primary_key=True),
        sa.Column("next_run", sa.Integer, nullable=False),
        sa.CheckConstraint("next_run >= 1", name="ck_calibration_run_counters_next"),
    )
    history = op.create_table(
        "calibration_history",
        sa.Column("id", sa.Uuid, primary_key=True),
        _printer_id(),
        sa.Column("tray", sa.String, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("action", sa.String, nullable=False),
        sa.Column("method", sa.String, nullable=True),
        sa.Column("offset_x_hmm", sa.Integer, nullable=True),
        sa.Column("offset_y_hmm", sa.Integer, nullable=True),
        sa.Column("scale_x_ppm", sa.Integer, nullable=True),
        sa.Column("scale_y_ppm", sa.Integer, nullable=True),
        sa.Column("rotation_udeg", sa.Integer, nullable=True),
        sa.Column("previous", sa.JSON, nullable=True),
        sa.Column("measured_on_type_version_id", sa.Uuid, nullable=True),
        sa.Column("run_id", sa.Uuid, nullable=True),
        sa.Column("source", sa.String, nullable=True),
        sa.Column("readings", sa.JSON, nullable=True),
        sa.Column("residuals", sa.JSON, nullable=True),
        sa.Column("reverted_from_id", sa.Uuid, nullable=True),
        sa.Column("actor", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String, nullable=True),
        sa.UniqueConstraint("printer_id", "tray", "seq", name="uq_calibration_history_seq"),
        sa.CheckConstraint(
            "action IN ('imported','save','revert','reset')",
            name="ck_calibration_history_action",
        ),
    )
    op.create_table(
        "calibration_check_sheets",
        sa.Column("id", sa.Uuid, primary_key=True),
        _printer_id(),
        sa.Column("tray", sa.String, nullable=False),
        sa.Column("type_version_id", sa.Uuid, nullable=False),
        sa.Column("passes_total", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("passes_total >= 1", name="ck_calibration_check_sheets_passes"),
    )
    op.create_table(
        "calibration_check_sheet_passes",
        sa.Column(
            "check_sheet_id",
            sa.Uuid,
            sa.ForeignKey("calibration_check_sheets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("pass_no", sa.Integer, primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid,
            sa.ForeignKey("calibration_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("run_id", name="uq_calibration_check_sheet_passes_run"),
        sa.CheckConstraint("pass_no >= 1", name="ck_calibration_check_sheet_passes_pass"),
    )
    _import_history(history)


def _import_history(history: sa.Table) -> None:
    """One ``imported`` entry (``seq`` 1) per stored calibration, with the row's own values."""
    bind = op.get_bind()
    calibrations = sa.table(
        "printer_calibrations",
        sa.column("printer_id", sa.Uuid),
        sa.column("tray_key", sa.String),
        sa.column("method", sa.String),
        sa.column("offset_x_hmm", sa.Integer),
        sa.column("offset_y_hmm", sa.Integer),
        sa.column("scale_x_ppm", sa.Integer),
        sa.column("scale_y_ppm", sa.Integer),
        sa.column("rotation_udeg", sa.Integer),
        sa.column("measured_on_type_version_id", sa.Uuid),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("updated_by", sa.String),
    )
    rows = bind.execute(sa.select(calibrations)).mappings().all()
    entries = [
        {
            "id": uuid.uuid4(),
            "printer_id": r["printer_id"],
            "tray": r["tray_key"],
            "seq": 1,
            "action": "imported",
            "method": r["method"],
            "offset_x_hmm": r["offset_x_hmm"],
            "offset_y_hmm": r["offset_y_hmm"],
            "scale_x_ppm": r["scale_x_ppm"],
            "scale_y_ppm": r["scale_y_ppm"],
            "rotation_udeg": r["rotation_udeg"],
            "measured_on_type_version_id": r["measured_on_type_version_id"],
            "actor": r["updated_by"],
            "created_at": r["updated_at"],
        }
        for r in rows
    ]
    if entries:
        op.bulk_insert(history, entries)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("calibration_check_sheet_passes")
    op.drop_table("calibration_check_sheets")
    op.drop_table("calibration_history")
    op.drop_table("calibration_run_counters")
    op.drop_table("calibration_runs")
