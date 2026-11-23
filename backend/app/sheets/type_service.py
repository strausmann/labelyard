# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Sheet type service: create, edit, versioned geometry, archive, delete (spec 7.1).

Every write starts with ``acquire_sheet_write_lock`` and commits inside; any failure rolls the
transaction back, so a rejected request leaves nothing behind.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.models.app_setting import AppSetting
from app.models.sheet import SheetType, SheetTypeVersion
from app.repositories import sheet_types as repo
from app.repositories.sheet_types import NO_USAGE, TypeUsage, UsageState
from app.repositories.write_lock import write_transaction
from app.sheets.errors import (
    BackingDesignPageSizeMismatchError,
    IdCellOutOfRangeError,
    InvalidFieldError,
    InvalidGeometryError,
    PrefixLockedError,
    PrefixTakenError,
    PreprintPresetPageSizeMismatchError,
    SheetTypeInUseError,
    SheetTypeNotFoundError,
    SheetTypeVersionNotFoundError,
    TextTooLongError,
    TypeExistsError,
    TypeKeyReservedError,
    TypeNotUnusedError,
)
from app.sheets.geometry import (
    Geometry,
    geometry_hash,
    normalise,
    normalise_shape,
    validate_geometry,
)
from app.sheets.naming import KEY_PATTERN, PREFIX_PATTERN, RESERVED_TYPE_KEYS, default_allow_qr
from app.sheets.text import check_http_url, log_value, sanitise_text

logger = logging.getLogger(__name__)

_SIZE_TOLERANCE_HMM = 50  # 0.5 mm, compared as integers (D16)
_EDITABLE = frozenset(
    {
        "vendor",
        "product_codes",
        "description",
        "allow_qr",
        "verified",
        "source_name",
        "source_url",
        "notes",
        "prefix",
        "id_placement",
        "id_cell",
        "backing_design",
        "id_margin_band",
        "id_margin_align",
        "id_margin_offset_hmm",
        "preprint_zones",
        "preprint_preset",
    }
)
_POSITION = frozenset({"id_margin_band", "id_margin_align", "id_margin_offset_hmm"})
TYPE_PLACEMENTS = frozenset({"auto", "margin", "back_side", "cell"})


@dataclass
class TypeFields:
    key: str
    prefix: str
    vendor: str | None
    product_codes: list[str]
    description: str | None
    allow_qr: bool | None  # None: derived from the geometry (OC-28)
    verified: bool = False
    source_name: str | None = None
    source_url: str | None = None
    notes: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def version_geometry(v: SheetTypeVersion) -> Geometry:
    return Geometry(
        page_size=v.page_size,  # type: ignore[arg-type]
        page_w=v.page_w,
        page_h=v.page_h,
        label_w=v.label_w,
        label_h=v.label_h,
        cols=v.cols,
        rows=v.rows,
        pitch_x=v.pitch_x,
        pitch_y=v.pitch_y,
        margin_top=v.margin_top,
        margin_left=v.margin_left,
        corner_radius=v.corner_radius,
        shape=v.shape,  # type: ignore[arg-type]
    )


def _new_version(
    type_id: UUID, n: int, g: Geometry, *, actor: str, change_note: str | None
) -> SheetTypeVersion:
    g = normalise(g)
    return SheetTypeVersion(
        type_id=type_id,
        version=n,
        page_size=g.page_size,
        page_w=g.page_w,
        page_h=g.page_h,
        label_w=g.label_w,
        label_h=g.label_h,
        cols=g.cols,
        rows=g.rows,
        pitch_x=g.pitch_x,
        pitch_y=g.pitch_y,
        margin_top=g.margin_top,
        margin_left=g.margin_left,
        corner_radius=g.corner_radius,
        shape=g.shape,
        geometry_hash=geometry_hash(g),
        created_by=actor,
        change_note=change_note,
    )


def mm_to_hmm_rounded(value: float) -> int:
    """Filter input in mm to whole hundredths; any float noise rounds away."""
    return round(Decimal(str(value)) * 100)


def _check_prefix(prefix: object) -> None:
    if not isinstance(prefix, str) or not PREFIX_PATTERN.fullmatch(prefix):
        raise InvalidFieldError("prefix", "invalid format")


