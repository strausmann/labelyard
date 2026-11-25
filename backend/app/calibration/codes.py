# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The ``LYC1`` run codes printed on calibration and check pages (#240, design 7.2, ADR 0029).

A printed code is a durable format: once a release prints it, later releases must read it.

```
page:  LYC1:<run>:0:<PREFIX>:<version>:<dx_hmm>:<dy_hmm>:<sx_ppm_dev>:<sy_ppm_dev>:<rot_udeg>
           [:K1][:PX<pitch_x_hmm>][:PY<pitch_y_hmm>][:MT<margin_top_hmm>][:ML<margin_left_hmm>]
cell:  LYC1:<run>:<cell>
```

Only the QR alphanumeric characters ``0-9 A-Z : -`` appear. The page codes carry the correction
and the geometry that differs from the type's catalog version; the cell codes carry only the run
and the cell. Page codes are drawn at version 5-M at least as a 10 mm symbol, cell codes at 1-M
as 8 mm. A payload is never truncated: a page code that would need modules below 0.25 mm is
refused with ``calibration_qr_too_large``, a cell code that does not fit is left out (the cell
prints its number only).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final, Literal

import qrcode.constants
import qrcode.exceptions
import qrcode.util
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.catalog.upgrade import CATALOG_ACTORS
from app.models.sheet import SheetTypeVersion
from app.page_render.transform import Deviation
from app.sheets.errors import CalibrationQrTooLargeError
from app.sheets.naming import PREFIX_PATTERN

MAGIC: Final = "LYC1"
PAGE_QR_HMM: Final = 1000
"""Drawn size of a page code (10 mm); its 4-module quiet zone lies outside."""
CELL_QR_HMM: Final = 800
"""Drawn size of a cell code (8 mm), its 4-module quiet zone included."""
MIN_MODULE_HMM: Final = 25
"""Smallest printed module (0.25 mm)."""
PAGE_MIN_VERSION: Final = 5
CELL_VERSION: Final = 1
ECC: Final = qrcode.constants.ERROR_CORRECT_M
QUIET_MODULES: Final = 4
ALNUM: Final = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ:-")
"""The characters a payload may hold (a subset of the QR alphanumeric mode)."""

GeometryField = Literal["PX", "PY", "MT", "ML"]
GEOMETRY_FIELDS: Final[tuple[GeometryField, ...]] = ("PX", "PY", "MT", "ML")
"""The optional geometry fields in grammar order, and the version columns they come from."""
_GEOMETRY_COLUMNS: Final[dict[GeometryField, str]] = {
    "PX": "pitch_x",
    "PY": "pitch_y",
    "MT": "margin_top",
    "ML": "margin_left",
}

AUTO_DETECT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"@"),
    re.compile(r"://"),
    re.compile(r"mailto:", re.IGNORECASE),
    re.compile(r"tel:", re.IGNORECASE),
    re.compile(r"sms:", re.IGNORECASE),
    re.compile(r"wifi:", re.IGNORECASE),
    re.compile(r"geo:", re.IGNORECASE),
    re.compile(r"begin:", re.IGNORECASE),
    re.compile(r"^\+\d"),
)
"""What a scanner acts on by itself (a mail, call, network or contact); no payload may match."""

_PPM: Final = 1_000_000
_MAX_QR_VERSION: Final = 40


def _checked(payload: str) -> str:
    if not set(payload) <= ALNUM:
        raise ValueError("payload outside the QR alphanumeric set")
    return payload


def page_payload(
    *,
    run_no: int,
    prefix: str,
    version: int,
    dev: Deviation,
    check: bool,
    geometry_diff: Mapping[GeometryField, int],
) -> str:
    """The page code of run ``run_no`` printed on sheet type ``prefix`` version ``version``.

    ``dev`` is the correction applied to the page; a page printed without correction passes
    ``IDENTITY``, so every correction field is ``0``. ``check`` adds ``:K1`` (pass 1 of a check
    sheet). ``geometry_diff`` holds the geometry fields that differ from the type's catalog
    version (``catalog_geometry_diff``), printed in grammar order.
    """
    if run_no < 1:
        raise ValueError("run_no must be at least 1")
    if version < 1:
        raise ValueError("version must be at least 1")
    if not PREFIX_PATTERN.fullmatch(prefix):
        raise ValueError("prefix does not match the sheet prefix pattern")
    unknown = set(geometry_diff) - set(GEOMETRY_FIELDS)
    if unknown:
        raise ValueError(f"unknown geometry fields: {sorted(unknown)}")
    fields = [
        MAGIC,
        str(run_no),
        "0",
        prefix,
        str(version),
        str(dev.offset_x_hmm),
        str(dev.offset_y_hmm),
        str(dev.scale_x_ppm - _PPM),
        str(dev.scale_y_ppm - _PPM),
        str(dev.rotation_udeg),
    ]
    if check:
        fields.append("K1")
    fields.extend(
        f"{name}{geometry_diff[name]}" for name in GEOMETRY_FIELDS if name in geometry_diff
    )
    return _checked(":".join(fields))


