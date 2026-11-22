# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The one way a route answers a stored job (ADR 0033).

A job payload carries label data, and its ``qr_payload`` values can name an integration's
internal base address (a tape label, every sheet cell, the sheet ID cell, every listing row).
Every route that answers a job builds it here, so a caller that is not an admin sees those
values in their web form or as ``null`` (``redact_job_payload``). The stored row is untouched.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from app.auth.dependencies import AuthContext
from app.integrations.links import redact_job_payload
from app.schemas.job import JobRead


def redacted(read: JobRead, auth: AuthContext | None, request: Request) -> JobRead:
    """``read`` with its payload as ``auth`` may see it.

    ``None`` (no auth context) only occurs where a test overrides the scope dependency
    (see ``owner_only``); it is treated as a caller that is not an admin.
    """
    caller = auth or AuthContext(source="api-key", scope="read", api_key_id=None, ip="")
    settings = getattr(request.app.state, "settings_store", None)
    payload = redact_job_payload(caller, read.payload, settings)
    return read.model_copy(update={"payload": payload})


def job_read(job: Any, auth: AuthContext | None, request: Request) -> JobRead:
    """A job row answered to ``auth``: :class:`JobRead` with the payload redacted."""
    return redacted(JobRead.model_validate(job, from_attributes=True), auth, request)