_NULLABLE_TEXT = frozenset({"vendor", "description", "source_name", "source_url", "notes"})
# Free text is cleaned before it is stored; same bounds as the export document (spec 9.2).
TEXT_LIMITS = {"vendor": 200, "description": 200, "source_name": 200, "notes": 2000}
PRODUCT_CODE_MAX = 40
PRODUCT_CODES_MAX = 50  # the export document's limit (``TypeDoc.product_codes``)
SOURCE_URL_MAX = 500
CHANGE_NOTE_MAX = 500


# Fields that may hold several lines; every other text is a single line (spec 18).
MULTILINE_FIELDS = frozenset({"description", "notes", "change_note"})


def clean_text(field: str, value: str, max_len: int, *, required: bool = False) -> str:
    """``sanitise_text`` with the error mapped to a 422 that names the field.

    ``required``: a text that is empty after cleaning (only invisible characters, say) is
    refused as ``must not be empty``, not stored as an empty string.
    """
    try:
        return sanitise_text(
            value, max_len=max_len, multiline=field in MULTILINE_FIELDS, required=required
        )
    except ValueError as exc:
        if str(exc) == "empty":
            raise InvalidFieldError(field, "must not be empty") from None
        raise TextTooLongError(field) from None


def _clean_optional(field: str, value: str | None) -> str | None:
    """Text that is nothing after cleaning is no text: ``None``, never an empty string."""
    return None if value is None else clean_text(field, value, TEXT_LIMITS[field]) or None


def _clean_url(value: str | None) -> str | None:
    """``source_url`` is http(s) only (the same check as import and the API schema)."""
    if value is None:
        return None
    if len(value) > SOURCE_URL_MAX:
        raise TextTooLongError("source_url")
    try:
        return check_http_url(value)
    except ValueError as exc:
        raise InvalidFieldError("source_url", str(exc)) from None


def _clean_codes(codes: list[str]) -> list[str]:
    if len(codes) > PRODUCT_CODES_MAX:
        raise InvalidFieldError("product_codes", "too many")
    cleaned = (clean_text("product_codes", c, PRODUCT_CODE_MAX) for c in codes)
    return [c for c in cleaned if c]  # an entry that cleans to nothing is dropped


def _clean_change_note(note: str | None) -> str | None:
    return None if note is None else clean_text("change_note", note, CHANGE_NOTE_MAX) or None


def _clean_fields(fields: TypeFields) -> TypeFields:
    return replace(
        fields,
        vendor=_clean_optional("vendor", fields.vendor),
        description=_clean_optional("description", fields.description),
        source_name=_clean_optional("source_name", fields.source_name),
        notes=_clean_optional("notes", fields.notes),
        source_url=_clean_url(fields.source_url),
        product_codes=_clean_codes(fields.product_codes),
    )


def clean_fields(fields: TypeFields) -> TypeFields:
    """The fields as they are stored: free text cleaned, the source URL checked (spec 18)."""
    return _clean_fields(fields)


def _clean_patch_value(name: str, value: Any) -> Any:
    if name in TEXT_LIMITS:
        return _clean_optional(name, value)
    if name == "source_url":
        return _clean_url(value)
    if name == "product_codes":
        return _clean_codes(value)
    return value


_BOOLEAN = frozenset({"allow_qr", "verified"})


