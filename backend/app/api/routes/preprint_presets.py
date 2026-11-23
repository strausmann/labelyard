# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Preprint preset API (#246, ADR 0023 D33): the vendor print on the front of a sheet.

The front-side twin of ``backing_designs``. Reads need ``read`` scope, writes ``admin``. A sheet
type links a preset by its key (``PATCH /api/v1/sheet-types/{key}``, ``preprint_preset``); the
preset's zones then count as keep-out zones of the type, besides its own.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.sheet_common import mapped
from app.api.routes.sheet_types import preset_read
from app.auth.dependencies import AuthContext
from app.auth.scope_deps import require_admin, require_read
from app.db.session import get_session
from app.schemas.sheets import PreprintPresetCreate, PreprintPresetPatch, PreprintPresetRead
from app.sheets import preprint_service as pps

router = APIRouter(prefix="/api/v1/preprint-presets", tags=["sheet-types"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ReadDep = Annotated[AuthContext, Depends(require_read)]
AdminDep = Annotated[AuthContext, Depends(require_admin)]
KeyPath = Annotated[str, Path(max_length=64, description="Preset key, e.g. `avery-zweckform`.")]
_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "Preprint preset not found."}}
_INVALID: dict[int | str, dict[str, Any]] = {
    422: {
        "description": (
            "Invalid request: a zone is smaller than 1 mm (`invalid_geometry`, "
            "`error_detail.field` names it, e.g. `zones.0`), reaches outside the page "
            "(`preprint_zone_off_page`) or, on a change, covers a label cell of a type that "
            "links the preset (`preprint_zone_overlaps_cell`)."
        )
    }
}


@router.get("", response_model=list[PreprintPresetRead], summary="List preprint presets")
async def list_preprint_presets(session: SessionDep, _auth: ReadDep) -> list[PreprintPresetRead]:
    with mapped():
        return [preset_read(p, used) for p, used in await pps.list_with_usage(session)]


@router.post(
    "",
    response_model=PreprintPresetRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a preprint preset",
    responses={409: {"description": "The key exists (`preprint_preset_exists`)."}, **_INVALID},
)
async def create_preprint_preset(
    body: PreprintPresetCreate, session: SessionDep, _auth: AdminDep
) -> PreprintPresetRead:
    with mapped():
        preset = await pps.create(
            session,
            key=body.key,
            name=body.name,
            page_size=body.page_size,
            zones=pps.zones_from_mm(body.zones, field="zones"),
        )
        return preset_read(preset, 0)


@router.patch(
    "/{key}",
    response_model=PreprintPresetRead,
    summary="Change the name or the zones of a preprint preset",
    responses={**_NOT_FOUND, **_INVALID},
)
async def patch_preprint_preset(
    key: KeyPath, body: PreprintPresetPatch, session: SessionDep, _auth: AdminDep
) -> PreprintPresetRead:
    changes: dict[str, Any] = {}
    sent = body.model_fields_set
    with mapped():
        if "name" in sent and body.name is not None:
            changes["name"] = body.name
        if "zones" in sent and body.zones is not None:
            changes["zones"] = pps.zones_from_mm(body.zones, field="zones")
        preset = await pps.patch(session, key, changes)
        return preset_read(preset, await pps.types_in_use(session, preset.id))


@router.delete(
    "/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a preprint preset no sheet type links",
    responses={
        **_NOT_FOUND,
        409: {"description": "A sheet type links the preset (`preset_in_use`)."},
    },
)
async def delete_preprint_preset(key: KeyPath, session: SessionDep, _auth: AdminDep) -> Response:
    with mapped():
        await pps.delete(session, key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
