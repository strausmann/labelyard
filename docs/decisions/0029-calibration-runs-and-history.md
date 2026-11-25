<!--
SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# 0029 — Every calibration print is a numbered run; a saved correction is history

- **Status:** Proposed
- **Date:** 2026-11-24
- **Deciders:** maintainer
- **Decision labels:** D34, D35
- **Amends:** [0023](0023-sheet-archive.md) — D17's stored calibration of a tray is no longer overwritten in place: every save, revert and reset is a new history entry, and the row in `printer_calibrations` is the newest of them
- **Related:** [0034](0034-database-migrations-and-the-way-back.md) (every migration has a downgrade; new payload keys only where unknown keys are ignored), [0020](0020-backend-kinds-ipp-page-printers.md) (IPP page printers)

## Context

Up to 1.7.x a calibration page is a job and nothing more (ADR 0023, D17). The person prints it,
measures the crossings and saves the values; the save overwrites the tray's row in
`printer_calibrations`. Three things are missing (#336, #240):

- **Which print a reading belongs to.** After two or three pages on the same tray nobody can tell
  them apart on paper, and the hub cannot tell which page the entered readings came from.
- **What was there before.** A save replaces the previous correction. A wrong save cannot be undone
  except by measuring again.
- **Which tray the page really came from.** A printer may take paper from another tray than the one
  requested (an empty tray, a tray set to "auto"). A correction measured on the wrong tray is saved
  for the requested one.

The 1.8.0 design (`docs/superpowers/specs/2026-11-21-milestone-1-8-0-design.md`, sections 7.1, 7.2
and 7.5) adds check sheets that take up to three passes on one sheet, and the photo calibration
(#333) later needs to resolve a printed code to the print it came from. Both need a stable record
per print.

## Decision

**Every calibration, check and probe page the hub sends is a run with a number per printer and
tray (D34).** A run row (`calibration_runs`) records the printer, the tray, `run_no`, the kind
(`calibration_page`, `check_page`, `probe_page`), the job, the sheet type version, the correction
applied (null for a calibration page), the check sheet and pass (when it is one), the requested tray,
the tray the printer reported, who sent it and when. The number comes from a counter row
(`calibration_run_counters`, one per printer and tray) taken under the sheet write lock in the same
transaction that inserts the run; a number is never given out twice, also not after a failed
submit or after retention dropped old runs. A failed submit deletes its run and leaves a gap. A
tray key the printer renames starts again at 1; the old runs stay under the old key.

- The run number, the hub's local time (`HH:MM`) and the run id go into the page payload's
  `detail` at submit. A retry copies the payload, so it prints the same number; the run list shows
  it as `#7 (retry)`. `detail` ignores unknown keys in every release since 1.7.0, so a downgraded hub
  still renders the job (ADR 0034). `CalibrationSnapshot` stays unchanged.
- Whether a run reached the queue is **derived**, never stored: a run whose job row is missing was
  not submitted, but only inside the job retention window (`jobs.retention_days`); an older run
  without a job row had its job cleaned up and counts as submitted.
- After an IPP job ends, the hub asks the printer once for the job's `media-col-actual`
  (`media-source`). When the printer names a source that differs from the requested tray, the run
  stores it as bounded text (64 characters, cleaned, never interpreted). When the printer names none,
  or the question fails, the run says "not reported". No operator input is needed.
- Retention: 1000 runs per printer and tray, the oldest dropped first, applied after a successful submit. A run still in flight (no job row yet, inside the job retention window) is neither counted nor dropped. The counter is not touched.
- A printer's runs, history and check sheets go with the printer (foreign keys with `CASCADE`). A
  deleted sheet type leaves them readable: they keep the version id without a foreign key.

**A saved correction is a history entry; a save never overwrites one.** `calibration_history` holds
one entry per save, revert, reset and import, numbered per printer and tray (`seq`). The row in
`printer_calibrations` stays the value the renderer reads; it is the newest entry. A revert writes a
new entry with an older entry's values and never deletes. 1.8.0 imports each existing calibration as
the first entry of its tray (action `imported`). Retention: 500 entries per printer and tray.

- The entry is written in the transaction that writes `printer_calibrations`, under the sheet write
  lock; `seq` is the tray's highest plus one, so concurrent saves are numbered in commit order.
- **With readings, the stored values are the hub's fit.** A save that carries readings is fitted
  again on its measured-on version and the fit is stored (`source: fit`) with its residuals; a sent
  value further than 0.05 mm from the fit (offsets directly, scale and rotation as the displacement
  at the page corner) is replaced and named. Values typed by hand are stored as sent
  (`source: manual`). The history must show values its stored readings produce.
- **Jobs link.** A page job names the entry it was printed with in `PagePayload.calibration_history_id`,
  not in `CalibrationSnapshot`, which 1.7.x reads with `extra="forbid"` (ADR 0034). Jobs from before
  1.8.0 belong to an entry by equal snapshot values inside its window (to the tray's next entry). The
  id on the payload, not a join table: the payload already travels with every job and survives
  retries.

**Check sheets take passes.** `calibration_check_sheets` holds a sheet that is printed in up to three
passes; `calibration_check_sheet_passes` links each pass to its run (one pass per run, one run per
pass). Readings of pass 2 and 3 are attributed through this table, not through codes on the paper.

**The codes on a calibration page use the `LYC1` grammar (D35).** QR alphanumeric mode, a page code
`LYC1:<run>:0:<PREFIX>:<version>:<dx_hmm>:<dy_hmm>:<sx_ppm_dev>:<sy_ppm_dev>:<rot_udeg>[:K1][:PX…][:PY…][:MT…][:ML…]`
and a cell code `LYC1:<run>:<cell>`. A printed code is a durable format: once a release prints it,
later releases must read it. The grammar (design section 7.2, `app/calibration/codes.py`):

```
page:  LYC1:<run>:0:<PREFIX>:<version>:<dx_hmm>:<dy_hmm>:<sx_ppm_dev>:<sy_ppm_dev>:<rot_udeg>[:K1][:PX<pitch_x_hmm>][:PY<pitch_y_hmm>][:MT<margin_top_hmm>][:ML<margin_left_hmm>]
cell:  LYC1:<run>:<cell>
```

- **Field order is fixed** as written; fields are separated by `:`, numbers are decimal integers
  with an optional leading `-`. A reader must accept the optional fields in this order and ignore
  none of the mandatory ones.
- **Characters:** only `0-9 A-Z : -`, so every code is QR alphanumeric mode. A payload with any
  other character is refused when it is built.
- `<run>` is the run number (D34). `<cell>` is `0` in a page code and the cell number from 1 in a
  cell code. `<PREFIX>` is the sheet type's prefix (`^[A-Z][A-Z0-9]{1,9}$`) and `<version>` its
  version as a **separate** field: prefixes end in digits, so `A7160` version 1 and `A71` version
  601 would both read `A71601` as one token.
- The correction fields are the applied `Deviation`: offsets in hundredths of a millimetre, the
  scales as parts per million **minus 1000000**, rotation in micro-degrees. A page printed without
  correction carries `0` in all five.
- `:K1` marks pass 1 of a check sheet; a calibration page has no `K` field. Cell codes carry no
  pass suffix; passes 2 and 3 print no codes at all.
- `PX`/`PY`/`MT`/`ML` carry the current value of pitch x, pitch y, margin top and margin left, each
  only when it differs from the **newest version of the type written by the catalog actor**
  (`system:catalog`, or `system:seed` from 1.7.x) in the database, never from the entry in the
  image: a newer catalog revision in the image does not make an unedited type look edited. A
  catalog-written version itself, and a type without any catalog-written version (custom,
  imported, installed under a new key), carry none of them.
- **QR floors:** error correction M. Page codes are drawn as a 10 mm symbol at version 5 at least
  (the longest payload of the bounds, 106 characters, fits 5-M); cell codes as an 8 mm symbol with
  its quiet zone, at version 1 (the longest, `LYC1:999999:400`, fits 1-M). A module is never below
  0.25 mm. The page code's 4-module quiet zone lies **outside** the 10 mm symbol (blank paper
  around it), so its 37 modules span the full 10 mm.
- **Never truncated.** A page code that needs a version whose module would drop below 0.25 mm is
  refused with `422 calibration_qr_too_large` (`error_detail`: `length`, `version_needed`,
  `max_version`; `version_needed` is `null` when no QR version holds the payload); at 10 mm no
  version above 5 keeps 0.25 mm modules, so in practice a page code never grows. A cell code that
  does not fit is left out and the cell prints its number only.
- **No code a scanner acts on by itself:** no payload may contain `@`, `://`, `mailto:`, `tel:`,
  `sms:`, `WIFI:`, `geo:`, `BEGIN:` or start with `+` and a digit; a test checks every catalog
  type, correction, page kind and cell. Scanner apps recognise a content type (`tel:`, `geo:`,
  `WIFI:`, `BEGIN:VCARD`, `mailto:`, `sms:`) at the **start** of the content, and every `LYC1`
  payload starts with `LYC1:`, so a custom prefix such as `TEL` or `GEO` inside the payload does
  not change how it is detected (a second test checks those prefixes at the start).

## Options considered

### Option A — a counter row per printer and tray (picked)
- Pros: numbers stay monotonic after retention or a failed submit deletes runs; one indexed row
  under the write lock that every sheet write already takes.
- Cons: one more table, and a gap in the numbers after a failed submit.

### Option B — `MAX(run_no) + 1`
- Pros: no counter table.
- Cons: deleting the newest run (a failed submit) or retention hands its number out again; two
  pages with the same number on paper are exactly what the runs exist to avoid.

### Option C — store "submitted" on the run
- Pros: no join with `jobs`.
- Cons: a second state that can disagree with the job row, and a write after the submit that can
  fail or be cancelled. Derived from the job row there is nothing to keep in step.

### Option D — overwrite the calibration row, keep a log beside it
- Pros: no change to the renderer's read.
- Cons: the log and the row can disagree; a revert would be a second kind of write. With the row as
  the newest history entry there is one write path.

## Consequences

- Migration C1 (`20261121c1`) adds the five tables and imports one history entry per existing
  calibration. Its downgrade drops them; `printer_calibrations` is untouched by both directions.
  Calibration pages printed before 1.8.0 have no run.
- A calibration page submit writes its run before the job. A cancellation of the request after the
  run is committed leaves the run: the job row may already exist and print after a restart.
- A printer that does not report `media-col-actual` shows "not reported" for every run; nothing is
  guessed.
- The run list (`GET /api/v1/printers/{id}/calibration/{tray}/runs`) needs `read` and the key's
  printer access; the printer-reported tray name is shown only there.
- `jobs_count` in the history counts every job on the printer printed with the entry, of every
  key; `GET /api/v1/jobs?calibration=<id>` shows a key without the admin scope only its own jobs,
  so its list can be shorter. A count carries no job data, as the printer-wide queue depth does.
- Reset entries are also written when the admin removes a calibrated tray or resolves a device swap
  as `replaced`, in the transaction that deletes the calibration, with the same actor form as every
  other entry (`user:<name>` or `key:<id>`), never the admin audit string.
- The history (`GET …/calibration/{tray}/history`) needs `read` and the printer access; restore
  (`POST …/history/{id}/restore`) needs `admin` and writes a `revert` entry.
- Follow-up in 1.8.0: the check passes and the page layout with the `LYC1` codes (#240), the run
  picker of the reading form and the history view.

## References

- Issues: #336, #240, #333
- Design: `docs/superpowers/specs/2026-11-21-milestone-1-8-0-design.md` sections 7.1, 7.2, 7.5, 7.8
- Related ADRs: [0023](0023-sheet-archive.md) (amended), [0034](0034-database-migrations-and-the-way-back.md)
