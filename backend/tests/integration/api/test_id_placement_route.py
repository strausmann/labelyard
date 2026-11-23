# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The per-printer view of the margin sheet code, the margin check's rectangle and the write
checks of the code position and the keep-out zones (#246 with #326's criteria).

The full app with its lifespan (``test_sheet_print_api``'s rig): page printer ``page-1`` with
the fake IPP printer, the type ``acme-t1`` (4 x 12 cells of 45.7 x 21.2 mm, margins 21.3 mm top
and bottom, 9.85 mm left and right).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app.models.printer import Printer
from app.printer_backends.ipp.attributes import Margins
from app.sheets import type_service as ts
from app.sheets.geometry import Geometry
from sqlalchemy import select

from tests.integration.api.test_sheet_print_api import Rig, _err, _printer, _type, running
from tests.integration.api.test_sheet_types_api import _factory, _insert_key

pytestmark = pytest.mark.asyncio

# Covers every margin band of acme-t1 and touches no cell.
ALL_BANDS = [
    {"x_mm": 0, "y_mm": 0, "w_mm": 210, "h_mm": 21.3},
    {"x_mm": 0, "y_mm": 275.7, "w_mm": 210, "h_mm": 21.3},
    {"x_mm": 0, "y_mm": 21.3, "w_mm": 9.85, "h_mm": 254.4},
    {"x_mm": 200.15, "y_mm": 21.3, "w_mm": 9.85, "h_mm": 254.4},
]
# 6 mm top margin: the code's 4 mm box, from the printer's 5 mm edge, reaches the first row.
TIGHT = Geometry("A4", 21000, 29700, 4570, 2120, 4, 12, 4820, 2120, 600, 985, None, "rectangle")


async def _admin() -> dict[str, str]:
    return {"X-Label-Hub-Key": await _insert_key(["admin"])}


async def _patch(rig: Rig, key: str, body: dict[str, Any], admin: dict[str, str]) -> httpx.Response:
    return await rig.client.patch(f"/api/v1/sheet-types/{key}", json=body, headers=admin)


async def _rows(rig: Rig, key: str = "acme-t1", **kw: Any) -> httpx.Response:
    return await rig.client.get(
        f"/api/v1/sheet-types/{key}/id-placement",
        params=kw.pop("params", {}),
        headers=kw.pop("headers", rig.reader),
    )


async def test_id_placement_route_fits_does_not_fit_overlaps() -> None:
    # 6 mm unprintable on the right: the right band (9.85 mm) is 2.85 mm deep.
    async with running(margins=Margins(top=423, left=423, bottom=423, right=600)) as rig:
        admin = await _admin()
        fits = await _rows(rig)
        assert fits.status_code == 200, fits.text
        (row,) = fits.json()
        assert row["printer_name"] == "P page-1" and row["tray"] == "auto"
        assert row["fits"] is True and row["band"] == "top" and row["reason"] is None
        assert row["content"] == "qr_code_type" and row["offset_mm"] == 0
        assert row["h_mm"] == round(21.3 - 4.23 - 1, 2)

        offset = await _patch(rig, "acme-t1", {"id_margin_offset_mm": 12.5}, admin)
        assert offset.status_code == 200, offset.text
        (moved,) = (await _rows(rig)).json()
        assert moved["fits"] is True and moved["offset_mm"] == 12.5

        right = await _patch(rig, "acme-t1", {"id_margin_band": "right"}, admin)
        assert right.status_code == 200, right.text
        (small,) = (await _rows(rig)).json()
        assert small["fits"] is False and small["reason"] == "too_small"
        assert small["band"] is None and small["w_mm"] is None

        await _patch(rig, "acme-t1", {"id_margin_band": "auto", "id_margin_offset_mm": 0}, admin)
        zoned = await _patch(rig, "acme-t1", {"preprint_zones": ALL_BANDS}, admin)
        assert zoned.status_code == 200, zoned.text
        (blocked,) = (await _rows(rig)).json()
        assert blocked["fits"] is False and blocked["reason"] == "overlaps_preprint"

        missing = await _rows(rig, "no-such-type")
        assert missing.status_code == 404
        anonymous = await rig.client.get("/api/v1/sheet-types/acme-t1/id-placement")
        assert anonymous.status_code in (401, 403)


async def test_id_placement_lists_only_the_keys_printers() -> None:
    async with running() as rig:
        other = await _printer("page-2")
        await _printer("page-off", enabled=False)
        await _printer("tape-1", backend="ptouch")
        restricted = {"X-Label-Hub-Key": await _insert_key(["read"], [str(other)])}

        everyone = await _rows(rig)
        only = await _rows(rig, headers=restricted)
        # a tray the printer cannot use (disabled here): evaluated on its default tray
        on_tray = await _rows(rig, params={"tray": "off"})

        assert everyone.status_code == 200 and only.status_code == 200
        assert [r["printer_name"] for r in everyone.json()] == ["P page-1", "P page-2"]
        assert [r["printer_id"] for r in only.json()] == [str(other)]
        assert on_tray.status_code == 200 and {r["tray"] for r in on_tray.json()} == {"auto"}


async def test_id_position_overlapping_a_cell_is_422() -> None:
    async with running() as rig:
        admin = await _admin()
        await _type("acme-tight", "TGT", TIGHT)

        cell = await _patch(rig, "acme-tight", {"id_margin_band": "top"}, admin)
        assert cell.status_code == 422 and _err(cell)["error_code"] == "id_position_overlaps_cell"
        off = await _patch(
            rig, "acme-t1", {"id_margin_band": "top", "id_margin_offset_mm": 300}, admin
        )
        assert off.status_code == 422 and _err(off)["error_code"] == "id_position_off_page"
        comma = await _patch(rig, "acme-t1", {"id_margin_offset_mm": 12.555}, admin)
        assert comma.status_code == 422  # three decimals
        for name in ("id_margin_offset_mm", "id_margin_band", "preprint_zones"):
            null = await _patch(rig, "acme-t1", {name: None}, admin)
            assert null.status_code == 422, name
        async with _factory()() as s:
            t = await ts.get_type(s, "acme-tight")
        assert t.id_margin_band == "auto"  # nothing was written
        ok = await _patch(
            rig, "acme-t1", {"id_margin_band": "top", "id_margin_align": "end"}, admin
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["id_margin_band"] == "top" and ok.json()["id_margin_align"] == "end"
        reader = await _patch(rig, "acme-t1", {"id_margin_band": "left"}, rig.reader)
        assert reader.status_code == 403


async def test_zone_over_a_cell_is_422() -> None:
    async with running() as rig:
        admin = await _admin()
        over = {"x_mm": 20, "y_mm": 30, "w_mm": 5, "h_mm": 5}
        cell = await _patch(rig, "acme-t1", {"preprint_zones": [over]}, admin)
        assert cell.status_code == 422
        assert _err(cell)["error_code"] == "preprint_zone_overlaps_cell"
        assert _err(cell)["error_detail"] == {"field": "preprint_zones.0"}
        outside = {"x_mm": 205, "y_mm": 5, "w_mm": 10, "h_mm": 5}
        off = await _patch(rig, "acme-t1", {"preprint_zones": [outside]}, admin)
        assert off.status_code == 422 and _err(off)["error_code"] == "preprint_zone_off_page"
        logo = {"x_mm": 9.85, "y_mm": 6, "w_mm": 40, "h_mm": 10, "note": "Logo​ of the vendor"}
        too_many = await _patch(rig, "acme-t1", {"preprint_zones": [logo] * 17}, admin)
        assert too_many.status_code == 422
        long_note = await _patch(
            rig, "acme-t1", {"preprint_zones": [{**logo, "note": "x" * 121}]}, admin
        )
        assert long_note.status_code == 422

        ok = await _patch(rig, "acme-t1", {"preprint_zones": [logo]}, admin)

        assert ok.status_code == 200, ok.text
        (zone,) = ok.json()["preprint_zones"]
        assert zone == {
            "x_mm": 9.85,
            "y_mm": 6.0,
            "w_mm": 40.0,
            "h_mm": 10.0,
            "note": "Logo of the vendor",
        }
        cleared = await _patch(rig, "acme-t1", {"preprint_zones": []}, admin)
        assert cleared.status_code == 200 and cleared.json()["preprint_zones"] == []


async def test_margin_check_reports_the_rectangle() -> None:
    async with running(margins=Margins(top=423, left=423, bottom=423, right=423)) as rig:
        admin = await _admin()
        params = {"type": "acme-t1", "printer": "page-1"}
        plain = await rig.client.get(
            "/api/v1/sheets/margin-check", params=params, headers=rig.reader
        )
        assert plain.status_code == 200, plain.text
        rect = plain.json()["rect"]
        assert rect["x_mm"] == 9.85 and rect["content"] == "qr_code_type"
        assert plain.json()["feed_mark_end"] == "top_right"
        assert plain.json()["feed_mark_reason"] is None

        logo = {"x_mm": 9.85, "y_mm": 6, "w_mm": 40, "h_mm": 10}
        await _patch(rig, "acme-t1", {"preprint_zones": [logo]}, admin)
        zoned = await rig.client.get(
            "/api/v1/sheets/margin-check", params=params, headers=rig.reader
        )

        assert zoned.json()["rect"]["x_mm"] == 50.85
        assert zoned.json()["band"] == "top" and zoned.json()["printable"] is True
        async with _factory()() as s:
            ids = list((await s.execute(select(Printer.slug))).scalars())
        assert "page-1" in ids
