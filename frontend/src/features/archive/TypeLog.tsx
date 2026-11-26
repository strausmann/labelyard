// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import { useMemo, useState, type ReactNode } from "react";
import { Button } from "react-aria-components";
import { usePrinters } from "../printers/api";
import { HINT, SelectField } from "../settings/controls";
import { SECONDARY_BUTTON, SECTION_HEADING } from "../ui";
import { useRunTime } from "../calibration/RunsTable";
import { LOG_KEPT, useTypeLog, type LogFilters, type LogKind, type LogVerdict, type SheetTypeLogEntry } from "./api";
import { TypeLogEntryDialog, useVerdictNames } from "./TypeLogEntryDialog";

const SMALL_BUTTON =
  "press inline-flex min-h-11 items-center justify-center gap-2 rounded-r2 border border-line-strong bg-surface px-3 text-small font-bold text-ink transition-colors duration-150 hover:bg-sunken data-[disabled]:opacity-60 data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link";
const PILL = "inline-flex items-center gap-1 rounded-full px-2.5 text-small leading-6 font-bold whitespace-nowrap";

function Icon({ d, size = 14 }: { d: string; size?: number }) {
  return (
    <svg viewBox="0 0 16 16" width={size} height={size} aria-hidden="true" className="shrink-0 fill-none stroke-current stroke-[1.8] [stroke-linecap:round] [stroke-linejoin:round]">
      <path d={d} />
    </svg>
  );
}
const CHECK = "m3 8.5 3 3 7-7";
const WARN = "M8 2.5 14.5 13.5h-13L8 2.5ZM8 6.5v3M8 11.5h.01";
const INFO = "M8 14.5a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13ZM8 7.5v4M8 5h.01";
const PLUS = "M8 3v10M3 8h10";
/** The route other tools read the log from; not translated. */
const LOG_API = "GET /api/v1/sheet-types/{key}/log";

/** The verdict as a badge with a shape and a word, never colour alone (mockup M12). No verdict shows no badge. */
export function VerdictBadge({ verdict }: { verdict: LogVerdict }) {
  const names = useVerdictNames();
  if (verdict === "none") return null;
  const look: Record<Exclude<LogVerdict, "none">, { className: string; icon?: string }> = {
    fits: { className: "bg-info-soft text-ok", icon: CHECK },
    sheet_geometry: { className: "border border-warn bg-accent-soft text-ink", icon: WARN },
    feed_variance: { className: "bg-info-soft text-link" },
    printer: { className: "border border-line-strong text-ink" },
  };
  const { className, icon } = look[verdict];
  return (
    <span className={`${PILL} ${className}`}>
      {icon ? <Icon d={icon} /> : null}
      {names[verdict]}
    </span>
  );
}

function useKindTitle(): (entry: SheetTypeLogEntry) => string {
  const { t } = useLingui();
  return (entry) => {
    switch (entry.kind) {
      case "check_print":
        return t`Check print`;
      case "verified":
        return t`Marked as verified`;
      case "new_version":
        return entry.type_version !== null ? t`New version ${entry.type_version}` : t`New version`;
      case "catalog_update":
        return t`Catalog update`;
      default:
        return t`Note`;
    }
  };
}

function useKindNames(): Record<LogKind, string> {
  const { t } = useLingui();
  return { check_print: t`Check prints`, verified: t`Marked as verified`, new_version: t`New versions`, catalog_update: t`Catalog updates`, note: t`Notes` };
}

/** Printer · tray · print #n · pass k · version v: where an entry happened, as far as it is known. */
function useWhereText(): (entry: SheetTypeLogEntry) => string | null {
  const { t } = useLingui();
  return (entry) => {
    const parts: string[] = [];
    if (entry.printer) parts.push(entry.printer.name);
    if (entry.tray) parts.push(entry.tray_label ?? entry.tray);
    if (entry.run) {
      parts.push(t`print #${entry.run.run_no}`);
      if (entry.run.pass_no !== null) parts.push(t`pass ${entry.run.pass_no}`);
    }
    if (entry.kind === "check_print" && entry.type_version !== null) parts.push(t`version ${entry.type_version}`);
    return parts.length > 0 ? parts.join(" · ") : null;
  };
}