def _check_patch_value(name: str, value: object) -> None:
    """Reject a value the column cannot hold before anything is written."""
    if name in _BOOLEAN:
        if not isinstance(value, bool):
            raise InvalidFieldError(name, "must be true or false")
    elif name in _NULLABLE_TEXT:
        if value is not None and not isinstance(value, str):
            raise InvalidFieldError(name, "must be text or null")
    elif name == "product_codes":
        if not (isinstance(value, list) and all(isinstance(c, str) for c in value)):
            raise InvalidFieldError(name, "must be a list of strings")
    elif name == "prefix":
        _check_prefix(value)
    elif name == "id_placement":
        if value not in TYPE_PLACEMENTS:
            raise InvalidFieldError(name, "unknown placement")
    elif name == "id_cell":
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise InvalidFieldError(name, "must be a cell number or null")
    elif name == "backing_design" and value is not None and not isinstance(value, str):
        raise InvalidFieldError(name, "must be a key or null")
    elif name == "id_margin_band":
        if value not in ("auto", "top", "bottom", "left", "right"):
            raise InvalidFieldError(name, "unknown band")
    elif name == "id_margin_align":
        if value not in ("start", "end"):
            raise InvalidFieldError(name, "must be start or end")
    elif name == "id_margin_offset_hmm":
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100_000:
            raise InvalidFieldError(name, "must be a length of at least 0")
    elif name == "preprint_zones":
        if not isinstance(value, list) or not all(isinstance(z, dict) for z in value):
            raise InvalidFieldError(name, "must be a list of zones")
    elif name == "preprint_preset" and value is not None and not isinstance(value, str):
        raise InvalidFieldError(name, "must be a key or null")


async def get_type(session: AsyncSession, key: str) -> SheetType:
    found = await repo.get_by_key(session, key)
    if found is None:
        raise SheetTypeNotFoundError()
    return found


async def current_version(session: AsyncSession, type_id: UUID) -> SheetTypeVersion:
    found = await repo.current_version(session, type_id)
    if found is None:  # a type without a version breaks the model invariant
        raise SheetTypeNotFoundError()
    return found


async def get_version(session: AsyncSession, key: str, n: int) -> SheetTypeVersion:
    t = await get_type(session, key)
    found = await repo.get_version(session, t.id, n)
    if found is None:  # the type was resolved above: it is the version that is missing
        raise SheetTypeVersionNotFoundError()
    return found


async def list_versions_with_sheet_counts(
    session: AsyncSession, type_id: UUID
) -> list[tuple[SheetTypeVersion, int]]:
    return await repo.versions_with_sheet_counts(session, type_id)


async def list_versions_with_sheet_counts_many(
    session: AsyncSession, type_ids: list[UUID]
) -> dict[UUID, list[tuple[SheetTypeVersion, int]]]:
    return await repo.versions_with_sheet_counts_many(session, type_ids)


async def create_type_locked(
    session: AsyncSession,
    fields: TypeFields,
    geometry: Geometry,
    *,
    actor: str,
    origin: Literal["seed", "custom", "imported"],
    change_note: str | None,
) -> tuple[SheetType, SheetTypeVersion]:
    """Insert type and version 1 inside a transaction that already holds the write lock."""
    fields = _clean_fields(fields)
    change_note = _clean_change_note(change_note)
    if not KEY_PATTERN.fullmatch(fields.key):
        raise InvalidFieldError("key", "invalid format")
    if fields.key in RESERVED_TYPE_KEYS:
        raise TypeKeyReservedError()
    _check_prefix(fields.prefix)
    validate_geometry(normalise_shape(geometry), on_write=True)
    if await repo.get_by_key(session, fields.key) is not None:
        raise TypeExistsError()
    if await repo.get_by_prefix(session, fields.prefix) is not None:
        raise PrefixTakenError()
    allow_qr = default_allow_qr(geometry) if fields.allow_qr is None else fields.allow_qr
    t = SheetType(
        key=fields.key,
        prefix=fields.prefix,
        vendor=fields.vendor,
        product_codes=list(fields.product_codes),
        description=fields.description,
        allow_qr=allow_qr,
        verified=fields.verified,
        origin=origin,
        source_name=fields.source_name,
        source_url=fields.source_url,
        notes=fields.notes,
        created_by=actor,
    )
    session.add(t)
    await session.flush()
    v = _new_version(t.id, 1, geometry, actor=actor, change_note=change_note)
    session.add(v)
    return t, v


