// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import { useQueries } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Button } from "react-aria-components";
import { usePrinterStatus } from "../printers/api";
import { SelectField } from "../settings/controls";
import { SheetFrontDiagram, SheetFrontLegend } from "../sheets/GeometryDiagram";
import { PANEL, PRIMARY_BUTTON } from "../ui";
import { idPlacementQuery, useIdPlacement, usePatchType, type IdPlacementRow, type SheetTypeDetail, type SheetTypePatch } from "./api";
import { useArchiveErrorText } from "./errors";
import { IdPositionControls, type PositionDraft } from "./IdPositionControls";
import { allZones, formatTenth } from "./preprint";
import { parseDecimal } from "./search";
import { rectOf } from "./SheetFrontPanel";

type Band = NonNullable<IdPlacementRow["band"]>;
type Content = NonNullable<IdPlacementRow["content"]>;
type Reason = NonNullable<IdPlacementRow["reason"]>;

function useWords() {
  const { t } = useLingui();
  return {
    band: (b: Band): string =>
      ({ top: t`Top band`, bottom: t`Bottom band`, left: t`Left band`, right: t`Right band` })[b],
    content: (c: Content): string =>
      ({ qr_code_type: t`QR + code + type text`, qr_code: t`QR + code`, code: t`Code and type text` })[c],
    reason: (r: Reason): string =>
      ({
        too_small: t`The band left on this printer is too shallow or too short for the code.`,
        overlaps_preprint: t`The preprinted areas leave no room for the code on this printer.`,
        outside_printable: t`The position lies outside what this printer can print.`,
        no_margin_band: t`The sheet has no margin on that side.`,
      })[r],
    contentNote: (c: Content): string | null =>
      c === "qr_code"
        ? t`The type text does not fit beside the QR here.`
        : c === "code"
          ? t`No room for the QR here: the code is printed as text.`
          : null,
  };
}

/** "Top band, 54.0 mm from the right": the distance from the end the type's position starts from. */
function usePlaceText(type: SheetTypeDetail): (row: IdPlacementRow) => string | null {
  const { t, i18n } = useLingui();
  const words = useWords();
  const g = type.geometry;
  return (row) => {
    if (!row.fits || row.band === null || row.offset_mm === null) return null;
    const horizontal = row.band === "top" || row.band === "bottom";
    const grid = horizontal ? (g.cols - 1) * g.pitch_x + g.label_w : (g.rows - 1) * g.pitch_y + g.label_h;
    const along = (horizontal ? row.w_mm : row.h_mm) ?? 0;
    const fromEnd = type.id_margin_align === "end";
    const mm = formatTenth(Math.max(0, fromEnd ? grid - row.offset_mm - along : row.offset_mm), i18n.locale);
    const band = words.band(row.band);
    if (horizontal) return fromEnd ? t`${band}, ${mm} mm from the right` : t`${band}, ${mm} mm from the left`;
    return fromEnd ? t`${band}, ${mm} mm from the bottom` : t`${band}, ${mm} mm from the top`;
  };
}

/** The tray picked per printer on the type page; a printer without an entry is shown on its default tray. */
export type TrayChoice = Record<string, string>;

/**
 * The per-printer rows, each on its picked tray (#246, "Pick another tray on a row to check it"). The endpoint takes one
 * `tray` for every printer, so each picked tray is one more request; its row for that printer replaces the default one.
 */
export function usePlacementRows(key: string, trays: TrayChoice) {
  const base = useIdPlacement(key);
  const picked = [...new Set(Object.values(trays))];
  const extra = useQueries({ queries: picked.map((tray) => idPlacementQuery(key, tray)) });
  const byTray = new Map(picked.map((tray, i) => [tray, extra[i]!]));
  const rows = (base.data ?? []).map((row) => {
    const tray = trays[row.printer_id];
    if (tray === undefined) return { row, requested: null, checking: false, failed: false };
    const answer = byTray.get(tray);
    const fresh = answer?.data?.find((r) => r.printer_id === row.printer_id);
    return { row: fresh ?? row, requested: tray, checking: !!answer?.isPending, failed: !!answer?.isError };
  });
  return { base, rows };
}

