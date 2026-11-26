# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Sheet type API (spec 15): catalogue, own types, versions, export and import.

Reads need ``read`` scope and are not limited by a key's printer restriction (OC-37), except the
log, whose entries of other printers a restricted key does not see (#351); every write needs
``admin`` (OC-11). The routes stay thin: parse, call the service, map
``SheetApiError`` to the existing error shape.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.api.bounded_body import read_bounded
from app.api.pagination import next_link
from app.api.routes.sheet_common import actor, mapped
from app.auth.dependencies import AuthContext
from app.auth.scope_deps import require_admin, require_read
from app.calibration import suggest
from app.catalog import service as catalog_service
from app.catalog.upgrade import catalog_update_state
from app.db.session import get_session
from app.models.backing_design import BackingDesign
from app.models.preprint_preset import PreprintPreset
from app.models.printer import Printer, is_page_printer
from app.models.sheet import SheetType, SheetTypeVersion
from app.repositories import sheet_types as types_repo
from app.repositories.sheet_types import NO_USAGE, TypeUsage
from app.schemas.sheets import (
    BackingDesignRead,
    CatalogWriteRead,
    DeleteUnusedRequest,
    DeleteUnusedResult,
    FacetCount,
    GeometryMm,
    IdPlacementRow,
    ImportResponse,
    PreprintPresetRead,
    SheetTypeCreate,
    SheetTypeDetail,
    SheetTypeFacets,
    SheetTypePatch,
    SheetTypeRead,
    UsageState,
    VersionCreate,
    VersionFromSuggestion,
    VersionHistoryRead,
    VersionRead,
)
from app.seed.sheet_seed import shipped_source_urls
from app.sheets import backing_design_service as bds
from app.sheets import exchange, print_service, type_log
from app.sheets import preprint_service as pps
from app.sheets import type_service as ts
from app.sheets.errors import ImportTooLargeError, SheetTypeVersionNotFoundError
from app.sheets.geometry import hmm_to_mm, mm_to_hmm, normalise, validate_geometry
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
_Presets = dict[Any, tuple[PreprintPreset, int]]


async def _designs(session: AsyncSession) -> _Designs:
    return {d.id: (d, used) for d, used in await bds.list_with_usage(session)}


def preset_read(preset: PreprintPreset, types_in_use: int) -> PreprintPresetRead:
    return PreprintPresetRead(
        key=preset.key,
        name=preset.name,
        page_size=preset.page_size,  # type: ignore[arg-type]
        zones=pps.zones_to_mm(preset.zones),
        origin=preset.origin,  # type: ignore[arg-type]
        types_in_use=types_in_use,
    )


async def _presets(session: AsyncSession) -> _Presets:
    return {p.id: (p, used) for p, used in await pps.list_with_usage(session)}


async def _type_read(
    session: AsyncSession,
    t: SheetType,
    versions: list[tuple[SheetTypeVersion, int]] | None = None,
    designs: _Designs | None = None,
    usage: dict[Any, TypeUsage] | None = None,
    presets: _Presets | None = None,
) -> tuple[SheetTypeRead, list[VersionRead]]:
    if presets is None:
        presets = await _presets(session) if t.preprint_preset_id is not None else {}
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
    preset = presets.get(t.preprint_preset_id)
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
        id_margin_band=t.id_margin_band,  # type: ignore[arg-type]
        id_margin_align=t.id_margin_align,  # type: ignore[arg-type]
        id_margin_offset_mm=hmm_to_mm(t.id_margin_offset_hmm or 0),
        preprint_zones=pps.zones_to_mm(t.preprint_zones or []),
        preprint_preset=None if preset is None else preset_read(*preset),
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
        presets = await _presets(session)
        body = [
            (await _type_read(session, t, by_type[t.id], designs, found.usage, presets))[0]
            for t in types
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
    "/{key}/id-placement",
    response_model=list[IdPlacementRow],
    summary="Whether and where the margin sheet code fits each page printer",
    description=(
        "One row per enabled page printer the credentials may use, evaluated on its default tray "
        "or on `tray`. A printer that cannot use the requested tray (unknown or disabled there) "
        "is evaluated on its default tray instead; `tray` in the row always names the tray used. "
        "The same decision the first job of a new sheet of the type makes: hardware "
        "margins, the tray's calibration, the type's keep-out zones and code position. `reason` "
        "says why it does not fit: `outside_printable` (the position lies outside the printable "
        "band), `overlaps_preprint` (zones leave no room), `too_small` (the band is shallower "
        "than 4 mm or too short) or `no_margin_band` (no margin on that side). Read-only."
    ),
    responses=_NOT_FOUND,
)
async def get_sheet_type_id_placement(
    key: KeyPath,
    session: SessionDep,
    auth: ReadDep,
    tray: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
) -> list[IdPlacementRow]:
    with mapped():
        await ts.get_type(session, key)  # 404 before anything else
        allowed = set(auth.allowed_printer_ids)
        printers = [
            p
            for p in (await session.execute(select(Printer).order_by(col(Printer.name)))).scalars()
            if p.enabled and is_page_printer(p) and (not allowed or str(p.id) in allowed)
        ]
        rows = await print_service.id_placement_rows(
            session, type_key=key, printers=printers, tray=tray
        )
    out: list[IdPlacementRow] = []
    for r in rows:
        placed = r.placement
        rect = None if placed is None else placed.rect
        out.append(
            IdPlacementRow(
                printer_id=r.printer.id,
                printer_name=r.printer.name,
                tray=r.tray,
                fits=placed is not None,
                band=None if placed is None else placed.band,
                offset_mm=None if r.offset_hmm is None else hmm_to_mm(r.offset_hmm),
                x_mm=None if rect is None else hmm_to_mm(rect.x),
                y_mm=None if rect is None else hmm_to_mm(rect.y),
                w_mm=None if rect is None else hmm_to_mm(rect.w),
                h_mm=None if rect is None else hmm_to_mm(rect.h),
                content=None if placed is None else placed.content,
                reason=r.reason,
            )
        )
    return out


@router.get(
    "/{key}/versions",
    response_model=list[VersionHistoryRead],
    summary="The versions of a sheet type with their changed geometry, newest first",
    responses=_NOT_FOUND,
)
async def list_sheet_type_versions(
    key: KeyPath, session: SessionDep, _auth: ReadDep
) -> list[VersionHistoryRead]:
    with mapped():
        t = await ts.get_type(session, key)
        versions = await ts.list_versions_with_sheet_counts(session, t.id)  # oldest first
    newest = versions[-1][0].version if versions else 0
    # A version with the newest geometry cannot be restored: the new version would be unchanged.
    newest_hash = versions[-1][0].geometry_hash if versions else None
    out: list[VersionHistoryRead] = []
    before: dict[str, Any] | None = None
    for i, (v, count) in enumerate(versions):
        read = _version_read(v, count)
        geometry = read.geometry.model_dump()
        changed = (
            {}
            if before is None
            else {
                k: {"old": before[k], "new": geometry[k]}
                for k in geometry
                if before[k] != geometry[k]
            }
        )
        newer_in_use = any(c > 0 for _, c in versions[i + 1 :])
        out.append(
            VersionHistoryRead(
                **read.model_dump(),
                created_by=v.created_by,
                changed_fields=changed,  # type: ignore[arg-type]  # validated into FieldChange
                restorable=(
                    v.version < newest and v.geometry_hash != newest_hash and not newer_in_use
                ),
            )
        )
        before = geometry
    return out[::-1]


@router.post(
    "/{key}/versions/{n}/restore",
    response_model=VersionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Restore an older version's geometry as a new version",
    description=(
        "Adds a version with version `n`'s geometry and the change note "
        '"Restored from version n". Refused with `409 newer_version_in_use` while a sheet is '
        "pinned to a newer version; the newest version itself is refused as unchanged geometry "
        "(`422 invalid_geometry`)."
    ),
    responses={
        **_NOT_FOUND,
        409: {"description": "A sheet uses a newer version (`newer_version_in_use`)."},
    },
)
async def restore_sheet_type_version(
    key: KeyPath, n: VersionPath, session: SessionDep, auth: AdminDep
) -> VersionRead:
    with mapped():
        v = await ts.restore_version(session, key, n, actor=actor(auth))
        return _version_read(v, 0)


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
        "clears it). `id_margin_band`, `id_margin_align` and `id_margin_offset_mm` place the "
        "margin sheet code; its smallest box (4 x 10 mm) must lie on the page and off every cell. "
        "`preprint_zones` (at most 16) and `preprint_preset` (a preset key; `null` unlinks it) are "
        "kept free of the code and the feed mark; a zone must lie on the page and off every cell."
    ),
    responses={
        404: {"description": "Sheet type or backing design not found."},
        422: {
            "description": (
                "A field is invalid, `id_cell` is outside the sheet (`id_cell_out_of_range`), "
                "the design or preset is for another page size "
                "(`backing_design_page_size_mismatch`, `preprint_preset_page_size_mismatch`), "
                "the code position leaves the page or covers a cell (`id_position_off_page`, "
                "`id_position_overlaps_cell`) or a zone does (`preprint_zone_off_page`, "
                "`preprint_zone_overlaps_cell`, `error_detail.field` names it)."
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
        # ``null`` stays ``None`` for the fields that refuse it: the service names them (422).
        if "id_margin_offset_mm" in changes:
            offset = changes.pop("id_margin_offset_mm")
            changes["id_margin_offset_hmm"] = (
                None if offset is None else mm_to_hmm(offset, field="id_margin_offset_mm")
            )
        if changes.get("preprint_zones") is not None and body.preprint_zones is not None:
            changes["preprint_zones"] = pps.zones_from_mm(
                body.preprint_zones, field="preprint_zones"
            )
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
    "/{key}/versions/from-suggestion",
    response_model=VersionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Apply a geometry suggestion from a check print as a new version",
    description=(
        "Adds a version with the geometry of the version `run_id` was printed for and the given "
        "values in place (pitch and margins, mm), with a change note naming the run. The run "
        "must be a check print (`422 run_not_a_check_print`) of this type "
        "(`422 run_not_for_this_type`) and of its newest version "
        "(`409 run_version_not_current`) that gives a suggestion "
        "(`422 suggestion_unavailable`, `error_detail.reason` as in the fit's "
        "`suggestion_unavailable`). Geometry rules apply as for any new version "
        "(`422 invalid_geometry`, also for values equal to the stored ones)."
    ),
    responses={
        **_NOT_FOUND,
        409: {"description": "The type has a newer version (`run_version_not_current`)."},
        422: {
            "description": (
                "The run is not a check print of this type (`run_not_for_this_type`, "
                "`run_not_a_check_print`), gives no suggestion (`suggestion_unavailable`), no "
                "value was sent (request validation), or the "
                "geometry is invalid or unchanged (`invalid_geometry`)."
            )
        },
    },
)
async def add_sheet_type_version_from_suggestion(
    key: KeyPath, body: VersionFromSuggestion, session: SessionDep, auth: AdminDep
) -> VersionRead:
    with mapped():
        values = {
            name: mm_to_hmm(value, field=f"{name}_mm")
            for name in suggest.SUGGESTION_FIELDS
            if (value := getattr(body, f"{name}_mm")) is not None
        }
        v = await suggest.apply_suggestion(session, key, body.run_id, values, actor=actor(auth))
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
    response_model=CatalogWriteRead,
    summary="Update a sheet type to its newer catalog revision",
    description=(
        "Applies the catalog entry's type fields and backing design link; adds a geometry "
        "version only when the entry's geometry differs from the current one (sheets stay on "
        "their version). `409 locally_edited` when the type was edited here, `409 no_update` "
        "when the catalog has no higher revision or the type is not from the catalog. When the "
        "entry's backing design differs from the local one, the other types that link it are "
        "updated with it and listed in `also_updated` when each is an unedited catalog type "
        "whose newest entry carries the same new design, else `409 backing_design_shared` "
        "(`error_detail.types`). A design that only this type links follows (the unedited type "
        "proves its state); one no type links follows while it still holds a shipped state, "
        "else `409 backing_design_differs` (`error_detail.design`)."
    ),
    responses={
        **_NOT_FOUND,
        409: {
            "description": (
                "`locally_edited`, `no_update`, `backing_design_differs` or "
                "`backing_design_shared`."
            )
        },
    },
)
async def catalog_update_sheet_type(
    key: KeyPath, session: SessionDep, auth: AdminDep
) -> CatalogWriteRead:
    with mapped():
        written = await catalog_service.apply_update(session, key, actor=actor(auth))
        read = (await _type_read(session, written.type))[0]
    return CatalogWriteRead(**read.model_dump(), also_updated=written.also_updated)


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


# ---------------------------------------------------------------------------- the log (#351)

_LOG_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"description": "Sheet type not found (`sheet_type_not_found`)."}
}


