<!--
SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# 0034 — Database migrations and the way back

- **Status:** Proposed
- **Date:** 2026-11-21
- **Deciders:** maintainer

## Context

The hub keeps everything in one SQLite database and changes its shape with Alembic migrations that
run at startup (`app/db/lifespan.py`). Until 1.7.x a migration added a column or a table now and
then; milestone 1.8.0 brings nine of them in one release, two of them pure data migrations (job
error texts, #405; SNMP communities, #412) and one a new job kind (`probe_page`, #241).

An operator who upgrades and finds a problem wants to go back. The 1.7.x image cannot do that by
itself: Alembic in 1.7.x does not know the revisions of 1.8.0, so a 1.7.x image does not start on a
database that 1.8.0 migrated. Two things are needed for a way back:

- a way to bring the database to the 1.7.x shape, and
- job rows that 1.7.x can still read after that: queued and failed jobs are retried and re-rendered
  from their stored payload, and several payload models refuse unknown keys
  (`CalibrationSnapshot`, `MarginIdPayload`, `FeedMarkPayload`, all `extra="forbid"`). A payload
  that 1.8.0 wrote with a new key in one of them would make the 1.7.x worker fail on that job.

The question is which promise the hub makes about going back, and what that promise asks of every
migration and every new payload field.

## Decision

We go back with the downgrade migrations of the newer image or with the backup taken before the
upgrade, and every migration and every stored payload is written so that this works.

- **Every migration has a downgrade.** A schema change drops what it added; a data migration either
  restores the old values (when they can be restored, such as decrypting a sealed secret) or is a
  documented no-op (when the upgrade only removed information, such as free text in job errors). A
  new job kind is removed by its downgrade. Every downgrade is tested on a populated database
  (`backend/tests/integration/test_migration_<rev>.py`: upgrade, seed rows in every touched table,
  downgrade, upgrade again).
- **The way back is documented, not automatic.** The upgrade notes say: going back to 1.7.x is only
  possible with the downgrade migrations from the 1.8.0 image (`alembic downgrade 20261118a2`) or by
  restoring the backup taken before the upgrade; 1.7.x does not start on a database migrated by
  1.8.0.
- **New payload fields go only into containers that ignore unknown keys.** For 1.8.0 these are
  `PagePayload` (pydantic default `extra="ignore"`) including its free `detail` dict,
  `SheetPageDetail` and `SheetCellPayload` (both `extra="ignore"`).
- **Payload models with `extra="forbid"` stay unchanged** within a minor series: `CalibrationSnapshot`,
  `MarginIdPayload` and `FeedMarkPayload` (with `anchor: Literal["top_right"]`). A value they cannot
  express is carried by a field they already have, or by a new field in a tolerant container.
- **A frozen copy of the last release's models guards this.** `backend/tests/fixtures/models_1_7_1.py`
  copies the 1.7.1 payload models with every imported type inlined; it imports nothing from `app`.
  `backend/tests/unit/test_payloads_readable_by_1_7_1.py` builds one payload per job kind with the
  production builders and parses it with the frozen models; every task that writes a new payload key
  adds a case.

## Options considered

### Option A — downgrade migrations plus tolerant payload containers (picked)

- Pros: an operator can go back without losing the jobs and sheets created after the upgrade; the
  rule for payloads is checked by a test against a copy that later code cannot widen; data
  migrations stay honest about what they cannot restore.
- Cons: every migration costs a downgrade and a populated-database test; new payload data has to go
  into the tolerant containers even where a stricter model would read better.

### Option B — backup only

- Pros: no downgrade code to write or test.
- Cons: everything printed after the upgrade is lost on the way back (sheets, cells, calibration,
  jobs); a problem found a week after the upgrade has no good answer.

### Option C — make the older release read the newer database

- Pros: no operator step at all.
- Cons: impossible for a release that is already out; 1.7.x cannot learn the 1.8.0 revisions after
  the fact. Each future release would have to predict the next one's schema.

## Consequences

- Every Alembic revision of 1.8.0 (E1, A2, A1, C1, P1, C2, D1, E2) has a downgrade and a test on a
  populated database. Migration E1 (job error texts) documents its downgrade as a no-op: it removed
  only free text, which cannot be restored and is not needed by 1.7.x.
- `CalibrationSnapshot`, `MarginIdPayload` and `FeedMarkPayload` keep their fields; features that
  need more data put it into `PagePayload.detail`, `SheetPageDetail` or `SheetCellPayload`.
- A job kind 1.7.x does not know (`probe_page`) is removed by the downgrade of the migration that
  introduced it.
- The upgrade notes of every minor release carry the paragraph on the way back.
- Code that follows this decision cites it as "ADR 0034" until it is accepted with the milestone.

## References

- Issue: #405 (first data migration, E1), #412 (E2), #241 (`probe_page`, P1)
- Spec: milestone 1.8.0 design, sections 3.3 and 3.4 (S11, S15)
- Related ADRs: [0023](0023-sheet-archive.md) (job payloads of the sheet archive),
  [0021](0021-api-versioning.md) (compatibility within `/api/v1`)
