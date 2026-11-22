# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The public web address through the real app: lookup link, setting, job answers (#247).

ADR 0033. Harness: ``tests/helpers/web_url_hub.py`` (real plugins, ``respx`` upstreams, a mock tape
printer). Every address is an invented ``example.invalid`` host.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import app.models  # noqa: F401
import httpx
import pytest
from app.models.job import Job as DbJob
from httpx import AsyncClient
from sqlalchemy import update

from tests.helpers.web_url_hub import (
    GROCY,
    INTERNAL,
    ITEM,
    PATH,
    SNIPE,
    WEB,
    Hub,
    Keys,
    assert_no_internal,
    h,
    hub,
)

pytestmark = pytest.mark.asyncio
__all__ = ["hub"]


# -- lookup link ------------------------------------------------------------------------


async def test_lookup_url_is_null_without_web_url(hub: Hub) -> None:
    async with hub.running() as (client, keys):
        r = await client.get("/api/v1/lookup/snipeit/A-1", headers=h(keys.read))
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Laptop Nine"
    assert r.json()["url"] is None


async def test_lookup_url_uses_web_url(hub: Hub) -> None:
    hub.web("snipeit", "grocy", "spoolman")
    async with hub.running() as (client, keys):
        for app_name in INTERNAL:
            r = await client.get(
                f"/api/v1/lookup/{app_name}/{ITEM[app_name]}", headers=h(keys.read)
            )
            assert r.status_code == 200, r.text
            assert r.json()["url"] == WEB[app_name].rstrip("/") + PATH[app_name]


async def test_web_url_env_locked_cannot_be_changed_through_the_api(hub: Hub) -> None:
    hub.web("snipeit")
    async with hub.running() as (client, keys):
        view = await client.get("/api/v1/settings", headers=h(keys.admin))
        assert view.status_code == 200, view.text
        row = next(s for s in view.json() if s["key"] == "integrations.snipeit.web_url")
        assert row["locked"] is True
        r = await client.put(
            "/api/v1/settings",
            json={"values": {"integrations.snipeit.web_url": "https://other.example.invalid"}},
            headers=h(keys.admin),
        )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error_code"] == "setting_locked"


async def test_web_url_can_be_set_and_refuses_user_info(hub: Hub) -> None:
    async with hub.running() as (client, keys):
        bad = await client.put(
            "/api/v1/settings",
            json={"values": {"integrations.snipeit.web_url": "https://u:pw@inv.example.invalid"}},
            headers=h(keys.admin),
        )
        assert bad.status_code == 422, bad.text
        assert "pw@" not in bad.text
        ok = await client.put(
            "/api/v1/settings",
            json={"values": {"integrations.snipeit.web_url": WEB["snipeit"]}},
            headers=h(keys.admin),
        )
        assert ok.status_code == 200, ok.text
        r = await client.get("/api/v1/lookup/snipeit/A-1", headers=h(keys.read))
    assert r.json()["url"] == WEB["snipeit"] + PATH["snipeit"]


# -- job answers ------------------------------------------------------------------------


async def _sheet_job(printer_id: UUID, owner: UUID) -> UUID:
    """A stored sheet job whose cells and ID cell carry internal addresses (state done)."""
    import app.db.engine as eng

    job_id = uuid4()

    def cell(n: int, app_name: str) -> dict[str, Any]:
        return {
            "cell": n,
            "content_type": "qr_two_lines",
            "label_data": {
                "source_app": app_name,
                "title": "Cell",
                "primary_id": str(n),
                "qr_payload": INTERNAL[app_name] + PATH[app_name],
                "items": [],
            },
        }

    payload = {
        "kind": "sheet_page",
        "media": "A4",
        "tray": "auto",
        "render_dpi": 600,
        "job_name": "Sheet · test",
        "calibration": None,
        "detail": {"cells": [cell(1, "grocy"), cell(2, "spoolman")], "id_cell": cell(3, "snipeit")},
    }
    async with eng.async_session() as s:
        s.add(
            DbJob(
                id=job_id,
                printer_id=printer_id,
                template_key=None,
                state="done",
                payload=payload,
                api_key_id=owner,
            )
        )
        await s.commit()
    return job_id


async def _printed_jobs(client: AsyncClient, keys: Keys, hub: Hub) -> list[str]:
    """A looked-up tape item per integration, a listing label with nested items, a sheet job."""
    ids: list[str] = []
    for app_name in INTERNAL:
        r = await client.post(
            "/api/v1/print",
            json={
                "content_type": "qr_two_lines",
                "lookup": {"app": app_name, "identifier": ITEM[app_name]},
            },
            headers=h(keys.print),
        )
        assert r.status_code == 202, r.text
        ids.append(r.json()["job_id"])
    listing = await client.post(
        "/api/v1/print",
        json={
            "content_type": "qr_with_listing",
            "data": {
                "primary_id": "Shelf 1",
                "qr_payload": SNIPE + "/locations/3",
                "items": [
                    {"item": "A - screws", "qr_payload": SNIPE + "/hardware/1"},
                    {"item": "B - nails", "qr_payload": GROCY + "/product/2"},
                ],
            },
        },
        headers=h(keys.print),
    )
    assert listing.status_code == 202, listing.text
    ids.append(listing.json()["job_id"])
    ids.append(str(await _sheet_job(hub.printer_id, keys.print_id)))
    return ids


