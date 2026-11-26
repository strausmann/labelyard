# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""List, install and update catalog entries (ADR 0028, spec 6.1.2, 6.1.3, S12).

Installing is importing a catalog document, with two differences to an upload: the document
comes from the image, so its ``verified`` is kept (an upload drops it, D19); and under its own
key the type is ``origin="seed"`` with catalog provenance, so it counts as unedited until
someone changes it. Installed under a new key it is ``origin="imported"`` without provenance:
it is no longer the catalog's type.

Every write takes the sheet write lock first and reads what it decides on inside it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.catalog import loader
from app.catalog.loader import CatalogEntry, CatalogMeta
from app.catalog.upgrade import (
    CATALOG_ACTOR,
    CatalogUpdateState,
    catalog_update_state,
    is_locally_edited,
    provenance_inputs,
)
from app.models.backing_design import BackingDesign
from app.models.sheet import SheetType, SheetTypeVersion
from app.repositories import sheet_types as repo
from app.repositories.write_lock import write_transaction
from app.sheets import backing_design_service as bds
from app.sheets import type_log
from app.sheets import type_service as ts
from app.sheets.errors import (
    BackingDesignDiffersError,
    BackingDesignNotFoundError,
    BackingDesignPageSizeMismatchError,
    BackingDesignSharedError,
    CatalogEntryNotFoundError,
    InvalidFieldError,
    NoCatalogUpdateError,
    PrefixTakenError,
    TypeExistsError,
    TypeKeyReservedError,
    TypeLocallyEditedError,
)
from app.sheets.exchange import BackingDesignDoc, ExportDoc, geometry_from_doc
from app.sheets.geometry import geometry_hash
from app.sheets.naming import KEY_PATTERN, PREFIX_PATTERN, RESERVED_TYPE_KEYS
from app.sheets.text import log_value

logger = logging.getLogger(__name__)

_SIZE_TOLERANCE_MM = 0.5  # the installed list's tolerance (``type_service``)
_MAX_SUGGESTIONS = 99


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogItem:
    entry: CatalogEntry
    installed: bool
    state: CatalogUpdateState


@dataclass(frozen=True)
class CatalogPage:
    items: list[CatalogItem]
    total: int
    vendors: list[tuple[str, int]]
    page_sizes: list[tuple[str, int]]


@dataclass(frozen=True)
class CatalogWrite:
    """The type an install or update wrote, and the types co-updated with its shared design."""

    type: SheetType
    also_updated: list[str]


def _matches(
    e: CatalogEntry,
    *,
    q: str | None,
    page: str | None,
    w: float | None,
    h: float | None,
    verified: bool | None,
) -> bool:
    if page is not None and e.page_size != page:
        return False
    if w is not None and abs(e.label_w_mm - w) > _SIZE_TOLERANCE_MM:
        return False
    if h is not None and abs(e.label_h_mm - h) > _SIZE_TOLERANCE_MM:
        return False
    if verified is not None and e.verified != verified:
        return False
    if q:
        needle = q.casefold()
        haystack = [e.key, e.vendor or "", *e.product_codes]
        if not any(needle in part.casefold() for part in haystack):
            return False
    return True


async def _installed_states(
    session: AsyncSession,
) -> tuple[set[str], dict[str, CatalogUpdateState]]:
    """The keys taken by installed types, and the update state per catalog key installed."""
    rows = await repo.list_with_current_version(session, vendor=None, include_archived=True)
    design_ids = [t.backing_design_id for t, _ in rows if t.backing_design_id is not None]
    designs: dict[object, BackingDesign] = {}
    if design_ids:
        found = await session.execute(
            select(BackingDesign).where(col(BackingDesign.id).in_(design_ids))
        )
        designs = {d.id: d for d in found.scalars()}
    taken: set[str] = set()
    states: dict[str, CatalogUpdateState] = {}
    for t, current in rows:
        taken.add(t.key)
        if t.catalog_key is None:
            continue
        taken.add(t.catalog_key)
        design = designs.get(t.backing_design_id) if t.backing_design_id is not None else None
        states[t.catalog_key] = catalog_update_state(t, current, design)
    return taken, states


