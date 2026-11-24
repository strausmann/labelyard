# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""preprint keep-out zones, presets and the position of the margin sheet code

Revision ID: 20261121a1
Revises: 20261121a2
Create Date: 2026-11-21 00:03:00.000000

#246 (ADR 0023, D33): a new table ``preprint_presets`` (the front-side twin of
``backing_designs``) and five columns on ``sheet_types``: ``id_margin_band`` (``auto``),
``id_margin_align`` (``start``), ``id_margin_offset_hmm`` (0), ``preprint_zones`` (``[]``) and
``preprint_preset_id``. Every existing type gets the defaults, which place the margin code as
1.7.x did. ``sheets.margin_id_rect`` and ``sheets.feed_mark_rect`` record where a printed
margin code and feed mark sit, so what is printed later keeps them free (``NULL`` for prints
made before).

The downgrade drops the columns and the table; a 1.7.x hub then places the margin code by its
band rule again. Stored page jobs keep their ``margin_id_rect``/``feed_mark_end`` keys, which
1.7.x ignores (``SheetPageDetail`` has ``extra="ignore"``, ADR 0034).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261121a1"
down_revision: str | Sequence[str] | None = "20261121a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "preprint_presets",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("key", sa.String, nullable=False, unique=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("page_size", sa.String, nullable=False),
        sa.Column("zones", sa.JSON, nullable=False),
        sa.Column("origin", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("page_size IN ('A4','Letter')", name="ck_preprint_presets_page_size"),
        sa.CheckConstraint(
            "origin IN ('seed','custom','imported')", name="ck_preprint_presets_origin"
        ),
    )
    with op.batch_alter_table("sheet_types") as batch:
        batch.add_column(
            sa.Column("id_margin_band", sa.String, nullable=False, server_default="auto")
        )
        batch.add_column(
            sa.Column("id_margin_align", sa.String, nullable=False, server_default="start")
        )
        batch.add_column(
            sa.Column("id_margin_offset_hmm", sa.Integer, nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("preprint_zones", sa.JSON, nullable=False, server_default="[]"))
        batch.add_column(sa.Column("preprint_preset_id", sa.Uuid, nullable=True))
        batch.create_foreign_key(
            "fk_sheet_types_preprint_preset_id", "preprint_presets", ["preprint_preset_id"], ["id"]
        )
        batch.create_check_constraint(
            "ck_sheet_types_id_margin_band",
            "id_margin_band IN ('auto','top','bottom','left','right')",
        )
        batch.create_check_constraint(
            "ck_sheet_types_id_margin_align", "id_margin_align IN ('start','end')"
        )
        batch.create_check_constraint(
            "ck_sheet_types_id_margin_offset", "id_margin_offset_hmm >= 0"
        )
    with op.batch_alter_table("sheets") as batch:
        batch.add_column(sa.Column("margin_id_rect", sa.JSON, nullable=True))
        batch.add_column(sa.Column("feed_mark_rect", sa.JSON, nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("sheets") as batch:
        batch.drop_column("feed_mark_rect")
        batch.drop_column("margin_id_rect")
    with op.batch_alter_table("sheet_types") as batch:
        batch.drop_constraint("ck_sheet_types_id_margin_offset", type_="check")
        batch.drop_constraint("ck_sheet_types_id_margin_align", type_="check")
        batch.drop_constraint("ck_sheet_types_id_margin_band", type_="check")
        batch.drop_constraint("fk_sheet_types_preprint_preset_id", type_="foreignkey")
        batch.drop_column("preprint_preset_id")
        batch.drop_column("preprint_zones")
        batch.drop_column("id_margin_offset_hmm")
        batch.drop_column("id_margin_align")
        batch.drop_column("id_margin_band")
    op.drop_table("preprint_presets")
