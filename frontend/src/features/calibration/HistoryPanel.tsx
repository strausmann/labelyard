// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { Button } from "react-aria-components";
import type { SheetTypeRead } from "../archive/api";
import { typeLabel } from "../sheets/typeLabel";
import { HINT } from "../settings/controls";
import { SECONDARY_BUTTON, SECTION_HEADING } from "../ui";
import { HISTORY_KEPT, useCalibrationHistory, useCalibrationRuns, type CalibrationHistoryEntry } from "./api";
import { changeText, HISTORY_FIELDS, largestResidual, UNIT, valueText, type HistoryField } from "./historyFormat";
import { RestoreDialog } from "./RestoreDialog";
import { RunsTable, useRunTime, WarnIcon } from "./RunsTable";

const SMALL_BUTTON =
  "press inline-flex min-h-11 items-center justify-center gap-2 rounded-r2 border border-line-strong bg-surface px-3 text-small font-bold text-ink transition-colors duration-150 hover:bg-sunken data-[disabled]:opacity-60 data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link";
const TEXT_LINK = "inline-flex min-h-11 items-center text-small text-link underline underline-offset-2";

function UndoIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" className="shrink-0 fill-none stroke-current stroke-[1.6] [stroke-linecap:round] [stroke-linejoin:round]">
      <path d="M5.5 4 2.5 7l3 3M2.5 7H10a3.5 3.5 0 0 1 0 7H7" />
    </svg>
  );
}

interface HistoryPanelProps {
  printerId: string;
  printerName: string;
  tray: string;
  trayLabel: string;
  labelOf: (tray: string) => string;
  types: SheetTypeRead[];
  admin: boolean;
}

/** The History tab (#336, mockup M7): the actual-tray warning, every saved correction of the tray and the pages printed from it. */
export function HistoryPanel({ printerId, printerName, tray, trayLabel, labelOf, types, admin }: HistoryPanelProps) {
  const { t } = useLingui();
  const history = useCalibrationHistory(printerId, tray);
  const runs = useCalibrationRuns(printerId, tray);
  const [restoring, setRestoring] = useState<CalibrationHistoryEntry | null>(null);
  const entries = history.data?.pages.flat() ?? [];
  const more = history.hasNextPage;
  // The newest page that the printer fed from another tray than the one asked for.
  const mismatch = runs.data?.find((run) => run.actual_media_source !== null);

  return (
    <div className="flex min-w-0 flex-col gap-8">
      {mismatch ? (
        <div role="note" aria-labelledby="cal-mismatch-h" className="flex items-start gap-3 rounded-r3 border border-warn bg-accent-soft p-4">
          <span className="mt-0.5">
            <WarnIcon size={18} />
          </span>
          <div className="flex min-w-0 flex-col gap-1 [overflow-wrap:anywhere]">
            <p id="cal-mismatch-h" className="m-0 font-bold">
              {t`Print #${mismatch.run_no} came from ${labelOf(mismatch.actual_media_source!)}`}
            </p>
            <p className="m-0 text-small">
              {t`You asked for ${labelOf(mismatch.requested_tray)} but the printer reported that the page was fed from ${labelOf(mismatch.actual_media_source!)}. The correction you save from it belongs to ${labelOf(mismatch.actual_media_source!)}.`}
            </p>
          </div>
        </div>
      ) : null}

      <section aria-labelledby="cal-history-h" className="flex min-w-0 flex-col gap-4">
        <div className="flex flex-col gap-1">
          <h2 id="cal-history-h" className={SECTION_HEADING}>{t`Corrections for this tray`}</h2>
          <p className={`${HINT} m-0 max-w-prose text-body`}>{t`Every save is kept. Restoring an entry adds a new one; nothing is overwritten or deleted.`}</p>
        </div>
        {history.isPending ? (
          <p role="status" className="text-ink-2">{t`Loading…`}</p>
        ) : history.isError ? (
          <p role="alert" className="text-bad">{t`The history could not be loaded.`}</p>
        ) : entries.length === 0 ? (
          <p className="text-ink-2">{t`Nothing saved for this tray yet.`}</p>
        ) : (
          <>
            <ul aria-label={t`Corrections for this tray`} className="m-0 flex list-none flex-col overflow-hidden rounded-r3 border border-line bg-surface p-0">
              {entries.map((entry, i) => (
                <Entry
                  key={entry.id}
                  entry={entry}
                  current={i === 0}
                  entries={entries}
                  types={types}
                  onRestore={admin && i !== 0 ? () => setRestoring(entry) : undefined}
                />
              ))}
            </ul>
            <div className="flex flex-wrap items-center gap-3">
              <p className={`${HINT} m-0`}>
                {more
                  ? t`Showing the newest ${entries.length} entries. The hub keeps the newest ${HISTORY_KEPT} per printer and tray.`
                  : t`Showing ${entries.length} of ${entries.length} entries. The hub keeps the newest ${HISTORY_KEPT} per printer and tray.`}
              </p>
              {more ? (
                <Button isDisabled={history.isFetchingNextPage} onPress={() => void history.fetchNextPage()} className={SECONDARY_BUTTON}>
                  {t`Show older entries`}
                </Button>
              ) : null}
            </div>
          </>
        )}
      </section>

      <section aria-labelledby="cal-runs-h" className="flex min-w-0 flex-col gap-4">
        <div className="flex flex-col gap-1">
          <h2 id="cal-runs-h" className={SECTION_HEADING}>{t`Pages printed from this tray`}</h2>
          <p className={`${HINT} m-0 max-w-prose text-body`}>{t`Every calibration, check and probe page has a run number. A retry keeps its number.`}</p>
        </div>
        {runs.isPending ? (
          <p role="status" className="text-ink-2">{t`Loading…`}</p>
        ) : runs.isError ? (
          <p role="alert" className="text-bad">{t`The printed pages could not be loaded.`}</p>
        ) : runs.data.length === 0 ? (
          <p className="text-ink-2">{t`No calibration, check or probe page printed from this tray yet.`}</p>
        ) : (
          <RunsTable runs={runs.data} labelOf={labelOf} />
        )}
      </section>

      <RestoreDialog printerId={printerId} printerName={printerName} tray={tray} trayLabel={trayLabel} entry={restoring} onClose={() => setRestoring(null)} />
    </div>
  );
}

