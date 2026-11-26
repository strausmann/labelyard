# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The sheet-type log: checks, verdicts and changes per sheet type (#351, design 7.6).

An entry belongs to its type (``ON DELETE CASCADE``). It may name a printer and tray; a deleted
printer leaves the entry with ``printer_id`` null (``SET NULL``, design 7.8). The type version,
the calibration history entry in effect and the run are referenced without a foreign key: the
entry stays readable when they are gone. The log names devices and local history, so it never
leaves the hub: not in an export, the catalog or a feedback issue.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, Uuid
from sqlmodel import Column, Field, SQLModel

LOG_KINDS = ("check_print", "verified", "new_version", "catalog_update", "note")
LOG_VERDICTS = ("fits", "sheet_geometry", "feed_variance", "printer", "none")


def _now() -> datetime:
    return datetime.now(UTC)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({','.join(repr(v) for v in values)})"


class SheetTypeLog(SQLModel, table=True):
    __tablename__ = "sheet_type_log"
    __table_args__ = (
        CheckConstraint(_in("kind", LOG_KINDS), name="ck_sheet_type_log_kind"),
        CheckConstraint(_in("verdict", LOG_VERDICTS), name="ck_sheet_type_log_verdict"),
        Index("ix_sheet_type_log_type_created", "type_id", "created_at"),
    )

    id: UUID = Field(default_factory=uuid4, sa_column=Column(Uuid, primary_key=True))
    type_id: UUID = Field(
        sa_column=Column(Uuid, ForeignKey("sheet_types.id", ondelete="CASCADE"), nullable=False)
    )
    type_version_id: UUID | None = Field(default=None, sa_column=Column(Uuid, nullable=True))
    kind: str = Field(sa_column=Column(String, nullable=False))
    printer_id: UUID | None = Field(
        default=None,
        sa_column=Column(Uuid, ForeignKey("printers.id", ondelete="SET NULL"), nullable=True),
    )
    tray: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    history_id: UUID | None = Field(default=None, sa_column=Column(Uuid, nullable=True))
    """The calibration history entry in effect (no foreign key: history retention drops it)."""
    run_id: UUID | None = Field(default=None, sa_column=Column(Uuid, nullable=True))
    """The calibration run the entry is about (no foreign key, as ``history_id``)."""
    readings: list[Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    """Per crossing: ``col_gap``, ``row_gap``, ``across_mm``, ``down_mm``, ``residual_mm``."""
    verdict: str = Field(
        default="none", sa_column=Column(String, nullable=False, server_default="none")
    )
    note: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    actor: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