async def create_type(
    session: AsyncSession,
    fields: TypeFields,
    geometry: Geometry,
    *,
    actor: str,
    origin: Literal["seed", "custom", "imported"] = "custom",
    change_note: str | None = None,
) -> tuple[SheetType, SheetTypeVersion]:
    # Cheap input checks first so a bad request does not take the write lock.
    if not KEY_PATTERN.fullmatch(fields.key):
        raise InvalidFieldError("key", "invalid format")
    if fields.key in RESERVED_TYPE_KEYS:
        raise TypeKeyReservedError()
    _check_prefix(fields.prefix)
    validate_geometry(normalise_shape(geometry), on_write=True)
    _clean_fields(fields)
    _clean_change_note(change_note)
    async with write_transaction(session):
        t, v = await create_type_locked(
            session, fields, geometry, actor=actor, origin=origin, change_note=change_note
        )
        await session.commit()
    return t, v


async def patch_type(
    session: AsyncSession,
    key: str,
    changes: dict[str, Any],
    *,
    actor: str,  # noqa: ARG001 - actor kept for the call contract; not logged
) -> SheetType:
    for name, value in changes.items():
        if name not in _EDITABLE:
            raise InvalidFieldError(name, "not editable")
        _check_patch_value(name, value)
    changes = {name: _clean_patch_value(name, value) for name, value in changes.items()}
    names = sorted(changes)
    async with write_transaction(session):
        t = await get_type(session, key)
        new_prefix = changes.get("prefix")
        if new_prefix is not None and new_prefix != t.prefix:
            if await repo.count_sheets(session, t.id) > 0:
                raise PrefixLockedError()  # OC-5
            if await repo.get_by_prefix(session, new_prefix) is not None:
                raise PrefixTakenError()
        changed = False
        if "id_cell" in changes and changes["id_cell"] is not None:
            check_id_cell(changes["id_cell"], await current_version(session, t.id))
        if "backing_design" in changes:
            design_id = await _design_id_for(session, t, changes.pop("backing_design"))
            if t.backing_design_id != design_id:
                t.backing_design_id = design_id
                changed = True
        if "preprint_preset" in changes or "preprint_zones" in changes or _POSITION & set(changes):
            changed = await _apply_position(session, t, changes) or changed
        for name, value in changes.items():
            new = list(value) if name == "product_codes" else value
            if getattr(t, name) != new:
                setattr(t, name, new)
                changed = True
        if changed:
            t.updated_at = _now()
        await session.commit()
    logger.info("sheet type %s edited: %s", log_value(key), log_value(names))
    return t


async def _apply_position(session: AsyncSession, t: SheetType, changes: dict[str, Any]) -> bool:
    """Apply the preset and zones of a patch, checked against the current version (#246, D33).

    Pops ``preprint_preset`` (a key, or ``None`` to unlink); the zones and the position rule stay
    in ``changes`` for the caller's generic loop, after they have been checked here.
    """
    from app.sheets import preprint_service as pps  # deferred: it imports this module

    g = version_geometry(await current_version(session, t.id))
    changed = False
    if "preprint_preset" in changes:
        key = changes.pop("preprint_preset")
        preset_id = None
        if key is not None:
            preset = await pps.get(session, key)
            if preset.page_size != g.page_size:
                raise PreprintPresetPageSizeMismatchError()
            pps.check_zones(
                preset.zones, page_w=g.page_w, page_h=g.page_h, geometry=g, field="preprint_preset"
            )
            preset_id = preset.id
        if t.preprint_preset_id != preset_id:
            t.preprint_preset_id = preset_id
            changed = True
    if "preprint_zones" in changes:
        pps.check_zones(
            changes["preprint_zones"],
            page_w=g.page_w,
            page_h=g.page_h,
            geometry=g,
            field="preprint_zones",
        )
    if _POSITION & set(changes):
        pps.check_id_position(
            g,
            band=changes.get("id_margin_band", t.id_margin_band),
            align=changes.get("id_margin_align", t.id_margin_align),
            offset_hmm=changes.get("id_margin_offset_hmm", t.id_margin_offset_hmm),
        )
    return changed


def check_id_cell(cell: int, version: SheetTypeVersion) -> None:
    """The preferred ID cell lies on the version's grid (R-350-7)."""
    total = version.cols * version.rows
    if cell < 1 or cell > total:
        raise IdCellOutOfRangeError(cell, total)


