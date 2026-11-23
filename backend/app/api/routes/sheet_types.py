# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Sheet type API (spec 15): catalogue, own types, versions, export and import.

Reads need ``read`` scope and are not limited by a key's printer restriction (OC-37); every
write needs ``admin`` (OC-11). The routes stay thin: parse, call the service, map
``SheetApiError`` to the existing error shape.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bounded_body import read_bounded
from app.api.pagination import next_link
from app.api.routes.sheet_common import actor, mapped
from app.auth.dependencies import AuthContext
from app.auth.scope_deps import require_admin, require_read
from app.catalog import service as catalog_service
from app.catalog.upgrade import catalog_update_state
from app.db.session import get_session
from app.models.backing_design import BackingDesign
from app.models.sheet import SheetType, SheetTypeVersion
from app.repositories import sheet_types as types_repo
from app.repositories.sheet_types import NO_USAGE, TypeUsage
from app.schemas.sheets import (
    BackingDesignRead,
    DeleteUnusedRequest,
    DeleteUnusedResult,
    FacetCount,
    GeometryMm,
    ImportResponse,
    SheetTypeCreate,
    SheetTypeDetail,
    SheetTypeFacets,
    SheetTypePatch,
    SheetTypeRead,
    UsageState,
    VersionCreate,
    VersionRead,
)
from app.seed.sheet_seed import shipped_source_urls
from app.sheets import backing_design_service as bds
from app.sheets import exchange
from app.sheets import type_service as ts
from app.sheets.errors import ImportTooLargeError, SheetTypeVersionNotFoundError
from app.sheets.geometry import normalise, validate_geometry
from app.sheets.id_placement import resolve_type_placement
from app.sheets.naming import KEY_PATTERN

router = APIRouter(prefix="/api/v1/sheet-types", tags=["sheet-types"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ReadDep = Annotated[AuthContext, Depends(require_read)]
AdminDep = Annotated[AuthContext, Depends(require_admin)]
KeyPath = Annotated[str, Path(description="Type key, e.g. `avery-l4736`.")]
VersionPath = Annotated[int, Path(ge=1, le=1_000_000)]

_IMPORT_BODY = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ExportDoc"}}},
    }
}
_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "Sheet type not found."}}


def _geometry(v: SheetTypeVersion) -> GeometryMm:
    return exchange.geometry_to_doc(ts.version_geometry(v))


def _version_read(v: SheetTypeVersion, sheets: int) -> VersionRead:
    return VersionRead(
        version=v.version,
        geometry=_geometry(v),
        geometry_hash=v.geometry_hash,
        change_note=v.change_note,
        created_at=v.created_at,
        sheets=sheets,
    )


def design_read(design: BackingDesign, types_in_use: int) -> BackingDesignRead:
    return BackingDesignRead(
        key=design.key,
        name=design.name,
        page_size=design.page_size,  # type: ignore[arg-type]
        strips=bds.strips_to_mm(design.strips),
        origin=design.origin,  # type: ignore[arg-type]
        types_in_use=types_in_use,
    )


_Designs = dict[Any, tuple[BackingDesign, int]]


async def _designs(session: AsyncSession) -> _Designs:
    return {d.id: (d, used) for d, used in await bds.list_with_usage(session)}


