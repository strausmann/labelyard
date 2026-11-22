# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Catalog provenance of installed sheet types and the upgrade of existing installations.

ADR 0028, spec 6.1.3 and 6.1.4. An installed type remembers the catalog entry it is a copy of
(``catalog_key``), the installed revision (``catalog_revision``) and the fields hash of the
entry's type fields at install (``catalog_fields_hash``).

``run_catalog_migration`` is the startup step that moves an installation from the 1.7.x seeder
to the catalog. It is not an Alembic revision: it needs the seed file and the catalog of the
image. In one transaction it

1. returns at once when ``internal.catalog.migrated`` is set;
2. runs the ``adopt``/``corrections`` of the seed that the installation has not applied yet;
3. stamps every ``origin="seed"`` type without provenance with ``catalog_key = key``,
   ``catalog_revision = 1`` and the fields hash of what the installed row is: its own hash when
   that is a state the catalog shipped (the baseline or a prior hash), otherwise the baseline,
   so the type counts as edited. Never the current revision: a later revision must reach an
   upgraded installation as an offered update;
4. sets the marker.

A failure rolls all of it back, leaves one notice for the notification centre and is tried
again on the next start. Nothing is inserted: an installation from before 1.7.0 that missed a
later seed type or backing design gets it from the catalog.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import col

from app.catalog import loader
from app.models.app_setting import AppSetting
from app.models.backing_design import BackingDesign
from app.models.sheet import SheetType, SheetTypeVersion
from app.repositories.write_lock import write_transaction
from app.seed import sheet_seed
from app.sheets.errors import CatalogEntryNotFoundError
from app.sheets.text import log_value

logger = logging.getLogger(__name__)

MIGRATED_KEY: Final = "internal.catalog.migrated"
CATALOG_ACTOR: Final = "system:catalog"
# Versions the catalog wrote: the catalog actor, and the 1.7.x seeder before it.
CATALOG_ACTORS: Final = frozenset({CATALOG_ACTOR, sheet_seed.SEED_ACTOR})
FAILED_NOTICE_ID: Final = "catalog-migration-failed"
UPGRADE_REVISION: Final = 1  # what a 1.7.x installation holds is catalog revision 1 (S14)


@dataclass
class CatalogMigrationReport:
    ran: bool = False  # False: the marker was set, nothing was done
    failed: bool = False
    stamped: list[str] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)  # stamped with the baseline, edited locally
    adopted: list[str] = field(default_factory=list)
    corrected: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CatalogUpdateState:
    """What the catalog has for an installed type (spec 6.1.3)."""

    update_available: bool = False  # unedited, and the catalog has a higher revision
    catalog_newer: bool = False  # edited, and the catalog has a higher revision
    catalog_missing: bool = False  # the type has provenance, the image no longer has the entry


def is_locally_edited(
    t: SheetType, current: SheetTypeVersion, design: BackingDesign | None
) -> bool:
    """Whether a type differs from what the catalog installed (spec 6.1.3).

    Edited when its current version was not written by the catalog actor, or the fields hash of
    the type with its linked design embedded differs from the one stored at install. A type
    without provenance, or one whose fields no exchange document can carry
    (``type_doc_of`` gives ``None``), counts as edited. ``design`` is the design ``t`` links to.
    """
    if t.catalog_fields_hash is None:
        return True
    if current.created_by not in CATALOG_ACTORS:
        return True
    doc = loader.type_doc_of(t, design)
    return doc is None or loader.fields_hash(doc) != t.catalog_fields_hash


def catalog_update_state(
    t: SheetType, current: SheetTypeVersion, design: BackingDesign | None
) -> CatalogUpdateState:
    """Whether the catalog offers ``t`` an update, or only says it has a newer version."""
    if t.catalog_key is None:
        return CatalogUpdateState()
    try:
        meta = loader.load_meta(t.catalog_key)
    except CatalogEntryNotFoundError:
        return CatalogUpdateState(catalog_missing=True)
    if meta.revision <= (t.catalog_revision or 0):
        return CatalogUpdateState()
    if is_locally_edited(t, current, design):
        return CatalogUpdateState(catalog_newer=True)
    return CatalogUpdateState(update_available=True)