async def _design_id_for(session: AsyncSession, t: SheetType, key: str | None) -> UUID | None:
    """The design a type may link: it exists and has the page size of the current version."""
    if key is None:
        return None
    from app.sheets import backing_design_service  # deferred: it imports this module

    design = await backing_design_service.get(session, key)
    if design.page_size != (await current_version(session, t.id)).page_size:
        raise BackingDesignPageSizeMismatchError()
    return design.id


async def add_version_locked(
    session: AsyncSession, key: str, geometry: Geometry, *, actor: str, change_note: str
) -> SheetTypeVersion:
    """Add the next version inside a transaction that already holds the write lock."""
    change_note = _clean_change_note(change_note) or ""
    if not change_note:
        raise InvalidFieldError("change_note", "must not be empty")
    validate_geometry(normalise_shape(geometry), on_write=True)
    t = await get_type(session, key)
    latest = await current_version(session, t.id)
    if latest.geometry_hash == geometry_hash(geometry):
        raise InvalidGeometryError("geometry", "unchanged")
    v = _new_version(t.id, latest.version + 1, geometry, actor=actor, change_note=change_note)
    session.add(v)
    t.updated_at = _now()
    return v


async def add_version(
    session: AsyncSession, key: str, geometry: Geometry, *, actor: str, change_note: str
) -> SheetTypeVersion:
    validate_geometry(normalise_shape(geometry), on_write=True)
    _clean_change_note(change_note)
    async with write_transaction(session):
        v = await add_version_locked(session, key, geometry, actor=actor, change_note=change_note)
        await session.commit()
    return v


async def _set_archived(session: AsyncSession, key: str, archived: bool, actor: str) -> SheetType:  # noqa: ARG001
    async with write_transaction(session):
        t = await get_type(session, key)
        if archived and t.archived_at is None:
            t.archived_at = _now()
        elif not archived:
            t.archived_at = None
        t.updated_at = _now()
        await session.commit()
    logger.info("sheet type %s %s", log_value(key), "archived" if archived else "restored")
    return t


async def archive_type(session: AsyncSession, key: str, *, actor: str) -> SheetType:
    return await _set_archived(session, key, True, actor)


async def unarchive_type(session: AsyncSession, key: str, *, actor: str) -> SheetType:
    return await _set_archived(session, key, False, actor)


def _sheet_type_setting_keys() -> tuple[str, ...]:
    """The settings that name a sheet type: the Grocy label default target and each label type."""
    from app.settings_store.registry import (  # deferred: the registry imports app.sheets
        GROCY_LABEL_DEFAULT_SHEET_TYPE_KEY,
        GROCY_LABEL_TYPES,
        grocy_label_sheet_type_key,
    )

    return (
        GROCY_LABEL_DEFAULT_SHEET_TYPE_KEY,
        *(grocy_label_sheet_type_key(t) for t in GROCY_LABEL_TYPES),
    )


async def types_in_settings(session: AsyncSession, keys: set[str]) -> dict[str, str]:
    """For each of ``keys`` a setting names, the first such setting (in registry order).

    Read from the stored rows inside the caller's transaction, so a setting written before the
    write lock was taken is seen. These settings have no environment override.
    """
    names = _sheet_type_setting_keys()
    rows = (
        (await session.execute(select(AppSetting).where(col(AppSetting.key).in_(names))))
        .scalars()
        .all()
    )
    stored: dict[str, str] = {}
    for row in rows:
        try:
            value = json.loads(row.value_json) if row.value_json is not None else None
        except ValueError:
            continue
        if isinstance(value, str) and value.strip():
            stored[row.key] = value.strip()
    found: dict[str, str] = {}
    for name in names:  # registry order: the default target first
        value = stored.get(name)
        if value is not None and value in keys and value not in found:
            found[value] = name
    return found


async def _delete_locked(session: AsyncSession, types: list[SheetType]) -> None:
    """Delete the types and their versions. ``sheet_id_counters`` is never touched: a type
    added again with the same prefix continues its numbering (spec 6.1.5). Linked backing
    designs stay; page jobs, calibrations and logs keep their rows (no foreign key to them)."""
    ids = [t.id for t in types]
    await session.execute(delete(SheetTypeVersion).where(col(SheetTypeVersion.type_id).in_(ids)))
    for t in types:
        await session.delete(t)