def cell_payload(run_no: int, cell: int) -> str:
    """The cell code: the run and the cell number (from 1), no pass suffix."""
    if run_no < 1:
        raise ValueError("run_no must be at least 1")
    if cell < 1:
        raise ValueError("cell must be at least 1")
    return _checked(f"{MAGIC}:{run_no}:{cell}")


def _fit_version(payload: str) -> int | None:
    """Smallest QR version holding ``payload`` in alphanumeric mode at ``ECC``; ``None`` past 40."""
    qr = qrcode.QRCode(error_correction=ECC, border=0)
    qr.add_data(qrcode.util.QRData(payload, mode=qrcode.util.MODE_ALPHA_NUM))
    try:
        qr.make(fit=True)
    except (qrcode.exceptions.DataOverflowError, ValueError):
        return None
    return int(qr.version)


def _max_version(symbol_hmm: int, extra_modules: int) -> int:
    """Largest version whose modules stay at ``MIN_MODULE_HMM`` or more (0 when none does)."""
    best = 0
    for v in range(1, _MAX_QR_VERSION + 1):
        if symbol_hmm >= MIN_MODULE_HMM * (17 + 4 * v + extra_modules):
            best = v
    return best


def page_qr_version(payload: str, *, symbol_hmm: int = PAGE_QR_HMM) -> int:
    """The QR version a page code is drawn at: ``PAGE_MIN_VERSION`` or the smallest that fits.

    A version is allowed while its module, ``symbol_hmm`` over the module count, stays at
    ``MIN_MODULE_HMM`` or more; otherwise ``CalibrationQrTooLargeError`` (422). At the drawn
    10 mm that is version 5 only: a page code does not grow, it is refused.
    """
    fit = _fit_version(payload)
    limit = _max_version(symbol_hmm, 0)
    needed = _MAX_QR_VERSION + 1 if fit is None else max(PAGE_MIN_VERSION, fit)
    if needed > limit:
        raise CalibrationQrTooLargeError(len(payload), needed, limit)
    return needed


def cell_qr_version(payload: str, *, symbol_hmm: int = CELL_QR_HMM) -> int | None:
    """The QR version of a cell code (quiet zone inside ``symbol_hmm``); ``None``: number only."""
    fit = _fit_version(payload)
    if fit is None:
        return None
    needed = max(CELL_VERSION, fit)
    return needed if needed <= _max_version(symbol_hmm, 2 * QUIET_MODULES) else None


# ---------------------------------------------------------------------------- geometry reference


def geometry_diff_of(
    current: SheetTypeVersion, reference: SheetTypeVersion | None
) -> dict[GeometryField, int]:
    """The geometry fields of ``current`` that differ from ``reference`` (none without one)."""
    if reference is None or reference.id == current.id:
        return {}
    diff: dict[GeometryField, int] = {}
    for name in GEOMETRY_FIELDS:
        column = _GEOMETRY_COLUMNS[name]
        value = int(getattr(current, column))
        if value != int(getattr(reference, column)):
            diff[name] = value
    return diff


async def catalog_geometry_diff(
    session: AsyncSession, current: SheetTypeVersion
) -> dict[GeometryField, int]:
    """The optional geometry fields of a page code for sheet type version ``current``.

    The reference is the newest version of the type written by the catalog actor
    (``system:catalog`` or the 1.7.x ``system:seed``) in the database, never the entry in the
    image: an image with a newer catalog revision does not make an unedited type look edited
    (design 7.2, S17). A version that is itself catalog-written, and a type without any
    catalog-written version (custom, imported, installed under a new key), give none (S6).
    """
    if current.created_by in CATALOG_ACTORS:
        return {}
    reference = (
        await session.execute(
            select(SheetTypeVersion)
            .where(
                col(SheetTypeVersion.type_id) == current.type_id,
                col(SheetTypeVersion.created_by).in_(sorted(CATALOG_ACTORS)),
            )
            .order_by(col(SheetTypeVersion.version).desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return geometry_diff_of(current, reference)
