// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import type { CalibrationRun } from "./api";

/** A cell of the table; on a phone every row becomes a card and each cell names its column (mockup M7, 390 px). */
const CELL = "px-3 py-3 align-top max-sm:grid max-sm:grid-cols-[7rem_minmax(0,1fr)] max-sm:gap-3 max-sm:px-4 max-sm:py-1 max-sm:before:font-bold max-sm:before:text-small max-sm:before:content-[attr(data-label)]";
const HEAD = "px-3 py-3 text-left text-small font-bold";

export function WarnIcon({ size = 16 }: { size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" className="shrink-0 fill-none stroke-warn stroke-2 [stroke-linejoin:round]">
      <path d="M12 3 2 20h20L12 3ZM12 10v4M12 17v.5" />
    </svg>
  );
}

/** The run's page in words: "Check page", "Probe page (retry)". */
export function useRunKind(): (run: CalibrationRun) => string {
  const { t } = useLingui();
  return (run) => {
    const kind = run.kind === "check_page" ? t`Check page` : run.kind === "probe_page" ? t`Probe page` : t`Calibration page`;
    return run.retry ? t`${kind} (retry)` : kind;
  };
}

export function useRunTime(): (iso: string) => string {
  const { i18n } = useLingui();
  return (iso) => {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleString(i18n.locale, { dateStyle: "medium", timeStyle: "short" });
  };
}

/** "Pages printed from this tray" (#336, #240, M7): run number, page, time, requested and actual tray. */
export function RunsTable({ runs, labelOf }: { runs: CalibrationRun[]; labelOf: (tray: string) => string }) {
  const { t } = useLingui();
  const kindOf = useRunKind();
  const time = useRunTime();
  const columns = { run: t`Run`, page: t`Page`, printed: t`Printed`, requested: t`Requested tray`, from: t`Printed from` };
  return (
    <div className="overflow-hidden rounded-r3 border border-line bg-surface">
      <table aria-label={t`Pages printed from this tray`} className="w-full border-collapse text-small">
        <thead className="max-sm:sr-only">
          <tr className="border-b border-line">
            <th scope="col" className={HEAD}>{columns.run}</th>
            <th scope="col" className={HEAD}>{columns.page}</th>
            <th scope="col" className={HEAD}>{columns.printed}</th>
            <th scope="col" className={HEAD}>{columns.requested}</th>
            <th scope="col" className={HEAD}>{columns.from}</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={`${run.id}/${run.job_id}`} className="border-b border-line last:border-b-0 max-sm:block max-sm:py-3">
              <td data-label={columns.run} className={`${CELL} font-mono font-bold whitespace-nowrap`}>
                #{run.run_no}
              </td>
              <td data-label={columns.page} className={CELL}>
                <span className="flex flex-wrap items-center gap-2">
                  {kindOf(run)}
                  {!run.submitted ? (
                    <span className="inline-flex items-center gap-1 rounded-r1 border border-warn px-2 text-small leading-6 font-bold">
                      <WarnIcon size={12} />
                      {t`not submitted`}
                    </span>
                  ) : null}
                </span>
              </td>
              <td data-label={columns.printed} className={`${CELL} tabular-nums`}>
                <time dateTime={run.created_at}>{time(run.created_at)}</time>
              </td>
              <td data-label={columns.requested} className={CELL}>{labelOf(run.requested_tray)}</td>
              <td data-label={columns.from} className={CELL}>
                <PrintedFrom run={run} labelOf={labelOf} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PrintedFrom({ run, labelOf }: { run: CalibrationRun; labelOf: (tray: string) => string }) {
  const { t } = useLingui();
  if (run.actual_media_source !== null) {
    return (
      <span className="inline-flex w-fit items-center gap-1.5 rounded-full bg-accent-soft px-2.5 leading-7 font-bold">
        <WarnIcon size={14} />
        {labelOf(run.actual_media_source)}
      </span>
    );
  }
  if (!run.media_source_reported) return <span className="text-ink-2">{t`not reported by the printer`}</span>;
  return <>{labelOf(run.requested_tray)}</>;
}
