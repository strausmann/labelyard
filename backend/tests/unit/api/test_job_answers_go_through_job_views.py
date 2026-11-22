# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Guard: every route builds a job answer through ``app/api/job_views.py`` (ADR 0033).

A job payload can carry an integration's internal base address in its ``qr_payload`` values;
``job_views.job_read`` and ``job_views.redacted`` answer them in web form or as ``null`` to
callers that are not admins. A route that builds ``JobRead`` itself skips that.

Why a source search and not a behaviour test: the behaviour is tested per route in
``tests/integration/api/test_web_url.py``, but only for the routes that exist today; this guard
fails for the next route that builds ``JobRead`` directly. What it cannot do: it matches text,
so a mention in a comment or a string counts as a use, and a ``JobRead`` built under another
name (an alias, ``model_construct``, ``JobRead(**...)``) is not found. The failure names file
and line.
"""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[3] / "app"
ALLOWED = {APP / "api" / "job_views.py"}
PATTERN = re.compile(r"\bJobRead\s*\.\s*model_validate\s*\(|\bJobRead\s*\(")


def test_no_route_builds_a_job_answer_itself() -> None:
    found = []
    for path in sorted((APP / "api").rglob("*.py")):
        if path in ALLOWED:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if PATTERN.search(line):
                found.append(f"{path.relative_to(APP.parent)}:{number}: {line.strip()}")
    assert not found, "build job answers with app.api.job_views.job_read:\n" + "\n".join(found)


def test_the_guard_sees_a_direct_build() -> None:
    assert PATTERN.search("    return JobRead.model_validate(job, from_attributes=True)")
    assert PATTERN.search("jobs=[JobRead.model_validate(j) for j in ordered]")