/** "Position in the margin" of the type page (mockup M2): the same controls as Edit details, saved on their own. */
export function MarginPosition({
  type,
  admin,
  value,
  onChange: setValue,
}: {
  type: SheetTypeDetail;
  admin: boolean;
  /** The draft lives in the section, so switching to another choice and back keeps it (re-review 1 of #441). */
  value: PositionDraft;
  onChange: (next: PositionDraft) => void;
}) {
  const { t } = useLingui();
  const errorText = useArchiveErrorText();
  const patch = usePatchType();
  const [saved, setSaved] = useState(false);
  const offset = parseDecimal(value.offset);
  const offsetOk = offset !== null && offset >= 0 && offset <= 1000;
  const body: SheetTypePatch = {};
  if (value.band !== type.id_margin_band) body.id_margin_band = value.band;
  if (value.align !== type.id_margin_align) body.id_margin_align = value.align;
  if (offsetOk && Math.round(offset * 100) !== Math.round(type.id_margin_offset_mm * 100)) body.id_margin_offset_mm = Math.round(offset * 100) / 100;
  const dirty = Object.keys(body).length > 0;
  const areas = (type.preprint_zones?.length ?? 0) + (type.preprint_preset?.zones.length ?? 0);
  const areaWord = plural(areas, { one: "# area", other: "# areas" });
  const presetWord = type.preprint_preset ? t`1 preset` : t`no preset`;

  return (
    <section aria-labelledby="position-h" className={`${PANEL} flex min-w-0 flex-col gap-4`}>
      <h3 id="position-h" className="text-section font-bold">{t`Position in the margin`}</h3>
      <IdPositionControls
        value={value}
        onChange={(next) => {
          setValue(next);
          setSaved(false);
          patch.reset();
        }}
        isDisabled={!admin}
        invalidOffset={!offsetOk}
      />
      <div className="flex flex-wrap items-center gap-3">
        {admin ? (
          <Button
            onPress={() => patch.mutate({ key: type.key, patch: body }, { onSuccess: () => setSaved(true) })}
            isDisabled={!dirty || !offsetOk || patch.isPending}
            className={PRIMARY_BUTTON}
          >
            {t`Save position`}
          </Button>
        ) : null}
        <span className="text-small text-ink-2">{t`Edit details also holds the preprinted areas (${areaWord}, ${presetWord}).`}</span>
        <span role="status" className="text-small text-ink-2">
          {saved && !dirty ? t`Saved.` : null}
        </span>
      </div>
      {!offsetOk ? <p className="m-0 text-small font-bold text-bad">{t`The offset is a number of mm from 0 to 1000, for example 12.5.`}</p> : null}
      {patch.isError ? <p role="alert" className="m-0 font-bold text-bad">{errorText(patch.error)}</p> : null}
    </section>
  );
}