async def _type_read(
    session: AsyncSession,
    t: SheetType,
    versions: list[tuple[SheetTypeVersion, int]] | None = None,
    designs: _Designs | None = None,
    usage: dict[Any, TypeUsage] | None = None,
) -> tuple[SheetTypeRead, list[VersionRead]]:
    if versions is None:
        versions = await ts.list_versions_with_sheet_counts(session, t.id)
    if designs is None:
        designs = await _designs(session) if t.backing_design_id is not None else {}
    if usage is None:
        usage = await types_repo.usage_by_type(session, [t.id])
    current, _ = versions[-1]  # oldest first; a type always has version 1
    geometry = ts.version_geometry(current)
    hints = validate_geometry(normalise(geometry))
    linked = designs.get(t.backing_design_id)
    catalog = catalog_update_state(t, current, None if linked is None else linked[0])
    used = usage.get(t.id, NO_USAGE)
    read = SheetTypeRead(
        key=t.key,
        prefix=t.prefix,
        vendor=t.vendor,
        product_codes=list(t.product_codes),
        description=t.description,
        allow_qr=t.allow_qr,
        verified=t.verified,
        origin=t.origin,  # type: ignore[arg-type]
        source_name=t.source_name,
        source_url=t.source_url,
        source_url_shipped=(
            t.origin == "seed"
            and t.source_url is not None
            and t.source_url == shipped_source_urls().get(t.key)
        ),
        notes=t.notes,
        archived_at=t.archived_at,
        created_at=t.created_at,
        updated_at=t.updated_at,
        current_version=current.version,
        geometry=_geometry(current),
        hints=hints,
        sheets_in_use=sum(count for _, count in versions),
        id_placement=t.id_placement,  # type: ignore[arg-type]
        id_cell=t.id_cell,
        id_placement_resolved=resolve_type_placement(t.id_placement, geometry),
        id_placement_auto=resolve_type_placement("auto", geometry),  # type: ignore[arg-type]
        backing_design=None if linked is None else design_read(*linked),
        catalog_key=t.catalog_key,
        catalog_revision=t.catalog_revision,
        update_available=catalog.update_available,
        catalog_newer=catalog.catalog_newer,
        catalog_missing=catalog.catalog_missing,
        sheets_with_free_cells=used.sheets_with_free_cells,
        free_labels=used.free_labels,
        sheets_total=used.sheets_total,
        last_used_at=used.last_used_at,
        usage_state=used.state,
    )
    return read, [_version_read(v, count) for v, count in versions]


@router.get(
    "",
    response_model=list[SheetTypeRead],
    summary="List sheet types",
    description=(
        "Without `limit` the whole list comes back. With `limit` (1-200) and `offset` the list is "
        'paged; the `Link` header names the next page (`rel="next"`), the body stays a list. '
        "`sort=usage` puts the types with the most sheets that have free cells first (then free "
        "labels, then sheets), `sort=last_used` the most recently used first (never used last). "
        "`unused_since` keeps the types not used on or after that day, never used ones included."
    ),
    responses={
        200: {
            "headers": {
                "Link": {
                    "description": 'The next page, `rel="next"`; absent on the last page.',
                    "schema": {"type": "string"},
                }
            }
        }
    },
)
async def list_sheet_types(
    request: Request,
    response: Response,
    session: SessionDep,
    _auth: ReadDep,
    page: Annotated[Literal["A4", "Letter"] | None, Query()] = None,
    vendor: Annotated[str | None, Query(max_length=200)] = None,
    q: Annotated[str | None, Query(max_length=100, description="Key, codes, description.")] = None,
    w: Annotated[float | None, Query(ge=0, le=1000, description="Label width, ±0.5 mm.")] = None,
    h: Annotated[float | None, Query(ge=0, le=1000, description="Label height, ±0.5 mm.")] = None,
    include_archived: bool = False,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    sort: Literal["key", "usage", "last_used"] = "key",
    usage_state: UsageState | None = None,
    unused_since: date | None = None,
) -> list[SheetTypeRead]:
    with mapped():
        found = await ts.list_types_page(
            session,
            page=page,
            vendor=vendor,
            q=q,
            w=w,
            h=h,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
            usage_state=usage_state,
            unused_since=unused_since,
            sort=sort,
        )
        types = found.types
        by_type = await ts.list_versions_with_sheet_counts_many(session, [t.id for t in types])
        designs = await _designs(session)
        body = [
            (await _type_read(session, t, by_type[t.id], designs, found.usage))[0] for t in types
        ]
    if limit is not None:
        response.headers.update(
            next_link(request, limit=limit, offset=offset, has_more=found.has_more)
        )
    return body


@router.get(
    "/facets",
    response_model=SheetTypeFacets,
    summary="Vendor and page-size counts for the type filters",
    description=(
        "Counted under the list's filters except `vendor`, so choosing a vendor keeps the other "
        "vendors available. Archived types are counted only with `include_archived`; "
        "`usage_state` counts only the types in that state."
    ),
)
async def sheet_type_facets(
    session: SessionDep,
    _auth: ReadDep,
    page: Annotated[Literal["A4", "Letter"] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100, description="Key, codes, description.")] = None,
    w: Annotated[float | None, Query(ge=0, le=1000, description="Label width, ±0.5 mm.")] = None,
    h: Annotated[float | None, Query(ge=0, le=1000, description="Label height, ±0.5 mm.")] = None,
    include_archived: bool = False,
    usage_state: UsageState | None = None,
) -> SheetTypeFacets:
    with mapped():
        facets = await ts.type_facets(
            session,
            page=page,
            q=q,
            w=w,
            h=h,
            include_archived=include_archived,
            usage_state=usage_state,
        )
    return SheetTypeFacets(
        vendors=[FacetCount(value=v, count=n) for v, n in facets.vendors],
        page_sizes=[FacetCount(value=v, count=n) for v, n in facets.page_sizes],
    )