async def delete_type(session: AsyncSession, key: str, *, actor: str) -> None:  # noqa: ARG001
    """Delete any type without sheets (ADR 0028: catalog, imported or custom)."""
    async with write_transaction(session):
        t = await get_type(session, key)
        if await repo.count_sheets(session, t.id) > 0:
            raise SheetTypeInUseError("has_sheets")
        setting = (await types_in_settings(session, {t.key})).get(t.key)
        if setting is not None:
            raise SheetTypeInUseError("type_in_settings", setting=setting)
        await _delete_locked(session, [t])
        await session.commit()
    logger.info("sheet type %s deleted", log_value(key))


async def delete_unused(session: AsyncSession, keys: list[str], *, actor: str) -> list[str]:  # noqa: ARG001
    """Delete every named type, all or nothing; each must be ``unused`` (no sheets at all).

    The state is read under the write lock, so a sheet created after the caller's list makes
    the whole call fail with ``not_unused`` and nothing is deleted.
    """
    wanted = sorted(set(keys))
    async with write_transaction(session):
        found = await repo.get_many_by_key(session, wanted)
        missing = [k for k in wanted if k not in found]
        if missing:
            raise SheetTypeNotFoundError(detail={"keys": missing})
        counts = await repo.sheet_counts(session, [t.id for t in found.values()])
        used = [k for k in wanted if counts.get(found[k].id, 0) > 0]
        if used:
            raise TypeNotUnusedError(used)
        named = await types_in_settings(session, set(wanted))
        if named:
            first = min(named)
            raise SheetTypeInUseError("type_in_settings", setting=named[first], key=first)
        await _delete_locked(session, [found[k] for k in wanted])
        await session.commit()
    logger.info("%d unused sheet types deleted: %s", len(wanted), log_value(wanted))
    return wanted


async def _filtered(
    session: AsyncSession,
    *,
    page: str | None,
    vendor: str | None,
    q: str | None,
    w: float | None,
    h: float | None,
    include_archived: bool,
) -> list[tuple[SheetType, SheetTypeVersion]]:
    # The vendor is compared case-folded here: SQLite's lower() only folds ASCII, so
    # "Ärger" would never match "ärger" in SQL.
    rows = await repo.list_with_current_version(
        session, vendor=None, include_archived=include_archived
    )
    vendor_key = vendor.casefold() if vendor is not None else None
    needle = q.lower() if q else None
    w_hmm = None if w is None else mm_to_hmm_rounded(w)
    h_hmm = None if h is None else mm_to_hmm_rounded(h)
    out: list[tuple[SheetType, SheetTypeVersion]] = []
    for t, v in rows:
        if vendor_key is not None and (t.vendor or "").casefold() != vendor_key:
            continue
        if page is not None and v.page_size != page:
            continue
        if w_hmm is not None and abs(v.label_w - w_hmm) > _SIZE_TOLERANCE_HMM:
            continue
        if h_hmm is not None and abs(v.label_h - h_hmm) > _SIZE_TOLERANCE_HMM:
            continue
        if needle is not None:
            haystack = [t.key, t.vendor or "", t.description or "", *t.product_codes]
            if not any(needle in part.lower() for part in haystack):
                continue
        out.append((t, v))
    return out


TypeSort = Literal["key", "usage", "last_used"]


@dataclass(frozen=True)
class TypePage:
    """The types of one page (all of them without a ``limit``) and whether more follow.

    ``usage`` holds the usage of every listed type that has sheets (one statement for all).
    """

    types: list[SheetType]
    has_more: bool
    usage: dict[UUID, TypeUsage] = field(default_factory=dict)


async def list_types(
    session: AsyncSession,
    *,
    page: str | None,
    vendor: str | None,
    q: str | None,
    w: float | None,
    h: float | None,
    include_archived: bool,
) -> list[SheetType]:
    """Every matching type in key order."""
    rows = await _filtered(
        session, page=page, vendor=vendor, q=q, w=w, h=h, include_archived=include_archived
    )
    return [t for t, _ in rows]