def _qr_payloads(node: Any) -> list[Any]:
    found: list[Any] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "qr_payload":
                found.append(value)
            found.extend(_qr_payloads(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_qr_payloads(value))
    return found


@pytest.mark.parametrize("with_web", [False, True])
async def test_read_key_responses_contain_no_internal_base_url(hub: Hub, with_web: bool) -> None:
    if with_web:
        hub.web("snipeit", "spoolman")  # Grocy stays without one: its codes become null
    async with hub.running() as (client, keys):
        ids = await _printed_jobs(client, keys, hub)

        async def responses(key: str) -> list[tuple[str, httpx.Response]]:
            out = [
                ("lookup", await client.get("/api/v1/lookup/snipeit/A-1", headers=h(key))),
                ("lookup grocy", await client.get("/api/v1/lookup/grocy/7", headers=h(key))),
                ("settings", await client.get("/api/v1/settings", headers=h(key))),
                ("info", await client.get("/api/v1/info", headers=h(key))),
                ("jobs", await client.get("/api/v1/jobs?limit=200", headers=h(key))),
            ]
            for job_id in ids:
                out.append(
                    (f"job {job_id}", await client.get(f"/api/v1/jobs/{job_id}", headers=h(key)))
                )
            return out

        # The print key (it submitted the jobs) and the read key (made their owner).
        checked = await responses(keys.print)
        import app.db.engine as eng

        async with eng.async_session() as s:
            await s.execute(
                update(DbJob)
                .where(DbJob.id.in_([UUID(i) for i in ids]))  # type: ignore[attr-defined]
                .values(api_key_id=keys.read_id)
            )
            await s.commit()
        checked += await responses(keys.read)

    jobs_seen = 0
    for where, r in checked:
        assert r.status_code in (200, 403), f"{where}: {r.status_code}"
        assert_no_internal(where, r.text)
        if where.startswith("job") and r.status_code == 200:
            body = r.json()
            jobs = body if isinstance(body, list) else [body]
            for job in jobs:
                jobs_seen += 1
                for value in _qr_payloads(job["payload"]):
                    if value is not None:
                        assert any(value.startswith(w.rstrip("/")) for w in WEB.values()), where
    # Each key: 5 jobs in the list plus 5 single reads; without the walk nothing would be checked.
    assert jobs_seen == 20


async def test_admin_job_detail_keeps_the_payload(hub: Hub) -> None:
    """Guard: the stored payload is what an admin sees."""
    hub.web("snipeit")
    async with hub.running() as (client, keys):
        ids = await _printed_jobs(client, keys, hub)
        r = await client.get(f"/api/v1/jobs/{ids[-1]}", headers=h(keys.admin))
        listing = await client.get(f"/api/v1/jobs/{ids[-2]}", headers=h(keys.admin))
    assert r.status_code == 200, r.text
    detail = r.json()["payload"]["detail"]
    assert detail["cells"][0]["label_data"]["qr_payload"] == GROCY + PATH["grocy"]
    assert detail["id_cell"]["label_data"]["qr_payload"] == SNIPE + PATH["snipeit"]
    items = listing.json()["payload"]["label_data"]["items"]
    assert items[0]["qr_payload"] == SNIPE + "/hardware/1"


async def _until_done(client: AsyncClient, key: str, job_id: str) -> None:
    async with asyncio.timeout(10):
        while True:
            r = await client.get(f"/api/v1/jobs/{job_id}", headers=h(key))
            if r.json()["state"] in ("done", "failed"):
                return
            await asyncio.sleep(0.05)


async def test_batch_read_and_retry_answers_are_redacted(hub: Hub) -> None:
    """Every route that answers a job, not only the list and the detail (ADR 0033)."""
    async with hub.running() as (client, keys):
        batch = await client.post(
            "/api/v1/print/tape-1/batch",
            json={
                "items": [
                    {"content_type": "qr_two_lines", "lookup": {"app": a, "identifier": ITEM[a]}}
                    for a in ("snipeit", "grocy")
                ]
            },
            headers=h(keys.print),
        )
        assert batch.status_code == 202, batch.text
        read = await client.get(
            f"/api/v1/batches/{batch.json()['batch_id']}", headers=h(keys.print)
        )
        single = await client.post(
            "/api/v1/print",
            json={"content_type": "qr_two_lines", "lookup": {"app": "spoolman", "identifier": "3"}},
            headers=h(keys.print),
        )
        job_id = single.json()["job_id"]
        await _until_done(client, keys.print, job_id)
        retry = await client.post(f"/api/v1/jobs/{job_id}/retry", headers=h(keys.print))
    assert read.status_code == 200, read.text
    assert len(read.json()["jobs"]) == 2
    assert all(j["payload"]["label_data"]["qr_payload"] is None for j in read.json()["jobs"])
    assert_no_internal("batch read", read.text)
    assert 200 <= retry.status_code < 300, retry.text
    assert retry.json()["payload"]["label_data"]["qr_payload"] is None
    assert_no_internal("retry", retry.text)
