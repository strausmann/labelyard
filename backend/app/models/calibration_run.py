# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Calibration runs, their counters, the calibration history and check sheets (#336, ADR 0029).

A run is one calibration, check or probe page the hub sent; ``run_no`` counts per printer and tray
from a counter row (``calibration_run_counters``), never from ``MAX(run_no)``, so a number is not
handed out twice after a failed submit or after retention dropped old runs. Every table goes with
its printer (``ON DELETE CASCADE``). Sheet type versions are referenced without a foreign key: a
deleted type leaves runs, history and check sheets readable.

Values follow ``printer_calibrations``: offsets in hundredths of a millimetre (D16), scale in parts
per million, rotation in micro-degrees.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlmodel import Column, Field, SQLModel

RUN_KINDS = ("calibration_page", "check_page", "probe_page")
HISTORY_ACTIONS = ("imported", "save", "revert", "reset")


def _now() -> datetime:
    return datetime.now(UTC)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({','.join(repr(v) for v in values)})"


def _printer_fk() -> ForeignKey:
    return ForeignKey("printers.id", ondelete="CASCADE")


class CalibrationRun(SQLModel, table=True):
    __tablename__ = "calibration_runs"
    __table_args__ = (
        UniqueConstraint("printer_id", "tray", "run_no", name="uq_calibration_runs_run_no"),
        CheckConstraint(_in("kind", RUN_KINDS), name="ck_calibration_runs_kind"),
        CheckConstraint("run_no >= 1", name="ck_calibration_runs_run_no"),
    )

    id: UUID = Field(default_factory=uuid4, sa_column=Column(Uuid, primary_key=True))
    printer_id: UUID = Field(sa_column=Column(Uuid, _printer_fk(), nullable=False))
    tray: str = Field(sa_column=Column(String, nullable=False))
    run_no: int = Field(sa_column=Column(Integer, nullable=False))
    kind: str = Field(sa_column=Column(String, nullable=False))
    job_id: UUID = Field(sa_column=Column(Uuid, nullable=False))
    """The job of the first submit; no foreign key, job rows are cleaned up after retention."""
    type_version_id: UUID = Field(sa_column=Column(Uuid, nullable=False))
    correction: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    """The correction applied (the payload's calibration snapshot); null for a calibration page."""
    check_sheet_id: UUID | None = Field(default=None, sa_column=Column(Uuid, nullable=True))
    pass_no: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    requested_tray: str = Field(sa_column=Column(String, nullable=False))
    actual_media_source: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    """The source the printer reported, only when it differs from the request (bounded text)."""
    media_source_reported: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default="0")
    )
    page_codes: list[Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    """Positions of the drawn page codes (hundredths of a millimetre), for the photo reader."""
    actor: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class CalibrationRunCounter(SQLModel, table=True):
    __tablename__ = "calibration_run_counters"
    __table_args__ = (CheckConstraint("next_run >= 1", name="ck_calibration_run_counters_next"),)

    printer_id: UUID = Field(sa_column=Column(Uuid, _printer_fk(), primary_key=True))
    tray: str = Field(sa_column=Column(String, primary_key=True))
    next_run: int = Field(sa_column=Column(Integer, nullable=False))


class CalibrationHistory(SQLModel, table=True):
    __tablename__ = "calibration_history"
    __table_args__ = (
        UniqueConstraint("printer_id", "tray", "seq", name="uq_calibration_history_seq"),
        CheckConstraint(_in("action", HISTORY_ACTIONS), name="ck_calibration_history_action"),
    )

    id: UUID = Field(default_factory=uuid4, sa_column=Column(Uuid, primary_key=True))
    printer_id: UUID = Field(sa_column=Column(Uuid, _printer_fk(), nullable=False))
    tray: str = Field(sa_column=Column(String, nullable=False))
    seq: int = Field(sa_column=Column(Integer, nullable=False))
    action: str = Field(sa_column=Column(String, nullable=False))
    method: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    """Null for a ``reset`` (no calibration after it)."""
    offset_x_hmm: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    offset_y_hmm: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    scale_x_ppm: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    scale_y_ppm: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    rotation_udeg: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    previous: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    """The values before this entry; null for the first entry of a tray."""
    measured_on_type_version_id: UUID | None = Field(
        default=None, sa_column=Column(Uuid, nullable=True)
    )
    run_id: UUID | None = Field(default=None, sa_column=Column(Uuid, nullable=True))
    source: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    readings: list[Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    residuals: list[Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    reverted_from_id: UUID | None = Field(default=None, sa_column=Column(Uuid, nullable=True))
    actor: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    note: str | None = Field(default=None, sa_column=Column(String, nullable=True))


class CalibrationCheckSheet(SQLModel, table=True):
    __tablename__ = "calibration_check_sheets"
    __table_args__ = (
        CheckConstraint("passes_total >= 1", name="ck_calibration_check_sheets_passes"),
    )

    id: UUID = Field(default_factory=uuid4, sa_column=Column(Uuid, primary_key=True))
    printer_id: UUID = Field(sa_column=Column(Uuid, _printer_fk(), nullable=False))
    tray: str = Field(sa_column=Column(String, nullable=False))
    type_version_id: UUID = Field(sa_column=Column(Uuid, nullable=False))
    passes_total: int = Field(sa_column=Column(Integer, nullable=False))
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    closed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class CalibrationCheckSheetPass(SQLModel, table=True):
    """One pass of a check sheet and its run; the primary key keeps a pass number unique per
    sheet, ``uq_calibration_check_sheet_passes_run`` one pass per run."""

    __tablename__ = "calibration_check_sheet_passes"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_calibration_check_sheet_passes_run"),
        CheckConstraint("pass_no >= 1", name="ck_calibration_check_sheet_passes_pass"),
    )

    check_sheet_id: UUID = Field(
        sa_column=Column(
            Uuid,
            ForeignKey("calibration_check_sheets.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )
    pass_no: int = Field(sa_column=Column(Integer, primary_key=True))
    run_id: UUID = Field(
        sa_column=Column(
            Uuid, ForeignKey("calibration_runs.id", ondelete="CASCADE"), nullable=False
        )
    )