def _not_used_since(cutoff: date) -> datetime:
    return datetime.combine(cutoff, time.min, tzinfo=UTC)


def _by_usage(
    rows: list[tuple[SheetType, SheetTypeVersion]],
    usage: dict[UUID, TypeUsage],
    *,
    usage_state: UsageState | None,
    unused_since: date | None,
) -> list[SheetType]:
    """The usage filters: the state, and "not used since" (never used counts as not used)."""
    cutoff = None if unused_since is None else _not_used_since(unused_since)
    out: list[SheetType] = []
    for t, _ in rows:
        u = usage.get(t.id, NO_USAGE)
        if usage_state is not None and u.state != usage_state:
            continue
        if cutoff is not None and u.last_used_at is not None and _aware(u.last_used_at) >= cutoff:
            continue
        out.append(t)
    return out


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _sorted(
    types: list[SheetType], usage: dict[UUID, TypeUsage], sort: TypeSort
) -> list[SheetType]:
    """``key``: by key. ``usage``: most open sheets, then most free labels, then most sheets.
    ``last_used``: most recently used first, never-used types last. Ties go by key."""
    if sort == "usage":

        def usage_key(t: SheetType) -> tuple[int, int, int, str]:
            u = usage.get(t.id, NO_USAGE)
            return (-u.sheets_with_free_cells, -u.free_labels, -u.sheets_total, t.key)

        return sorted(types, key=usage_key)
    if sort == "last_used":
        used = [t for t in types if usage.get(t.id, NO_USAGE).last_used_at is not None]
        never = [t for t in types if usage.get(t.id, NO_USAGE).last_used_at is None]
        used.sort(key=lambda t: t.key)
        used.sort(key=lambda t: _aware(usage[t.id].last_used_at), reverse=True)  # type: ignore[arg-type]
        return used + never
    return types  # already in key order


async def list_types_page(
    session: AsyncSession,
    *,
    page: str | None,
    vendor: str | None,
    q: str | None,
    w: float | None,
    h: float | None,
    include_archived: bool,
    limit: int | None = None,
    offset: int = 0,
    usage_state: UsageState | None = None,
    unused_since: date | None = None,
    sort: TypeSort = "key",
) -> TypePage:
    """Types in ``sort`` order; without ``limit`` every match comes back (as before)."""
    rows = await _filtered(
        session, page=page, vendor=vendor, q=q, w=w, h=h, include_archived=include_archived
    )
    usage = await repo.usage_by_type(session)
    types = _sorted(
        _by_usage(rows, usage, usage_state=usage_state, unused_since=unused_since), usage, sort
    )
    if limit is None:
        return TypePage(types, False, usage)
    return TypePage(types[offset : offset + limit], len(types) > offset + limit, usage)


@dataclass(frozen=True)
class TypeFacets:
    vendors: list[tuple[str, int]]
    page_sizes: list[tuple[str, int]]


async def type_facets(
    session: AsyncSession,
    *,
    page: str | None,
    q: str | None,
    w: float | None,
    h: float | None,
    include_archived: bool,
    usage_state: UsageState | None = None,
) -> TypeFacets:
    """Vendor and page-size counts under the list's filters, **without** the vendor filter.

    Choosing a vendor must not hide the other vendors, so the vendor is never applied here.
    """
    rows = await _filtered(
        session, page=page, vendor=None, q=q, w=w, h=h, include_archived=include_archived
    )
    if usage_state is not None:
        usage = await repo.usage_by_type(session)
        rows = [(t, v) for t, v in rows if usage.get(t.id, NO_USAGE).state == usage_state]
    vendors: dict[str, tuple[str, int]] = {}  # lower-case -> (first spelling seen, count)
    sizes: dict[str, int] = {}
    for t, v in rows:
        if t.vendor:
            spelling, n = vendors.get(t.vendor.casefold(), (t.vendor, 0))
            vendors[t.vendor.casefold()] = (spelling, n + 1)
        sizes[v.page_size] = sizes.get(v.page_size, 0) + 1
    return TypeFacets(
        vendors=[vendors[k] for k in sorted(vendors)],
        page_sizes=sorted(sizes.items()),
    )