function useActionBadge(): (action: CalibrationHistoryEntry["action"]) => string {
  const { t } = useLingui();
  return (action) => ({ save: t`Save`, revert: t`Revert`, reset: t`Reset`, imported: t`Imported` })[action];
}

function useFieldNames(): Record<HistoryField, string> {
  const { t } = useLingui();
  return { offset_x_mm: t`Across`, offset_y_mm: t`Down`, scale_x: t`Scale across`, scale_y: t`Scale down`, rotation_deg: t`Rotation` };
}

interface EntryProps {
  entry: CalibrationHistoryEntry;
  current: boolean;
  /** The loaded entries, to name the one a revert came from. */
  entries: CalibrationHistoryEntry[];
  types: SheetTypeRead[];
  onRestore?: () => void;
}

function Entry({ entry, current, entries, types, onRestore }: EntryProps) {
  const { t, i18n } = useLingui();
  const badge = useActionBadge();
  const names = useFieldNames();
  const time = useRunTime();
  const how = useHowText(entries, types);
  const [showReadings, setShowReadings] = useState(false);
  const printNo = (n: number) => t`print #${n}`;
  const readings = entry.readings ?? [];
  const pill = "inline-flex items-center gap-1 rounded-full px-2.5 text-small leading-7 font-bold";
  return (
    <li className={`flex min-w-0 flex-col gap-3 border-b border-line p-4 last:border-b-0 ${current ? "bg-sunken" : ""}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex min-w-0 flex-wrap items-center gap-2">
          <span data-testid="seq" className="inline-flex size-7 items-center justify-center rounded-full bg-ink font-mono text-small text-surface">
            {entry.seq}
          </span>
          <span className={`${pill} ${entry.action === "revert" ? "bg-info-soft text-link" : "border border-line-strong text-ink"}`}>
            {entry.action === "revert" || entry.action === "imported" ? <UndoIcon /> : null}
            {badge(entry.action)}
          </span>
          {current ? (
            <span className={`${pill} bg-info-soft text-ok`}>
              <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" className="fill-none stroke-current stroke-2">
                <path d="m3 8.5 3 3 7-7" />
              </svg>
              {t`In use now`}
            </span>
          ) : null}
          <span className="text-small text-ink-2">
            {entry.action === "imported" ? t`before 1.8.0` : <time dateTime={entry.created_at}>{time(entry.created_at)}</time>} · {entry.actor ?? "—"}
          </span>
        </span>
        {onRestore ? (
          <Button onPress={onRestore} aria-label={t`Restore these values (entry ${entry.seq})`} className={SMALL_BUTTON}>
            <UndoIcon />
            {t`Restore these values`}
          </Button>
        ) : null}
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-5">
        {HISTORY_FIELDS.map((field) => {
          const change = changeText(field, entry, i18n.locale);
          return (
            <div key={field} role="group" aria-label={names[field]} className="flex min-w-0 flex-col">
              <span className="text-small text-ink-2">{names[field]}</span>
              <span className="font-mono font-bold">
                {valueText(field, entry.values, i18n.locale)}
                <span className="text-small font-normal text-ink-3"> {UNIT[field]}</span>
              </span>
              {change ? (
                <span className="text-small text-ink-2">
                  {change} <span className="text-ink-3">{t`vs. before`}</span>
                </span>
              ) : null}
            </div>
          );
        })}
      </div>
      <p className="m-0 text-small [overflow-wrap:anywhere]">{how(entry)}</p>
      {entry.note ? <p className="m-0 text-small text-ink-2 [overflow-wrap:anywhere]">{t`Note: ${entry.note}`}</p> : null}
      <div className="flex flex-wrap gap-x-4">
        <Link to="/jobs" search={{ calibration: entry.id }} className={TEXT_LINK}>
          {plural(entry.jobs_count, { one: "# job printed with it", other: "# jobs printed with it" })}
        </Link>
        {readings.length > 0 ? (
          <Button onPress={() => setShowReadings((s) => !s)} aria-expanded={showReadings} className={`${TEXT_LINK} cursor-pointer`}>
            {plural(readings.length, { one: "# reading", other: "# readings" })}
            {entry.run_no !== null ? ` · ${printNo(entry.run_no)}` : null}
          </Button>
        ) : null}
      </div>
      {showReadings ? <Readings entry={entry} /> : null}
    </li>
  );
}

/** The readings a fit came from, with the residual of each where the hub kept it. */
function Readings({ entry }: { entry: CalibrationHistoryEntry }) {
  const { t, i18n } = useLingui();
  const mm = (n: unknown) => (typeof n === "number" ? `${n.toLocaleString(i18n.locale, { maximumFractionDigits: 2 })} mm` : "—");
  return (
    <table aria-label={t`Readings`} className="w-full max-w-lg border-collapse text-small">
      <thead>
        <tr className="text-left text-ink-2">
          <th scope="col" className="py-1 pr-3 font-bold">{t`Crossing`}</th>
          <th scope="col" className="py-1 pr-3 font-bold">{t`Across`}</th>
          <th scope="col" className="py-1 pr-3 font-bold">{t`Down`}</th>
          <th scope="col" className="py-1 font-bold">{t`Residual`}</th>
        </tr>
      </thead>
      <tbody>
        {(entry.readings ?? []).map((r, i) => (
          <tr key={i} className="border-t border-line">
            <td className="py-1 pr-3 font-mono">{`S${String(r.col_gap)} · Z${String(r.row_gap)}`}</td>
            <td className="py-1 pr-3 tabular-nums">{mm(r.across_mm)}</td>
            <td className="py-1 pr-3 tabular-nums">{mm(r.down_mm)}</td>
            <td className="py-1 tabular-nums">{mm(entry.residuals_mm?.[i])}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Where an entry's values came from, in one sentence (mockup M7). */
function useHowText(entries: CalibrationHistoryEntry[], types: SheetTypeRead[]): (entry: CalibrationHistoryEntry) => string {
  const { t, i18n } = useLingui();
  return (entry) => {
    const parts: string[] = [];
    if (entry.action === "imported") return t`The correction this tray had when 1.8.0 was installed. It had no readings.`;
    if (entry.action === "reset") parts.push(t`Reset to no correction.`);
    if (entry.action === "revert") {
      const from = entries.find((e) => e.id === entry.reverted_from_id);
      parts.push(from ? t`Restored from entry ${from.seq}.` : t`Restored from an earlier entry.`);
    }
    if (entry.action === "save") {
      const count = entry.readings?.length ?? 0;
      const worst = largestResidual(entry);
      if (entry.source === "fit" && count > 0) {
        const residual = worst === null ? null : worst.toLocaleString(i18n.locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const fit = entry.run_no !== null ? t`Fit from ${count} readings of print #${entry.run_no}` : t`Fit from ${count} readings`;
        parts.push(residual === null ? `${fit}.` : t`${fit} · largest residual ${residual} mm.`);
      } else if (entry.source === "manual") {
        parts.push(t`Typed by hand, no readings.`);
      }
    }
    if (entry.measured_on_deleted) parts.push(t`Measured on a sheet type version that was deleted since.`);
    else if (entry.measured_on) {
      const type = types.find((x) => x.key === entry.measured_on!.type_key);
      const name = type ? typeLabel(type) : entry.measured_on.type_key;
      const version = entry.measured_on.version;
      parts.push(t`Measured on ${name} v${version}.`);
    }
    return parts.join(" ");
  };
}
