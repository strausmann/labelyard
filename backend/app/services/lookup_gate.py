# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The bounds on integration lookups that fan out from a single request (263-78, #285, #405).

Tape batch prints, sheet prints and render checks look up many items per request. Their lookups
together run at most ``MAX_BATCH_LOOKUPS`` at once, whatever the number of requests. The
single-item GET lookup routes are not part of it: one request is one lookup there.

The lookups of one request share one deadline (``BatchDeadline``, the setting
``integrations.lookup_batch_deadline_s``, #405): waiting for the shared bound counts against it,
and a lookup not done by then raises ``LookupDeadlineError`` (``lookup-timeout``).
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Awaitable, Callable
from typing import Final

from app.services.errors import LookupDeadlineError

MAX_BATCH_LOOKUPS = 8
"""Integration lookups of fan-out requests that run at once, all requests together."""

_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def lookup_gate() -> asyncio.Semaphore:
    """The process-wide lookup bound. A semaphore belongs to one event loop, so there is one
    per running loop (exactly one in production)."""
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _gates[loop] = asyncio.Semaphore(MAX_BATCH_LOOKUPS)
    return gate


DEFAULT_BATCH_DEADLINE_S: Final = 30.0
"""Default of ``integrations.lookup_batch_deadline_s`` (#405)."""

DeadlineSource = Callable[[], float]
_deadline_source: DeadlineSource | None = None


def bind_batch_deadline(source: DeadlineSource | None) -> DeadlineSource | None:
    """Read the batch deadline from ``source`` (the settings store at startup); returns the
    source it replaces. ``None`` falls back to ``DEFAULT_BATCH_DEADLINE_S``."""
    global _deadline_source
    previous, _deadline_source = _deadline_source, source
    return previous


def batch_deadline_s() -> float:
    """The deadline in seconds over the lookups of one fan-out request."""
    source = _deadline_source
    return DEFAULT_BATCH_DEADLINE_S if source is None else float(source())


class BatchDeadline:
    """One deadline for every lookup of a request, fixed when the request starts looking up."""

    def __init__(self, seconds: float | None = None) -> None:
        loop = asyncio.get_running_loop()
        self._at = loop.time() + (batch_deadline_s() if seconds is None else seconds)

    async def run[T](self, call: Awaitable[T]) -> T:
        """Await ``call`` (the gate wait and the lookup) by the deadline, else
        ``LookupDeadlineError``. A ``TimeoutError`` of the lookup itself passes through."""
        cm = asyncio.timeout_at(self._at)
        try:
            async with cm:
                return await call
        except TimeoutError:
            if cm.expired():
                raise LookupDeadlineError() from None
            raise
