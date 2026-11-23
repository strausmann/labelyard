# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Preprint presets: the vendor print on the front of a sheet, where nothing is printed (#246).

The front-side twin of ``backing_designs`` (ADR 0023, D33). ``zones`` lists keep-out rectangles
as ``{"x", "y", "w", "h", "note"}`` in hundredths of a millimetre (D16), in page coordinates
(origin top left). A sheet type that links a preset keeps its zones and its own free (a union).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, String, Uuid
from sqlmodel import Column, Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


class PreprintPreset(SQLModel, table=True):
    __tablename__ = "preprint_presets"
    __table_args__ = (
        CheckConstraint("page_size IN ('A4','Letter')", name="ck_preprint_presets_page_size"),
        CheckConstraint(
            "origin IN ('seed','custom','imported')", name="ck_preprint_presets_origin"
        ),
    )

    id: UUID = Field(default_factory=uuid4, sa_column=Column(Uuid, primary_key=True))
    key: str = Field(sa_column=Column(String, nullable=False, unique=True))
    name: str = Field(sa_column=Column(String, nullable=False))
    page_size: str = Field(sa_column=Column(String, nullable=False))
    zones: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    origin: str = Field(sa_column=Column(String, nullable=False))
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