@router.post(
    "/import",
    response_model=ImportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Import a sheet type document",
    openapi_extra=_IMPORT_BODY,
    responses={
        200: {"model": ImportResponse, "description": "The same geometry exists already."},
        413: {"description": "Document larger than 64 KB."},
    },
)
async def import_sheet_type(
    request: Request,
    response: Response,
    session: SessionDep,
    auth: AdminDep,
    mode: Annotated[Literal["strict", "new_version", "as_new_key"], Query()] = "strict",
    new_key: Annotated[str | None, Query(max_length=64, pattern=KEY_PATTERN.pattern)] = None,
    new_prefix: Annotated[str | None, Query(max_length=10, pattern=r"^[A-Z][A-Z0-9]{1,9}$")] = None,
) -> ImportResponse:
    with mapped():
        raw = await read_bounded(request, exchange.MAX_IMPORT_BYTES, ImportTooLargeError)
        doc = exchange.parse_import(raw)
        result = await exchange.import_type(
            session, doc, mode=mode, new_key=new_key, new_prefix=new_prefix, actor=actor(auth)
        )
    if result.outcome == "unchanged":
        response.status_code = status.HTTP_200_OK
    return ImportResponse(outcome=result.outcome, key=result.key, version=result.version)


@router.post(
    "/delete-unused",
    response_model=DeleteUnusedResult,
    summary="Delete several unused sheet types at once",
    description=(
        "Each key must name a type in state `unused` (no sheets at all), checked under the write "
        "lock: all of them are deleted or none. `409 not_unused` lists the keys that have sheets "
        "(`error_detail.keys`); `409 type_in_use` with reason `type_in_settings` names a Grocy "
        "label setting that points to one of them (`error_detail.setting`, `error_detail.key`). "
        "Prefix counters stay, so a type added again continues its sheet codes."
    ),
    responses={
        404: {"description": "A key names no type (`error_detail.keys`)."},
        409: {"description": "A type has sheets or is named in a setting."},
    },
)
async def delete_unused_sheet_types(
    body: DeleteUnusedRequest, session: SessionDep, auth: AdminDep
) -> DeleteUnusedResult:
    with mapped():
        deleted = await ts.delete_unused(session, list(body.keys), actor=actor(auth))
    return DeleteUnusedResult(deleted=deleted)


@router.post(
    "",
    response_model=SheetTypeRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a sheet type with version 1",
)
async def create_sheet_type(
    body: SheetTypeCreate, session: SessionDep, auth: AdminDep
) -> SheetTypeRead:
    with mapped():
        geometry = exchange.geometry_from_doc(body.geometry)
        fields = ts.TypeFields(
            key=body.key,
            prefix=body.prefix,
            vendor=body.vendor,
            product_codes=list(body.product_codes),
            description=body.description,
            allow_qr=body.allow_qr,
            verified=body.verified,
            source_name=body.source_name,
            source_url=body.source_url,
            notes=body.notes,
        )
        t, _ = await ts.create_type(session, fields, geometry, actor=actor(auth))
        return (await _type_read(session, t))[0]


@router.get(
    "/{key}",
    response_model=SheetTypeDetail,
    summary="One sheet type with its versions",
    responses=_NOT_FOUND,
)
async def get_sheet_type(key: KeyPath, session: SessionDep, _auth: ReadDep) -> SheetTypeDetail:
    with mapped():
        t = await ts.get_type(session, key)
        read, versions = await _type_read(session, t)
        return SheetTypeDetail(**read.model_dump(), versions=versions)


@router.get(
    "/{key}/versions/{n}",
    response_model=VersionRead,
    summary="One version of a sheet type",
    responses=_NOT_FOUND,
)
async def get_sheet_type_version(
    key: KeyPath, n: VersionPath, session: SessionDep, _auth: ReadDep
) -> VersionRead:
    with mapped():
        t = await ts.get_type(session, key)
        for v, count in await ts.list_versions_with_sheet_counts(session, t.id):
            if v.version == n:
                return _version_read(v, count)
        raise SheetTypeVersionNotFoundError()  # the type was resolved above


