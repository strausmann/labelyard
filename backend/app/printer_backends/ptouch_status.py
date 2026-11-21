# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The status reads of the tape backend (#395): socket status, SNMP read and preflight.

The functions take the status queries as parameters. ``PTouchBackend`` passes the names from its
own module namespace at call time, so a test that patches ``ptouch_backend.query_preflight``
still takes effect.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.printer_backends.exceptions import PrinterOfflineError, SnmpQueryError
from app.printer_backends.post_send import raise_for_error_flags
from app.printer_backends.snmp_helper import PreflightStatus
from app.printer_backends.tape_confirmation import TapeSendState
from app.services.status_block import StatusBlock

_logger = logging.getLogger(__name__)

_RETRY_BACKOFFS: tuple[float, ...] = (0.0, 1.0, 2.0)


async def query_status_impl(
    host: str,
    port: int,
    *,
    query: Callable[..., Awaitable[StatusBlock]],
) -> StatusBlock:
    """The ESC i S status over the socket, retried while the printer is offline."""
    last_exc: Exception | None = None
    for delay in _RETRY_BACKOFFS:
        if delay:
            _logger.warning("retrying status query in %.1fs", delay)
            await asyncio.sleep(delay)
        try:
            return await query(host, port, timeout_s=5.0)
        except PrinterOfflineError as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


async def read_status_impl(
    host: str,
    community: str,
    timeout_s: float,
    send_state: TapeSendState,
    *,
    query_preflight: Callable[..., Awaitable[PreflightStatus]],
) -> PreflightStatus:
    """One SNMP read: hrPrinterStatus, error bitmap, loaded tape. Never refuses on a bit.

    ESC i S (used by query_status) is unreliable on PT-Series in idle state — SNMP runs in
    parallel on UDP/161 and returns reliably. Raises ``PrinterOfflineError`` when the
    printer does not answer.
    """
    await send_state.flush()  # #398: a record write that did not get through
    asked = send_state.ask_counter()
    try:
        status = await query_preflight(
            host,
            community=community,
            timeout_s=timeout_s,
            # Only passed once it is off: older callers' fakes take no such argument.
            **({} if asked else {"ask_counter": False}),
        )
    except SnmpQueryError as exc:
        raise PrinterOfflineError(f"preflight SNMP failed: {exc}") from exc
    # #393, R26-1: consecutive refusals stop asking for the label counter; an answer turns
    # it back on.
    await send_state.note_counter(
        refused=status.counter_refused, answered=status.marker_count is not None, asked=asked
    )
    return status


async def preflight_check_impl(
    read_status: Callable[[], Awaitable[PreflightStatus]],
) -> PreflightStatus:
    """The status read plus a refusal on every blocking error bit (``post_send``).

    *read_status* is the backend's own method, so a read replaced on the instance is used.
    """
    preflight = await read_status()
    raise_for_error_flags(preflight.error_flags)
    return preflight
