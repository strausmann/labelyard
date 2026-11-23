<!--
SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# 0028 — Sheet types ship as a catalog in the image instead of being seeded

- **Status:** Proposed
- **Date:** 2026-11-22
- **Deciders:** maintainer
- **Decision labels:** D32
- **Amends:** [0023](0023-sheet-archive.md) — supersedes its "Seed" bullet, including the `adopt`/`corrections` rule of #334 described in its amendment of 2026-11-21, and OC-10; changes D14's "a type that has sheets is archived instead of deleted" for types without sheets
- **Related:** [0034](0034-database-migrations-and-the-way-back.md) (the way back), [0021](0021-api-versioning.md) (compatibility within `/api/v1`)

## Context

Up to 1.7.x the hub inserts its 39 bundled sheet types on every start from one seed file
(`backend/app/seed/sheet_types.json`). The seeder decides on its own which shipped change reaches an
installed type: a new geometry is added while nobody edited or used the type, and since 1.7.0 two
exception maps (`adopt`, `corrections`, #334) let a shipped change through even for a type in use
(ADR 0023, amendment of 2026-11-21). Three things do not fit any more (#335):

- A fresh installation gets 39 types it may never use, and an admin cannot remove them: only custom
  types without sheets may be deleted (OC-10), every other type is archived.
- A shipped change is applied by the hub itself. The maintainer cannot offer a better geometry without
  either touching installations silently or adding another exception map.
- The seed file has no place for what a catalog needs to say about an entry: its revision, when and
  how it was verified on paper, and what an installed copy looked like when it was shipped.

## Decision

**Sheet types ship as a read-only catalog in the image; an admin installs and updates them, and the
hub never changes an installed type on its own.**

- **The catalog is a folder of exchange documents.** `backend/app/catalog/sheet_types/<key>.json` is
  one sheet-type export document (the import format of ADR 0023, unchanged) per type. What the
  document cannot carry lives in the sidecar `backend/app/catalog/meta.json`, per key: `revision`
  (from 1, raised by every new geometry or field change of the entry), `verified_on`, `evidence`
  (at most 500 characters), `baseline_fields_hash` and `prior_fields_hashes`.
  `backend/app/catalog/index.json` is generated from both by `scripts/catalog_index.py`; a CI step
  (`--check`) and a test fail when the index, the sidecar and the documents disagree. `backend/app/catalog/presets/` holds preprint presets.
- **Two hashes identify an entry.** The geometry hash is the one the type service stores per
  version (D14). The fields hash is the SHA-256 of the canonical JSON (sorted keys, no spaces) of the
  document's `type` object without `key` and `prefix` and **without fields that hold their default
  value**, so a later field with a default changes no hash. A linked backing design is always part
  of it: the catalog documents of the shipped UltraGrip types (Avery Zweckform 6121 and 3490) embed
  their design, and an installed type is hashed with its linked design embedded the same way, so a
  shipped link never counts as an edit.
- **The baseline is what 1.7.x installed.** `baseline_fields_hash` is the fields hash of the
  revision-1 document, which is the seed of revision 3 with its corrections applied. It is written
  once and never changed by a later revision. Upgraded installations are stamped with revision 1 and
  this baseline, so a later revision is offered to them as an update.
- **Older shipped states count as unedited too.** An installation first seeded before 1.6.0 kept
  `allow_qr` off for three types (Avery 5167, HERMA 10001, Avery Zweckform L4731REV) that 1.6.0
  only turned on in the seed, and the seeder never changes a row field afterwards. The hashes of
  such states are listed per key in `prior_fields_hashes`; a type whose fields hash is the baseline
  or one of them counts as unedited. They are derived by seeding a database with the older seed
  files and then with the 1.7.1 seed, never written by hand, and a test repeats this for v1.5.2.
- **Nothing is inserted on start.** A fresh installation starts with no types and installs from the
  catalog. Existing installations keep their types: a one-time startup step applies the pending
  `adopt`/`corrections` of seed revision 3 that an installation has not run yet, then records catalog
  provenance on every seeded type. When it fails it leaves a notice in the existing notices store
  (`internal.notices` in `app_settings`, written by `_append_notice` in
  `backend/app/seed/sheet_seed.py`) and runs again on the next start. After that step the
  `adopt`/`corrections` maps are history; no new entry is added to them.
- **Updates are offered, never applied by themselves.** An installed type that was not edited
  locally and whose catalog entry has a higher revision is offered the update; applying it adds a
  new version (sheets stay pinned to theirs, D14). A locally edited type only says that the catalog
  has a newer version.
- **Installing is importing, with the catalog's own word.** Under its own key an installed
  entry is a bundled type (`origin="seed"`) with catalog provenance; under a new key
  (`as_new_key`) it is an imported copy without provenance. Both keep the entry's `verified`:
  it is the maintainer's evidence on paper, not a stranger's claim, so the rule that an upload
  loses `verified` (D19) does not apply.
- **A backing design follows the catalog only while nobody depends on it differing.** When an
  install or an update brings an entry whose backing design differs from the local design of
  that key, the local design is brought to the entry's state only if it still holds a state the
  hub shipped; otherwise the write is refused with `backing_design_differs`, naming the design.
  Other types that link the design are updated together with it in the same transaction: each
  must be an unedited catalog type whose newest catalog entry carries the identical new design,
  and then gets its own catalog update (fields, a geometry version only when its geometry
  changed, provenance) by the catalog actor; the answer lists them in `also_updated`. If any of
  them is edited, custom or imported, or its entry carries another design, the write is refused
  with `backing_design_shared`, naming those types. A catalog write never reports success while
  any type it touched ends up edited (operator and controller rulings, 2026-11-23).
- **Any type without sheets can be deleted** (catalog, imported or custom). This replaces OC-10 and
  the part of D14 that archives every type: a type **with** sheets is still archived instead of
  deleted. The prefix counter of a deleted type is kept, so a re-added type never issues a sheet code
  twice.

## Options considered

### Option A — catalog folder with a sidecar and a generated index (picked)

- Pros: each entry is a document the import already validates, so installing is importing; the
  maintainer reviews a change as a diff of one file plus one sidecar line; an installation decides
  when a change reaches it; unused types can go.
- Cons: three files describe an entry, so a generated index and a CI check are needed to keep them
  in step; existing installations need a one-time provenance step.

### Option B — keep seeding and add more exception maps

- Pros: no new concept, no startup step.
- Cons: every shipped fix needs another rule in the seeder; installations still change without the
  admin's choice; fresh installations still get every type.

### Option C — fetch the catalog from a server at runtime

- Pros: updates without a new image.
- Cons: the hub would need network access to an external service it has never needed; an offline
  installation would see no catalog; the content of the image would no longer say what it ships.

## Consequences

- `backend/app/catalog/` ships in the image (wheel data); `backend/app/catalog/loader.py` reads it
  (`load_index`, `load_entry`, `load_meta`, `load_preset`, `fields_hash`, `type_doc_of`).
- The seed file stays in the image only for the one-time step that migrates installations that never
  ran its `adopt`/`corrections`.
- Sheet types gain catalog provenance (key, installed revision, fields hash at install); the catalog
  API lists, installs and updates entries; the archive gains a Catalog tab.
- Deleting a type without sheets is a loosening of the API and is named in the upgrade notes.
- Code that follows this decision cites it as "ADR 0028" until it is accepted with the milestone.

## References

- Issue: #335 (catalog), #334 (seed corrections), #350 (backing designs)
- Spec: milestone 1.8.0 design, section 6.1 (S3, S12, S14)
- Related ADRs: [0023](0023-sheet-archive.md) (amended), [0034](0034-database-migrations-and-the-way-back.md)
