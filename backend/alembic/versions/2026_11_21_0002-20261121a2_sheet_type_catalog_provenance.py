# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""catalog provenance of an installed sheet type

Revision ID: 20261121a2
Revises: 20261121e1
Create Date: 2026-11-21 00:02:00.000000

#335 (ADR 0028): a sheet type remembers which catalog entry it is a copy of. ``catalog_key`` is
the entry's key, ``catalog_revision`` the installed revision and ``catalog_fields_hash`` the
fields hash of the entry's type fields at install. All three are ``NULL`` for every existing
type: the startup step ``app.catalog.upgrade.run_catalog_migration`` stamps the seeded types on
the first start and records that in the ``app_settings`` row ``internal.catalog.migrated``.

The downgrade drops the columns and deletes that row, so an upgrade after a downgrade stamps the
types again instead of finding a marker without the columns it describes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261121a2"
down_revision: str | Sequence[str] | None = "20261121e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MARKER = "internal.catalog.migrated"  # frozen copy of app.catalog.upgrade.MIGRATED_KEY


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("sheet_types") as batch:
        batch.add_column(sa.Column("catalog_key", sa.String(), nullable=True))
        batch.add_column(sa.Column("catalog_revision", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("catalog_fields_hash", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("sheet_types") as batch:
        batch.drop_column("catalog_fields_hash")
        batch.drop_column("catalog_revision")
        batch.drop_column("catalog_key")
    op.execute(sa.text("DELETE FROM app_settings WHERE key = :key").bindparams(key=_MARKER))