@router.patch(
    "/{key}",
    response_model=SheetTypeRead,
    summary="Edit the non-geometric fields of a sheet type",
    description=(
        "`id_placement` sets where the sheet code of a new sheet goes, `id_cell` the preferred "
        "cell of a `cell` sheet (checked against the current version's cell count), "
        "`backing_design` the vendor print on the back of the liner (a design key; `null` "
        "clears it)."
    ),
    responses={
        404: {"description": "Sheet type or backing design not found."},
        422: {
            "description": (
                "A field is invalid, `id_cell` is outside the sheet (`id_cell_out_of_range`) or "
                "the design is for another page size (`backing_design_page_size_mismatch`)."
            )
        },
    },
)
async def patch_sheet_type(
    key: KeyPath, body: SheetTypePatch, session: SessionDep, auth: AdminDep
) -> SheetTypeRead:
    with mapped():
        # Only what the client sent: an omitted field is untouched, `null` is a value.
        changes = body.model_dump(exclude_unset=True)
        t = await ts.patch_type(session, key, changes, actor=actor(auth))
        return (await _type_read(session, t))[0]


@router.post(
    "/{key}/versions",
    response_model=VersionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a version with new geometry",
    responses=_NOT_FOUND,
)
async def add_sheet_type_version(
    key: KeyPath, body: VersionCreate, session: SessionDep, auth: AdminDep
) -> VersionRead:
    with mapped():
        geometry = exchange.geometry_from_doc(body.geometry)
        v = await ts.add_version(
            session, key, geometry, actor=actor(auth), change_note=body.change_note
        )
        return _version_read(v, 0)


@router.post(
    "/{key}/archive",
    response_model=SheetTypeRead,
    summary="Archive a sheet type",
    responses=_NOT_FOUND,
)
async def archive_sheet_type(key: KeyPath, session: SessionDep, auth: AdminDep) -> SheetTypeRead:
    with mapped():
        t = await ts.archive_type(session, key, actor=actor(auth))
        return (await _type_read(session, t))[0]


@router.post(
    "/{key}/unarchive",
    response_model=SheetTypeRead,
    summary="Restore an archived sheet type",
    responses=_NOT_FOUND,
)
async def unarchive_sheet_type(key: KeyPath, session: SessionDep, auth: AdminDep) -> SheetTypeRead:
    with mapped():
        t = await ts.unarchive_type(session, key, actor=actor(auth))
        return (await _type_read(session, t))[0]


@router.post(
    "/{key}/catalog-update",
    response_model=SheetTypeRead,
    summary="Update a sheet type to its newer catalog revision",
    description=(
        "Applies the catalog entry's type fields and backing design link; adds a geometry "
        "version only when the entry's geometry differs from the current one (sheets stay on "
        "their version). `409 locally_edited` when the type was edited here, `409 no_update` "
        "when the catalog has no higher revision or the type is not from the catalog."
    ),
    responses={**_NOT_FOUND, 409: {"description": "Edited here, or no update."}},
)
async def catalog_update_sheet_type(
    key: KeyPath, session: SessionDep, auth: AdminDep
) -> SheetTypeRead:
    with mapped():
        t = await catalog_service.apply_update(session, key, actor=actor(auth))
        return (await _type_read(session, t))[0]


@router.delete(
    "/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a sheet type without sheets",
    description=(
        "Any type without sheets can be deleted, whatever its origin. `409 type_in_use` with "
        "`error_detail.reason` `has_sheets` (archive it instead) or `type_in_settings` (a Grocy "
        "label setting names it, `error_detail.setting`). Linked backing designs and the prefix "
        "counter stay."
    ),
    responses={**_NOT_FOUND, 409: {"description": "Type has sheets or is named in a setting."}},
)
async def delete_sheet_type(key: KeyPath, session: SessionDep, auth: AdminDep) -> Response:
    with mapped():
        await ts.delete_type(session, key, actor=actor(auth))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{key}/export",
    response_model=exchange.ExportDoc,
    summary="Export one version of a sheet type as a JSON document",
    responses=_NOT_FOUND,
)
async def export_sheet_type(
    key: KeyPath,
    session: SessionDep,
    _auth: ReadDep,
    version: Annotated[int | None, Query(ge=1, le=1_000_000)] = None,
) -> JSONResponse:
    with mapped():
        doc = await exchange.export_type(session, key, version)
    filename = f"{doc.type.key}-v{doc.version.version}.json"
    return JSONResponse(
        content=doc.to_json(),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
