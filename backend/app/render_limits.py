# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The bounds of ``render.max_concurrent`` (#405), shared by the settings, the settings store and
``app.services.render_slots`` (which imports ``app.config`` and so cannot be imported by it)."""

from __future__ import annotations

from typing import Final

DEFAULT_MAX_CONCURRENT_RENDERS: Final = 2
"""Default of ``render.max_concurrent``: renders at once, process-wide (spec 18, CPU)."""
MIN_CONCURRENT_RENDERS: Final = 2
"""The fewest slots: an owner never holds the last one, so one has to be left."""
MAX_CONCURRENT_RENDERS_LIMIT: Final = 16
"""The most slots the setting allows."""
