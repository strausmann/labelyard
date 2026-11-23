# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The sheet-type catalog that ships in the image (ADR 0028, spec 6.1.2).

Reads need ``read`` scope; installing needs ``admin``. An installed type is managed under
``/api/v1/sheet-types`` like any other, including its catalog update.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.sheet_common import actor, mapped
from app.api.routes.sheet_types import _type_read
from app.auth.dependencies import AuthContext
from app.auth.scope_deps import require_admin, require_read
from app.catalog import service
from app.catalog.service import CatalogItem
from app.db.session import get_session
from app.schemas.sheets import (
    CatalogEntryDetail,
    CatalogEntryRead,
    CatalogPage,
    CatalogWriteRead,
    FacetCount,
    InstallRequest,
    SheetTypeFacets,
)
from app.sheets.naming import KEY_PATTERN

router = APIRouter(prefix="/api/v1/sheet-catalog", tags=["sheet-catalog"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ReadDep = Annotated[AuthContext, Depends(require_read)]
AdminDep = Annotated[AuthContext, Depends(require_admin)]
EntryPath = Annotated[
    str,
    Path(max_length=64, pattern=KEY_PATTERN.pattern, description="Entry key, e.g. `avery-5160`."),
]
_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"description": "No catalog entry under this key (`catalog_entry_not_found`)."}
}


def _read(item: CatalogItem) -> CatalogEntryRead:
    e = item.entry
    return CatalogEntryRead(
        **e.model_dump(exclude={"fields_hash"}),
        installed=item.installed,
        update_available=item.state.update_available,
        catalog_newer=item.state.catalog_newer,
    )


@router.get(
    "",
    response_model=CatalogPage,
    summary="List the sheet-type catalog",
    description=(
        "`q` matches the key, the vendor or a product code; `w`/`h` the label size within 0.5 mm. "
        "The facets count under every filter except `vendor`."
    ),
)
async def list_catalog(
    session: SessionDep,
    _auth: ReadDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[Literal["A4", "Letter"] | None, Query()] = None,
    vendor: Annotated[str | None, Query(max_length=200)] = None,
    w: Annotated[float | None, Query(ge=0, le=1000, description="Label width, ±0.5 mm.")] = None,
    h: Annotated[float | None, Query(ge=0, le=1000, description="Label height, ±0.5 mm.")] = None,
    verified: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> CatalogPage:
    with mapped():
        found = await service.list_entries(
            session,
            q=q,
            page=page,
            w=w,
            h=h,
            verified=verified,
            vendor=vendor,
            limit=limit,
            offset=offset,
        )
    return CatalogPage(
        items=[_read(item) for item in found.items],
        total=found.total,
        facets=SheetTypeFacets(
            vendors=[FacetCount(value=v, count=n) for v, n in found.vendors],
            page_sizes=[FacetCount(value=v, count=n) for v, n in found.page_sizes],
        ),
    )


@router.get(
    "/{key}",
    response_model=CatalogEntryDetail,
    summary="One catalog entry with its geometry",
    responses=_NOT_FOUND,
)
async def get_catalog_entry(
    key: EntryPath, session: SessionDep, _auth: ReadDep
) -> CatalogEntryDetail:
    with mapped():
        item, doc = await service.get_entry(session, key)
    return CatalogEntryDetail(
        **_read(item).model_dump(),
        prefix=doc.type.prefix,
        description=doc.type.description,
        notes=doc.type.notes,
        allow_qr=doc.type.allow_qr,
        geometry=doc.version.geometry,
    )


@router.post(
    "/{key}/install",
    response_model=CatalogWriteRead,
    status_code=status.HTTP_201_CREATED,
    summary="Install a catalog entry as a sheet type",
    description=(
        "Under its own key the type is `seed` with catalog provenance; with `as_new_key` it is "
        "`imported` without provenance. Both keep the entry's `verified` (the catalog's own "
        "evidence, ADR 0028). `409 type_exists` when the key is taken; `409 prefix_taken` when "
        "the prefix is, with a free one in `error_detail.suggested_prefix`; `409 "
        "backing_design_differs` when the local backing design of the entry's key differs and "
        "was edited here or is linked by a type that is not an unedited catalog type "
        "(`error_detail.design`); `409 backing_design_shared` when other types share that design "
        "and cannot take the change with it (`error_detail.types`). Types that share the design "
        "and can take it are updated with it and listed in `also_updated`."
    ),
    responses={
        **_NOT_FOUND,
        409: {"description": "The key or the prefix is taken, or the backing design differs."},
        422: {"description": "`as_new_key` is reserved or invalid."},
    },
)
async def install_catalog_entry(
    key: EntryPath,
    session: SessionDep,
    auth: AdminDep,
    body: InstallRequest | None = None,
) -> CatalogWriteRead:
    request = body or InstallRequest()
    with mapped():
        written = await service.install(
            session,
            key,
            as_new_key=request.as_new_key,
            prefix=request.prefix,
            actor=actor(auth),
        )
        read = (await _type_read(session, written.type))[0]
    return CatalogWriteRead(**read.model_dump(), also_updated=written.also_updated)
