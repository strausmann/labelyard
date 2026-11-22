# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A cell's ``job_id`` opens nothing the key may not read (#204 note 3, 1.8.0).

``GET /api/v1/sheets/{code}`` names the job of a cell; ``GET /api/v1/jobs/{id}`` answers 404 for a
key whose ``allowed_printer_ids`` do not hold the job's printer (``jobs.py:_get_job_or_404``),
exactly like an unknown job. So the ``job_id`` is a pointer, not a grant: no extra gate on the
sheet route.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from app.models.api_key import ApiKey
from app.models.job import Job
from sqlalchemy import select

from tests.integration.api.test_sheet_print_api import _body, _factory, _sheet, running
from tests.integration.api.test_sheet_types_api import _insert_key

pytestmark = pytest.mark.asyncio


async def _own_the_job_with(key: str, job_id: str) -> None:
    """Make ``key`` the owner of the job, so only the printer filter can refuse it."""
    async with _factory()() as s:
        row = (await s.execute(select(ApiKey).where(ApiKey.key_prefix == key[:16]))).scalar_one()
        job = await s.get(Job, UUID(job_id))
        assert job is not None
        job.api_key_id = row.id
        await s.commit()


async def test_restricted_read_key_gets_404_for_a_cell_job_on_another_printer() -> None:
    async with running() as rig:
        code = await _sheet()
        resp = await rig.print(_body(code, 2))
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["pages"][0]["job_id"]
        sheet = await rig.sheet(code)
        assert any(c["job_id"] == job_id for c in sheet["cells"])  # the pointer is visible

        elsewhere = await _insert_key(["read"], allowed_printer_ids=["not-this-printer"])
        await _own_the_job_with(elsewhere, job_id)
        refused = await rig.client.get(
            f"/api/v1/jobs/{job_id}", headers={"X-Label-Hub-Key": elsewhere}
        )
        assert refused.status_code == 404, refused.text

        unknown = await rig.client.get(
            "/api/v1/jobs/00000000-0000-4000-8000-000000000000",
            headers={"X-Label-Hub-Key": elsewhere},
        )
        # the same answer: existence is not disclosed (the text names the asked id only)
        assert refused.json()["detail"].replace(job_id, "<id>") == unknown.json()["detail"].replace(
            "00000000-0000-4000-8000-000000000000", "<id>"
        )

        allowed = await _insert_key(["read"], allowed_printer_ids=[str(rig.printer_id)])
        await _own_the_job_with(allowed, job_id)
        opened = await rig.client.get(
            f"/api/v1/jobs/{job_id}", headers={"X-Label-Hub-Key": allowed}
        )
        assert opened.status_code == 200, opened.text
