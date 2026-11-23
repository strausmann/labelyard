# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The reserved sheet type keys come from the routes of the included routers (#335, fix round 1)."""

from __future__ import annotations

import pytest
from app.main import _reserve_sheet_type_keys
from app.sheets import naming


def test_start_fails_loudly_without_a_sheet_type_route() -> None:
    with pytest.raises(RuntimeError, match="/api/v1/sheet-types/"):
        _reserve_sheet_type_keys(["/api/v1/sheets", "/api/v1/sheet-catalog/{key}"])


def test_static_segments_are_reserved_and_parameters_are_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(naming, "RESERVED_TYPE_KEYS", set(naming.RESERVED_TYPE_KEYS))
    _reserve_sheet_type_keys(
        [
            "/api/v1/sheet-types",
            "/api/v1/sheet-types/acme-static",
            "/api/v1/sheet-types/{key}/versions",
            "/api/v1/sheets/other",
        ]
    )
    assert "acme-static" in naming.RESERVED_TYPE_KEYS
    assert not any(k.startswith("{") for k in naming.RESERVED_TYPE_KEYS)


def test_the_sheet_types_router_reserves_its_static_routes() -> None:
    """The real router: every static segment it declares is in the derived set."""
    from app.api.routes.sheet_types import router

    lead = "/api/v1/sheet-types/"
    static = {
        r.path[len(lead) :].split("/", 1)[0]
        for r in router.routes
        if getattr(r, "path", "").startswith(lead) and not r.path[len(lead) :].startswith("{")
    }
    assert {"facets", "import", "delete-unused"} <= static