function Readings({ entry }: { entry: SheetTypeLogEntry }) {
  const { t, i18n } = useLingui();
  const readings = entry.readings ?? [];
  // Signed with a real minus, two decimals: the values are read against each other down the column.
  const mm = (n: number | null | undefined) =>
    typeof n === "number" ? `${n > 0 ? "+" : ""}${n.toLocaleString(i18n.locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).replace("-", "−")} mm` : "—";
  const head = "px-3 py-2 text-small font-bold";
  return (
    <details open className="flex min-w-0 flex-col gap-2">
      <summary className="inline-flex min-h-11 cursor-pointer items-center text-small font-bold">
        {plural(readings.length, { one: "Readings (# crossing)", other: "Readings (# crossings)" })}
      </summary>
      <div className="overflow-x-auto rounded-r2 border border-line">
        <table aria-label={t`Readings`} className="w-full border-collapse text-left">
          <thead>
            <tr className="border-b border-line bg-sunken">
              <th scope="col" className={head}>{t`Crossing`}</th>
              <th scope="col" className={`${head} text-right`}>{t`Across`}</th>
              <th scope="col" className={`${head} text-right`}>{t`Down`}</th>
              <th scope="col" className={`${head} text-right`}>{t`Residual`}</th>
            </tr>
          </thead>
          <tbody>
            {readings.map((r, i) => (
              <tr key={i} className="border-t border-line first:border-t-0">
                <td className="px-3 py-2 font-mono font-bold whitespace-nowrap">{`S${r.col_gap} · Z${r.row_gap}`}</td>
                <td className="px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap">{mm(r.across_mm)}</td>
                <td className="px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap">{mm(r.down_mm)}</td>
                <td className="px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap text-ink-2">{mm(r.residual_mm)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function Entry({ entry, onEdit }: { entry: SheetTypeLogEntry; onEdit?: () => void }) {
  const { t, i18n } = useLingui();
  const title = useKindTitle();
  const where = useWhereText();
  const time = useRunTime();
  const place = where(entry);
  const signed = (n: number | null) =>
    n === null ? "—" : `${n > 0 ? "+" : ""}${n.toLocaleString(i18n.locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).replace("-", "−")}`;
  const c = entry.correction;
  return (
    <li className="flex min-w-0 flex-col gap-1.5 border-b border-line px-4 py-3.5 last:border-b-0">
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
        <span className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="font-bold">{title(entry)}</span>
          <VerdictBadge verdict={entry.verdict} />
        </span>
        <span className="text-small text-ink-2 [overflow-wrap:anywhere]">
          <time dateTime={entry.created_at}>{time(entry.created_at)}</time> · {entry.actor ?? "—"}
        </span>
      </div>
      {place ? <p className="m-0 text-small text-ink-2 [overflow-wrap:anywhere]">{place}</p> : null}
      {entry.note ? <p className="m-0 whitespace-pre-wrap [overflow-wrap:anywhere]">{entry.note}</p> : null}
      {entry.readings && entry.readings.length > 0 ? <Readings entry={entry} /> : null}
      {c ? (
        <p className="m-0 text-small text-ink-2">
          {t`Correction in effect: history entry ${c.seq} (dx ${signed(c.offset_x_mm)}, dy ${signed(c.offset_y_mm)} mm)`}
        </p>
      ) : null}
      {onEdit ? (
        <div>
          <Button onPress={onEdit} className={SMALL_BUTTON}>
            {entry.verdict === "none" && !entry.note ? t`Add verdict and note` : t`Edit verdict and note`}
          </Button>
        </div>
      ) : null}
    </li>
  );
}

interface TypeLogProps {
  typeKey: string;
  /** How the type is named to the person (the dialog's subtitle). */
  typeName: string;
  admin: boolean;
  /**
   * On the calibration page the log is fixed to one printer and tray (design 7.6): no printer choice, and a new note
   * names them. On the type detail the person filters by printer.
   */
  fixed?: { printerId: string; printerName: string; tray: string; trayLabel: string };
  /** The heading's level and text, for where the log sits. */
  heading?: ReactNode;
}

/** The log of a sheet type (#351, design 7.6, mockup M12): checks, verdicts and changes, newest first. */
export function TypeLog({ typeKey, typeName, admin, fixed, heading }: TypeLogProps) {
  const { t } = useLingui();
  const kindNames = useKindNames();
  const printers = usePrinters();
  const pagePrinters = useMemo(() => (printers.data ?? []).filter((p) => p.backend === "ipp"), [printers.data]);
  const [printer, setPrinter] = useState("all");
  const [kind, setKind] = useState<"all" | LogKind>("all");
  const [dialog, setDialog] = useState<{ mode: "add" } | { mode: "edit"; entry: SheetTypeLogEntry } | null>(null);
  const filters: LogFilters = {
    ...(fixed ? { printer: fixed.printerId, tray: fixed.tray } : printer !== "all" ? { printer } : {}),
    ...(kind !== "all" ? { kind } : {}),
  };
  const log = useTypeLog(typeKey, filters);
  const entries = log.data?.pages.flat() ?? [];
  const more = log.hasNextPage;
  const headingId = `type-log-h-${fixed ? "cal" : "type"}`;

  return (
    <section aria-labelledby={headingId} className="flex min-w-0 flex-col gap-4">
      {fixed ? (
        <h2 id={headingId} className={`${SECTION_HEADING} [overflow-wrap:anywhere]`}>
          {heading ?? t`Log`}
        </h2>
      ) : (
        <h3 id={headingId} className={heading ? SECTION_HEADING : "sr-only"}>
          {heading ?? t`Log`}
        </h3>
      )}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className={`grid min-w-0 flex-1 gap-3 ${fixed ? "max-w-64" : "max-w-[32.5rem] grid-cols-2"}`}>
          {fixed ? null : (
            <SelectField
              label={t`Printer`}
              value={printer}
              options={[{ id: "all", label: t`All printers` }, ...pagePrinters.map((p) => ({ id: p.id, label: p.name }))]}
              onChange={setPrinter}
            />
          )}
          <SelectField
            label={t`Kind`}
            value={kind}
            options={[{ id: "all", label: t`All` }, ...(Object.keys(kindNames) as LogKind[]).map((k) => ({ id: k, label: kindNames[k] }))]}
            onChange={(id) => setKind(id as "all" | LogKind)}
          />
        </div>
        {admin ? (
          <Button onPress={() => setDialog({ mode: "add" })} className={`${SECONDARY_BUTTON} gap-2`}>
            <Icon d={PLUS} size={16} />
            {t`Add a note`}
          </Button>
        ) : null}
      </div>

      <div role="note" className="flex gap-3 rounded-r2 bg-info-soft p-3 text-small text-ink">
        <span className="mt-0.5 text-link">
          <Icon d={INFO} size={16} />
        </span>
        <p className="m-0 flex min-w-0 flex-col gap-0.5 [overflow-wrap:anywhere]">
          <strong>{t`Internal only`}</strong>
          <span>
            {t`This log names your printers and trays. It is never part of an export, of the catalog or of a pre-filled feedback issue. Other tools read it through the API: ${LOG_API}.`}
          </span>
        </p>
      </div>

      {log.isPending ? (
        <p role="status" className="text-ink-2">{t`Loading…`}</p>
      ) : log.isError ? (
        <p role="alert" className="text-bad">{t`The log could not be loaded.`}</p>
      ) : entries.length === 0 ? (
        <p className="text-ink-2">
          {fixed ? t`Nothing logged for this printer and tray yet.` : t`Nothing logged yet. Check prints, verdicts, new versions and catalog updates appear here.`}
        </p>
      ) : (
        <>
          <ul aria-label={t`Log`} className="m-0 flex list-none flex-col overflow-hidden rounded-r3 border border-line bg-surface p-0">
            {entries.map((entry) => (
              <Entry key={entry.id} entry={entry} onEdit={admin ? () => setDialog({ mode: "edit", entry }) : undefined} />
            ))}
          </ul>
          <div className="flex flex-wrap items-center gap-3">
            <p className={`${HINT} m-0`}>
              {more
                ? t`Showing the newest ${entries.length} entries. The newest ${LOG_KEPT} per type are kept.`
                : t`${entries.length} of ${entries.length} entries. The newest ${LOG_KEPT} per type are kept.`}
            </p>
            {more ? (
              <Button isDisabled={log.isFetchingNextPage} onPress={() => void log.fetchNextPage()} className={SECONDARY_BUTTON}>
                {t`Show older entries`}
              </Button>
            ) : null}
          </div>
        </>
      )}

      <TypeLogEntryDialog
        typeKey={typeKey}
        typeName={typeName}
        state={dialog}
        fixed={fixed}
        pagePrinters={pagePrinters.filter((p) => p.enabled)}
        onClose={() => setDialog(null)}
      />
    </section>
  );
}