function Badge({ tone, children }: { tone: "ok" | "bad" | "info" | "plain"; children: ReactNode }) {
  const cls = {
    ok: "border-ok text-ink",
    bad: "border-bad bg-bad/10 text-ink",
    info: "border-transparent bg-info-soft text-ink",
    plain: "border-line-strong text-ink-2",
  }[tone];
  return <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-small font-bold whitespace-nowrap ${cls}`}>{children}</span>;
}

function PrinterRow({
  row,
  place,
  requested,
  checking,
  failed,
  onTray,
}: {
  row: IdPlacementRow;
  place: string | null;
  requested: string | null;
  checking: boolean;
  failed: boolean;
  onTray: (tray: string) => void;
}) {
  const { t } = useLingui();
  const words = useWords();
  const status = usePrinterStatus(row.printer_id);
  const trays = (status.data?.trays ?? []).filter((tray) => tray.enabled);
  const label = (key: string) => trays.find((tray) => tray.key === key)?.label ?? key;
  const note = row.fits ? (row.content ? words.contentNote(row.content) : null) : row.reason ? words.reason(row.reason) : null;
  return (
    <li className="flex flex-col gap-2 border-line px-4 py-3 not-first:border-t">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-bold [overflow-wrap:anywhere]">
          {row.printer_name} <span className="font-normal text-ink-2">· {label(row.tray)}</span>
        </span>
        <span className="flex flex-wrap gap-2">
          {row.fits ? (
            <Badge tone="ok">
              <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" className="fill-none stroke-ok stroke-2">
                <path d="m3 8 3.5 3.5L13 5" />
              </svg>
              {t`Fits`}
            </Badge>
          ) : (
            <Badge tone="bad">
              <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" className="fill-none stroke-bad stroke-2">
                <path d="m4 4 8 8M12 4l-8 8" />
              </svg>
              {t`Does not fit`}
            </Badge>
          )}
          {row.fits && row.content ? (
            <Badge tone={row.content === "qr_code_type" ? "ok" : "info"}>{words.content(row.content)}</Badge>
          ) : (
            <Badge tone="plain">{t`Not printed`}</Badge>
          )}
        </span>
      </div>
      <p className="m-0 text-small">{row.fits ? place : t`Not printed in the margin`}</p>
      {note ? (
        <p className="m-0 text-small text-ink-2">
          {note}
          {!row.fits && row.reason ? (
            <>
              {" "}
              <span className="font-mono text-ink-3">{row.reason}</span>
            </>
          ) : null}
        </p>
      ) : null}
      {status.isError ? (
        <p className="m-0 text-small text-ink-2">{t`Tray check needs the printer status, which could not be read.`}</p>
      ) : null}
      {trays.length > 1 ? (
        <div className="flex max-w-xs flex-col gap-1">
          <SelectField
            label={t`Check on tray`}
            value={requested ?? row.tray}
            options={trays.map((tray) => ({ id: tray.key, label: tray.label }))}
            onChange={onTray}
          />
          <span role="status" className="text-small text-ink-2">
            {checking
              ? t`Checking this tray…`
              : failed
                ? t`This tray could not be checked.`
                : requested !== null && requested !== row.tray
                  ? t`This printer cannot use that tray now: shown on its default tray.`
                  : null}
          </span>
        </div>
      ) : null}
    </li>
  );
}

/** "On your printers" (mockup M2, spec 5.3): per enabled page printer on its tray, fits or why not. */
export function OnYourPrinters({ type, trays, onTray }: { type: SheetTypeDetail; trays: TrayChoice; onTray: (printerId: string, tray: string) => void }) {
  const { t } = useLingui();
  const { base, rows } = usePlacementRows(type.key, trays);
  const placeText = usePlaceText(type);
  const missing = rows.filter((r) => !r.row.fits).length;
  const total = rows.length;
  return (
    <section aria-labelledby="printers-h" className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h3 id="printers-h" className="text-section font-bold">{t`On your printers`}</h3>
        <p className="m-0 text-ink-2">{t`One row per enabled page printer, with the tray its sheets use. Pick another tray on a row to check it.`}</p>
      </div>
      {base.isPending ? <p className="m-0 text-small text-ink-2">{t`Checking the printers…`}</p> : null}
      {base.isError ? <p className="m-0 text-small text-ink-2">{t`The printers could not be checked.`}</p> : null}
      {base.isSuccess && total === 0 ? <p className="m-0 text-small text-ink-2">{t`No enabled page printer.`}</p> : null}
      {total > 0 ? (
        <ul aria-label={t`Printers`} className="m-0 flex list-none flex-col overflow-hidden rounded-r3 border border-line bg-surface p-0">
          {rows.map(({ row, requested, checking, failed }) => (
            <PrinterRow
              key={row.printer_id}
              row={row}
              place={placeText(row)}
              requested={requested}
              checking={checking}
              failed={failed}
              onTray={(tray) => onTray(row.printer_id, tray)}
            />
          ))}
        </ul>
      ) : null}
      {missing > 0 ? (
        <div role="note" className="flex items-start gap-3 rounded-r3 border border-warn bg-accent-soft p-4">
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" className="mt-0.5 shrink-0 fill-none stroke-warn stroke-2 [stroke-linejoin:round]">
            <path d="M12 3 2 20h20L12 3ZM12 10v4M12 17v.5" />
          </svg>
          <div className="flex min-w-0 flex-col gap-1">
            <p className="m-0 font-bold">{t`${missing} of ${total} printers do not print the code in the margin`}</p>
            <p className="m-0 text-small">
              {t`On those printers a new sheet gets no sheet code in the margin: the print preview says so, and you add it by hand. Change the band, the offset or the preprinted areas if you want it there.`}
            </p>
          </div>
        </div>
      ) : null}
    </section>
  );
}

/** "Chosen place" (mockup M2): the sheet front with the code where one printer puts it, and the numbers in words. */
export function ChosenPlace({ type, trays }: { type: SheetTypeDetail; trays: TrayChoice }) {
  const { t, i18n } = useLingui();
  const words = useWords();
  const list = usePlacementRows(type.key, trays).rows.map((r) => r.row);
  const [chosen, setChosen] = useState<string | null>(null);
  const row = list.find((r) => r.printer_id === chosen) ?? list[0];
  const rect = rectOf(row);
  const g = type.geometry;
  const zones = allZones(type.preprint_zones ?? [], type.preprint_preset?.zones);
  const n = (v: number) => formatTenth(v, i18n.locale);
  let place: string | null = null;
  if (rect && row?.band) {
    const band = words.band(row.band);
    const along = n(row.band === "top" || row.band === "bottom" ? rect.x_mm : rect.y_mm);
    place = row.band === "top" || row.band === "bottom" ? t`${band}, ${along} mm from the left` : t`${band}, ${along} mm from the top`;
  }
  const zoom: [number, number, number, number] | undefined = rect
    ? [Math.max(0, rect.x_mm - 20), Math.max(0, rect.y_mm - 6), rect.w_mm + 40, rect.h_mm + 12]
    : undefined;
  return (
    <aside aria-labelledby="chosen-h" className={`${PANEL} flex min-w-0 flex-col gap-4 lg:sticky lg:top-20`}>
      <h3 id="chosen-h" className="text-section font-bold">{t`Chosen place`}</h3>
      {list.length > 1 ? (
        <SelectField
          label={t`Printer`}
          value={row ? row.printer_id : ""}
          options={list.map((r) => ({ id: r.printer_id, label: r.printer_name }))}
          onChange={setChosen}
        />
      ) : row ? (
        <p className="m-0 text-small">
          <span className="text-ink-2">{t`Printer`}</span> <span className="font-bold">{row.printer_name}</span>
        </p>
      ) : null}
      <div className="mx-auto w-full max-w-72">
        <SheetFrontDiagram geometry={g} zones={zones} idRect={rect} label={t`Sheet front with the sheet code where this printer puts it`} />
      </div>
      {zoom ? <SheetFrontDiagram geometry={g} zones={zones} idRect={rect} view={zoom} label={t`The sheet code, enlarged`} /> : null}
      {rect && row ? (
        <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-small">
          <dt className="text-ink-2">{t`Place`}</dt>
          <dd className="m-0 font-bold">{place}</dd>
          <dt className="text-ink-2">{t`Size`}</dt>
          <dd className="m-0 font-bold tabular-nums">{t`${n(rect.w_mm)} × ${n(rect.h_mm)} mm`}</dd>
          <dt className="text-ink-2">{t`Content`}</dt>
          <dd className="m-0 font-bold">{row.content ? words.content(row.content) : null}</dd>
        </dl>
      ) : row ? (
        <p className="m-0 text-small text-ink-2">{t`This printer does not print the code in the margin.`}</p>
      ) : null}
      <SheetFrontLegend />
    </aside>
  );
}