async def provenance_inputs(
    session: AsyncSession, t: SheetType
) -> tuple[SheetTypeVersion, BackingDesign | None]:
    """The current version of ``t`` and the design it links to (the inputs of the checks)."""
    current = (
        await session.execute(
            select(SheetTypeVersion)
            .where(col(SheetTypeVersion.type_id) == t.id)
            .order_by(col(SheetTypeVersion.version).desc())
            .limit(1)
        )
    ).scalar_one()
    design = (
        await session.get(BackingDesign, t.backing_design_id)
        if t.backing_design_id is not None
        else None
    )
    return current, design


async def _stamp(session: AsyncSession, report: CatalogMigrationReport) -> None:
    """Give every seeded type without provenance the provenance of what it is (step 3)."""
    query = select(SheetType).where(
        col(SheetType.origin) == "seed", col(SheetType.catalog_key).is_(None)
    )
    for t in list((await session.execute(query)).scalars()):
        try:
            meta = loader.load_meta(t.key)
        except CatalogEntryNotFoundError:
            logger.warning("seeded type %s has no catalog entry: no provenance", log_value(t.key))
            continue
        design = (
            await session.get(BackingDesign, t.backing_design_id)
            if t.backing_design_id is not None
            else None
        )
        doc = loader.type_doc_of(t, design)
        own = None if doc is None else loader.fields_hash(doc)
        shipped = own is not None and meta.counts_as_unedited(own)
        t.catalog_key = t.key
        t.catalog_revision = UPGRADE_REVISION
        # the row's own hash only when the catalog shipped that state; else the baseline (edited)
        t.catalog_fields_hash = own if shipped and own is not None else meta.baseline_fields_hash
        report.stamped.append(t.key)
        if not shipped:
            report.edited.append(t.key)
    await session.flush()


async def _migrate(session: AsyncSession, report: CatalogMigrationReport) -> None:
    seed_report = sheet_seed.SeedReport()
    await sheet_seed.apply_pending_fixes(session, seed_report)
    report.adopted, report.corrected = seed_report.adopted, seed_report.corrected
    await _stamp(session, report)
    session.add(
        AppSetting(
            key=MIGRATED_KEY,
            value_json=json.dumps({"revision": UPGRADE_REVISION, "stamped": len(report.stamped)}),
        )
    )
    await session.flush()


async def _leave_failure_notice(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session, write_transaction(session):
        await sheet_seed._append_notice(
            session,
            kind="catalog_migration_failed",
            key="",
            code="",
            revision=UPGRADE_REVISION,
            now=datetime.now(UTC),
            notice_id=FAILED_NOTICE_ID,
        )
        await session.commit()


async def run_catalog_migration(
    session_factory: async_sessionmaker[AsyncSession],
) -> CatalogMigrationReport:
    """The idempotent catalog startup step (A3); see the module docstring."""
    report = CatalogMigrationReport()
    try:
        async with session_factory() as session, write_transaction(session):
            if await session.get(AppSetting, MIGRATED_KEY) is not None:
                return report
            report.ran = True
            await _migrate(session, report)
            await session.commit()
    except Exception:
        logger.exception("The sheet type catalog step failed; it runs again on the next start")
        report = CatalogMigrationReport(ran=True, failed=True)
        try:
            await _leave_failure_notice(session_factory)
        except Exception:
            logger.exception("The notice of the failed catalog step could not be stored")
        return report
    logger.info(
        "sheet type catalog step: %d stamped (%d edited), %d adopted, %d corrected",
        len(report.stamped),
        len(report.edited),
        len(report.adopted),
        len(report.corrected),
    )
    return report