async def list_entries(
    session: AsyncSession,
    *,
    q: str | None,
    page: str | None,
    w: float | None,
    h: float | None,
    verified: bool | None,
    vendor: str | None,
    limit: int,
    offset: int,
) -> CatalogPage:
    """Catalog entries in key order with their installed state; facets without ``vendor``."""
    unfiltered = [
        e for e in loader.load_index() if _matches(e, q=q, page=page, w=w, h=h, verified=verified)
    ]
    vendors: dict[str, tuple[str, int]] = {}
    sizes: dict[str, int] = {}
    for e in unfiltered:
        if e.vendor:
            spelling, n = vendors.get(e.vendor.casefold(), (e.vendor, 0))
            vendors[e.vendor.casefold()] = (spelling, n + 1)
        sizes[e.page_size] = sizes.get(e.page_size, 0) + 1
    wanted = vendor.casefold() if vendor is not None else None
    entries = sorted(
        (e for e in unfiltered if wanted is None or (e.vendor or "").casefold() == wanted),
        key=lambda e: e.key,
    )
    taken, states = await _installed_states(session)
    items = [
        CatalogItem(e, e.key in taken, states.get(e.key, CatalogUpdateState()))
        for e in entries[offset : offset + limit]
    ]
    return CatalogPage(
        items=items,
        total=len(entries),
        vendors=[vendors[k] for k in sorted(vendors)],
        page_sizes=sorted(sizes.items()),
    )


def entry_of(key: str) -> CatalogEntry:
    for e in loader.load_index():
        if e.key == key:
            return e
    raise CatalogEntryNotFoundError()


async def get_entry(session: AsyncSession, key: str) -> tuple[CatalogItem, ExportDoc]:
    entry = entry_of(key)
    doc = loader.load_entry(key)
    taken, states = await _installed_states(session)
    return CatalogItem(entry, key in taken, states.get(key, CatalogUpdateState())), doc


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


async def suggest_prefix(session: AsyncSession, base: str) -> str | None:
    """A free prefix close to ``base``: ``base`` with a number in front of the length limit."""
    for n in range(2, _MAX_SUGGESTIONS + 1):
        suffix = str(n)
        candidate = base[: 10 - len(suffix)] + suffix
        if (
            PREFIX_PATTERN.fullmatch(candidate)
            and await repo.get_by_prefix(session, candidate) is None
        ):
            return candidate
    return None


def _check_new_key(as_new_key: str) -> None:
    if not KEY_PATTERN.fullmatch(as_new_key):
        raise InvalidFieldError("as_new_key", "invalid format")
    if as_new_key in RESERVED_TYPE_KEYS:
        raise TypeKeyReservedError()


def shipped_design_states() -> dict[str, set[str]]:
    """Every state of every backing design the hub shipped, by design key, as canonical JSON.

    The designs the catalog documents embed today, and the strips the 1.7.x seed linked under
    each of those names (the state an upgraded installation holds). It reads every catalog
    document, so the writes call it before they take the write lock.
    """
    shipped: dict[str, list[BackingDesignDoc]] = {}
    for entry in loader.load_index():
        embedded = loader.load_entry(entry.key).type.backing_design
        if embedded is not None:
            shipped.setdefault(embedded.key, []).append(embedded)
    from app.seed.sheet_seed import shipped_backing_defaults  # deferred: the seed imports a lot

    seeded: dict[str, set[tuple[tuple[int, int, int, int], ...]]] = {}
    for design_key, strips in shipped_backing_defaults().values():
        seeded.setdefault(design_key, set()).add(strips)
    states: dict[str, set[str]] = {}
    for key, docs in shipped.items():
        found = states.setdefault(key, set())
        for d in docs:
            found.add(d.model_dump_json())
            for strips in seeded.get(key, set()):
                hmm = [{"x": x, "y": y, "w": w, "h": h} for x, y, w, h in strips]
                found.add(d.model_copy(update={"strips": bds.strips_to_mm(hmm)}).model_dump_json())
    return states


@dataclass
class _Followers:
    """Types co-updated because the design they share follows the catalog (ruling 2026-11-23)."""

    keys: list[str] = field(default_factory=list)
    types: list[SheetType] = field(default_factory=list)
    # ``shipped_design_states()``, read before the write lock was taken
    shipped: dict[str, set[str]] = field(default_factory=dict)
    # who asked for the write: named in the sheet-type log entry of each updated type (7.6)
    actor: str | None = None