@router.get(
    "/{key}/log",
    response_model=list[type_log.SheetTypeLogRead],
    summary="The log of a sheet type: checks, verdicts and changes, newest first",
    description=(
        'Every check print whose readings were entered (`check_print`), every "Mark as '
        'verified" (`verified`), new version (`new_version`) and catalog update '
        "(`catalog_update`), and the hand entries (`note`), each with its printer and tray, the "
        "calibration history entry in effect (`correction`), the run, the readings per crossing, "
        "a verdict and a note. The hub keeps the newest 500 entries per type. 50 per page "
        "(`limit` up to 100, `offset`); a `Link` header names the next page. A key restricted to "
        "some printers sees only the entries of those printers and the entries without a "
        "printer. Internal only: the log is never part of an export, the catalog or a feedback "
        "issue."
    ),
    responses=_LOG_NOT_FOUND,
)
async def list_sheet_type_log(
    request: Request,
    response: Response,
    key: KeyPath,
    session: SessionDep,
    auth: ReadDep,
    printer: Annotated[UUID | None, Query(description="Only entries of this printer.")] = None,
    tray: Annotated[
        str | None, Query(min_length=1, max_length=100, description="Only entries of this tray.")
    ] = None,
    kind: Annotated[
        type_log.LogKind | None, Query(description="Only entries of this kind.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=type_log.LIST_LIMIT_MAX)] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> list[type_log.SheetTypeLogRead]:
    with mapped():
        entries, has_more = await type_log.list_entries(
            session,
            key,
            allowed_printer_ids=auth.allowed_printer_ids,
            printer_id=printer,
            tray=tray,
            kind=kind,
            limit=limit,
            offset=offset,
        )
    response.headers.update(next_link(request, limit=limit, offset=offset, has_more=has_more))
    return entries


@router.post(
    "/{key}/log",
    response_model=type_log.SheetTypeLogRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a note to the log of a sheet type",
    description=(
        "A hand entry (kind `note`) on the type's current version, optionally naming a printer "
        "and tray, with a verdict (`fits`, `sheet_geometry`, `feed_variance`, `printer`, "
        "`none`) and a note of at most 2000 characters (cleaned, line breaks kept)."
    ),
    responses={
        404: {"description": "Sheet type or printer not found."},
        422: {
            "description": (
                "The note is too long (`label_too_long`) or `tray` came without `printer_id` "
                "(`invalid_geometry`, `error_detail.field` is `tray`)."
            )
        },
    },
)
async def add_sheet_type_log_note(
    key: KeyPath, body: type_log.LogNoteCreate, session: SessionDep, auth: AdminDep
) -> type_log.SheetTypeLogRead:
    with mapped():
        return await type_log.add_note(session, key, body, actor=actor(auth))


@router.patch(
    "/{key}/log/{entry_id}",
    response_model=type_log.SheetTypeLogRead,
    summary="Set the verdict and note of a log entry",
    description=(
        "Only `verdict` and `note` change; an omitted field stays, `note: null` clears it. "
        "Everything else of the entry (kind, printer, readings, time) is kept as written."
    ),
    responses={
        404: {"description": "Sheet type or entry not found (`sheet_type_log_not_found`)."},
        422: {"description": "The note is too long (`label_too_long`)."},
    },
)
async def patch_sheet_type_log_entry(
    key: KeyPath,
    entry_id: Annotated[UUID, Path(description="Log entry id.")],
    body: type_log.LogEntryPatch,
    session: SessionDep,
    _auth: AdminDep,
) -> type_log.SheetTypeLogRead:
    with mapped():
        return await type_log.patch_entry(
            session, key, entry_id, body.model_dump(exclude_unset=True)
        )
