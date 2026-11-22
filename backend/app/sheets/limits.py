# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The bounds of the sheet archive (#204, spec 8.3): caps on active sheets, the list page, purge.

Shared by the settings registry, the settings store and the sheet service. The caps count active
sheets only, so discarding a sheet frees room; the defaults are far above household use. The
service reads the caps live through ``bind_caps`` (the settings store at startup).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

DEFAULT_MAX_ACTIVE: Final = 1000
"""Default of ``sheets.max_active``: active sheets per hub."""
DEFAULT_MAX_ACTIVE_PER_TYPE: Final = 200
"""Default of ``sheets.max_active_per_type``: active sheets per sheet type."""
MAX_ACTIVE_LIMIT: Final = 10000
"""The largest value either cap accepts (1 is the smallest)."""

LIST_PAGE_MAX: Final = 500
"""``GET /api/v1/sheets`` answers at most this many rows per request, with a ``Link`` to the next
page (ADR 0021, amendment 2026-11-22). Above the per-type default, so an ordinary installation
never sees a second page."""

PURGE_LIMIT_MAX: Final = 1000
"""``POST /api/v1/sheets/purge`` deletes at most this many sheets per call (S5)."""


@dataclass(frozen=True)
class SheetCaps:
    max_active: int = DEFAULT_MAX_ACTIVE
    max_active_per_type: int = DEFAULT_MAX_ACTIVE_PER_TYPE


CapsSource = Callable[[], SheetCaps]
_source: CapsSource | None = None


def bind_caps(source: CapsSource | None) -> CapsSource | None:
    """Read the caps from ``source``; returns the source it replaces. ``None``: the defaults."""
    global _source
    previous, _source = _source, source
    return previous


def caps() -> SheetCaps:
    """The caps in force now."""
    source = _source
    return SheetCaps() if source is None else source()