async def _follow_the_catalog(
    session: AsyncSession,
    design: BackingDesign,
    wanted: BackingDesignDoc,
    t: SheetType,
    followers: _Followers,
) -> None:
    """Bring a local design to the entry's state together with every type that shares it.

    The design must still hold a state the hub shipped (else ``backing_design_differs``): proven
    by an unedited catalog type that links it, or found among the shipped states. Every other
    type that links it must be an unedited catalog type whose newest catalog entry carries the
    identical new design (else ``backing_design_shared`` naming the ones that are not); each of
    them then gets its own catalog update in the same transaction, so none of them ends up
    edited by a change of the design it shares.
    """
    linked = (
        await session.execute(
            select(SheetType).where(
                col(SheetType.backing_design_id) == design.id, col(SheetType.id) != t.id
            )
        )
    ).scalars()
    ready: list[tuple[SheetType, ExportDoc, CatalogMeta, SheetTypeVersion]] = []
    blocked: list[str] = []
    for other in sorted(linked, key=lambda o: o.key):
        current, own_design = await provenance_inputs(session, other)
        entry = await _entry_of_type(other)
        if (
            entry is None
            or is_locally_edited(other, current, own_design)
            or entry[0].type.backing_design != wanted
        ):
            blocked.append(other.key)
            continue
        ready.append((other, entry[0], entry[1], current))
    if blocked:
        raise BackingDesignSharedError(design.key, blocked)
    # The design still holds a shipped state when an unedited catalog type links it: that type's
    # fields hash embeds the design, so it proves the state of its installed revision, also when
    # no entry of the image embeds that state any more. ``t`` itself proves it on an update (its
    # check ran before); otherwise the states of the image and of the 1.7.x seed decide.
    proven = t.backing_design_id == design.id or bool(ready)
    state = bds.to_doc(design).model_dump_json()
    if not proven and state not in followers.shipped.get(design.key, set()):
        raise BackingDesignDiffersError(design.key)
    strips = bds.strips_from_mm(wanted.strips)
    bds.validate_strips(strips, design.page_size)
    design.name = wanted.name
    design.strips = strips
    design.updated_at = datetime.now(UTC)
    logger.info("backing design %s follows the catalog", log_value(design.key))
    for other, doc, meta, current in ready:
        await _apply_entry(session, other, doc, meta, current, followers)
        followers.keys.append(other.key)
        followers.types.append(other)


async def _entry_of_type(t: SheetType) -> tuple[ExportDoc, CatalogMeta] | None:
    """The newest catalog entry of an installed catalog type; ``None`` without one."""
    if t.catalog_key is None:
        return None
    try:
        return loader.load_entry(t.catalog_key), loader.load_meta(t.catalog_key)
    except CatalogEntryNotFoundError:
        return None


async def _link_design(
    session: AsyncSession,
    t: SheetType,
    doc: ExportDoc,
    origin: Literal["seed", "imported"],
    *,
    followers: _Followers | None,
) -> None:
    """Link the document's backing design; ``None`` unlinks.

    A design of that key that does not exist here is created from the document. One that exists
    and differs from the document is brought to the document's state when ``followers`` is given
    (the type is to be the catalog's) and ``_follow_the_catalog`` allows it; without (a copy under
    a new key) it is linked as stored, the import's rule (R-350-8).
    """
    wanted = doc.type.backing_design
    if wanted is None:
        t.backing_design_id = None
        return
    try:
        design: BackingDesign = await bds.get(session, wanted.key)
    except BackingDesignNotFoundError:
        design = await bds.create_locked(
            session,
            key=wanted.key,
            name=wanted.name,
            page_size=wanted.page_size,
            strips=bds.strips_from_mm(wanted.strips),
            origin=origin,
        )
    if design.page_size != doc.version.geometry.page_size or design.page_size != wanted.page_size:
        raise BackingDesignPageSizeMismatchError()
    if followers is not None and bds.to_doc(design) != wanted:
        await _follow_the_catalog(session, design, wanted, t, followers)
    t.backing_design_id = design.id


async def _still_unedited(session: AsyncSession, types: list[SheetType]) -> None:
    """Defensive: every type a catalog write touched must be unedited before it commits."""
    await session.flush()
    for t in types:
        current, design = await provenance_inputs(session, t)
        if is_locally_edited(t, current, design):
            logger.error("catalog write of %s would leave it edited", log_value(t.key))
            raise TypeLocallyEditedError()


async def _apply_entry(
    session: AsyncSession,
    t: SheetType,
    doc: ExportDoc,
    meta: CatalogMeta,
    current: SheetTypeVersion,
    followers: _Followers,
) -> None:
    """Make an installed catalog type the entry ``doc``: fields, design, geometry, provenance."""
    fields = ts.clean_fields(_fields(doc, t.key, t.prefix))
    t.vendor = fields.vendor
    t.product_codes = list(fields.product_codes)
    t.description = fields.description
    t.allow_qr = bool(fields.allow_qr)
    t.verified = fields.verified
    t.source_name = fields.source_name
    t.source_url = fields.source_url
    t.notes = fields.notes
    t.id_placement = doc.type.id_placement
    t.id_cell = doc.type.id_cell
    await _link_design(session, t, doc, "seed", followers=followers)
    geometry = geometry_from_doc(doc.version.geometry)
    version_id = current.id
    if geometry_hash(geometry) != current.geometry_hash:
        added = await ts.add_version_locked(
            session,
            t.key,
            geometry,
            actor=CATALOG_ACTOR,
            change_note=f"Catalog revision {meta.revision}",
            log=False,  # the update is logged once below, with its version
        )
        version_id = added.id
    # The log names the person who updated, not the catalog: the version stays catalog-written.
    await type_log.log_entry(
        session,
        type_id=t.id,
        version_id=version_id,
        kind="catalog_update",
        note=f"Updated to catalog revision {meta.revision}",
        actor=followers.actor,
    )
    t.catalog_revision = meta.revision
    t.catalog_fields_hash = loader.fields_hash(doc.type)
    t.updated_at = datetime.now(UTC)


def _fields(doc: ExportDoc, key: str, prefix: str) -> ts.TypeFields:
    t = doc.type
    return ts.TypeFields(
        key=key,
        prefix=prefix,
        vendor=t.vendor,
        product_codes=list(t.product_codes),
        description=t.description,
        allow_qr=t.allow_qr,
        verified=t.verified,  # the catalog's own evidence, not a stranger's claim (D19)
        source_name=t.source_name,
        source_url=t.source_url,
        notes=t.notes,
    )


async def install(
    session: AsyncSession,
    key: str,
    *,
    as_new_key: str | None,
    prefix: str | None,
    actor: str,
) -> CatalogWrite:
    """Install the catalog entry ``key`` (S12); see the module docstring."""
    doc = loader.load_entry(key)  # 404 for a key the catalog does not have
    meta = loader.load_meta(key)
    new_key = as_new_key if as_new_key is not None and as_new_key != key else None
    if new_key is not None:
        _check_new_key(new_key)
    if prefix is not None and not PREFIX_PATTERN.fullmatch(prefix):
        raise InvalidFieldError("prefix", "invalid format")
    target = new_key or key
    wanted_prefix = prefix or doc.type.prefix
    geometry = geometry_from_doc(doc.version.geometry)
    own = new_key is None
    shipped = shipped_design_states() if own else {}  # read before the lock
    async with write_transaction(session):
        if await repo.get_by_key(session, target) is not None:
            raise TypeExistsError()
        if await repo.get_by_prefix(session, wanted_prefix) is not None:
            suggestion = await suggest_prefix(session, wanted_prefix)
            raise PrefixTakenError(detail={"suggested_prefix": suggestion})
        t, _ = await ts.create_type_locked(
            session,
            _fields(doc, target, wanted_prefix),
            geometry,
            # the version is the catalog's under its own key: it counts as unedited (6.1.3)
            actor=CATALOG_ACTOR if own else actor,
            origin="seed" if own else "imported",
            change_note=f"Installed from catalog revision {meta.revision}",
        )
        t.created_by = actor
        t.id_placement = doc.type.id_placement
        t.id_cell = doc.type.id_cell
        followers = _Followers(shipped=shipped, actor=actor) if own else None
        await _link_design(session, t, doc, "seed" if own else "imported", followers=followers)
        if own:
            t.catalog_key = key
            t.catalog_revision = meta.revision
            t.catalog_fields_hash = loader.fields_hash(doc.type)
        if followers is not None:
            await _still_unedited(session, [t, *followers.types])
        await session.flush()
        await session.commit()
    logger.info("catalog entry %s installed as %s", log_value(key), log_value(target))
    return CatalogWrite(t, [] if followers is None else followers.keys)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def apply_update(
    session: AsyncSession,
    key: str,
    *,
    actor: str,
) -> CatalogWrite:
    """Bring an unedited installed type to the newer catalog revision (spec 6.1.3, D14).

    The type fields and the backing design link become the entry's; a new version is added
    only when the entry's geometry differs from the current one, and sheets stay pinned to
    theirs. The provenance is moved in the same transaction, so the type is unedited after it.
    """
    shipped = shipped_design_states()  # every catalog document: read before the lock
    async with write_transaction(session):
        t = await ts.get_type(session, key)
        if t.catalog_key is None:
            raise NoCatalogUpdateError()
        try:
            meta = loader.load_meta(t.catalog_key)
        except CatalogEntryNotFoundError:
            raise NoCatalogUpdateError() from None
        if meta.revision <= (t.catalog_revision or 0):
            raise NoCatalogUpdateError()
        current, design = await provenance_inputs(session, t)
        if is_locally_edited(t, current, design):
            raise TypeLocallyEditedError()
        doc = loader.load_entry(t.catalog_key)
        followers = _Followers(shipped=shipped, actor=actor)
        await _apply_entry(session, t, doc, meta, current, followers)
        await _still_unedited(session, [t, *followers.types])
        await session.commit()
    logger.info(
        "sheet type %s updated to catalog revision %d (also %s)",
        log_value(key),
        meta.revision,
        log_value(followers.keys),
    )
    return CatalogWrite(t, followers.keys)
