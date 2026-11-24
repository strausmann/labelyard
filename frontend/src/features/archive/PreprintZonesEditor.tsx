// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { useState, type ReactNode } from "react";
import { Button } from "react-aria-components";
import { HINT, ModalDialog, SelectField, TextInput } from "../settings/controls";
import type { GeometryMm } from "../sheets/cellState";
import { PRIMARY_BUTTON, SECONDARY_BUTTON } from "../ui";
import type { PreprintPresetRead } from "./api";
import { coveredCells, formatTenth, MAX_ZONES, offPage, rowCol, type Zone, type ZoneRefusal } from "./preprint";
import { parseDecimal } from "./search";

/** What the editor changes: the linked preset (a key) and the type's own zones. */
export interface ZonesDraft {
  preset: string | null;
  zones: Zone[];
}

const NONE = "";
const FOCUS = "data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link";
const ICON_BUTTON = `press flex size-11 shrink-0 items-center justify-center rounded-r2 transition-colors duration-150 data-[hovered]:bg-sunken data-[pressed]:bg-sunken ${FOCUS}`;
/** The server's limit for a zone note (`ZONE_NOTE_MAX`). */
const NOTE_MAX = 120;

function Icon({ d, size = 20 }: { d: string; size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" className="shrink-0 fill-none stroke-current stroke-2 [stroke-linecap:round] [stroke-linejoin:round]">
      <path d={d} />
    </svg>
  );
}
const PENCIL = "M4 20h4L19 9l-4-4L4 16v4ZM14 6l4 4";
const TRASH = "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3";
const LOCK = "M7 11V8a5 5 0 0 1 10 0v3M5 11h14v10H5z";
const PLUS = "M12 5v14M5 12h14";
const CROSS = "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM9 9l6 6M15 9l-6 6";

/** The words for the cells a zone covers: "label 1 (row 1, column 1)", or several numbers. */
function useCoverText(geometry: GeometryMm): (cells: number[], short?: boolean) => string {
  const { t } = useLingui();
  return (cells, short = false) => {
    const first = cells[0]!;
    const { row, col } = rowCol(geometry, first);
    if (cells.length > 1) {
      const list = cells.slice(0, 4).join(", ");
      return cells.length > 4 ? t`labels ${list} and more` : t`labels ${list}`;
    }
    return short ? t`label ${first} (R${row} C${col})` : t`label ${first} (row ${row}, column ${col})`;
  };
}

/**
 * Preprinted areas of a sheet type (#246, mockup M1): the vendor preset, its zones (locked), the type's own zones
 * (edit, remove, add in a dialog) and the zone the server refused, marked in place.
 */
export function PreprintZonesEditor({
  geometry,
  presets,
  linked,
  value,
  onChange,
  refusal,
  isDisabled,
}: {
  geometry: GeometryMm;
  presets: readonly PreprintPresetRead[];
  /** The preset the type links now, for when the list is not loaded (or the key cannot list it). */
  linked: PreprintPresetRead | null;
  value: ZonesDraft;
  onChange: (next: ZonesDraft) => void;
  refusal: ZoneRefusal | null;
  isDisabled?: boolean;
}) {
  const { t, i18n } = useLingui();
  const cover = useCoverText(geometry);
  const n = (v: number) => formatTenth(v, i18n.locale);
  const [editing, setEditing] = useState<{ index: number | null } | null>(null);

  const sameSize = presets.filter((p) => p.page_size === geometry.page_size);
  const preset = value.preset === null ? null : (sameSize.find((p) => p.key === value.preset) ?? (linked?.key === value.preset ? linked : null));
  const presetZones = preset?.zones ?? [];
  const own = value.zones;
  const copyFits = presetZones.length + own.length <= MAX_ZONES;
  const options = [{ id: NONE, label: t`No preset` }, ...sameSize.map((p) => ({ id: p.key, label: p.name }))];
  if (preset && !sameSize.some((p) => p.key === preset.key)) options.push({ id: preset.key, label: preset.name });

  const name = (z: Zone, number: number) => (z.note && z.note.trim() !== "" ? z.note : t`Area ${number}`);
  const refusalText = (z: Zone): string => {
    if (offPage(geometry, z)) return t`Reaches outside the page.`;
    const cells = coveredCells(geometry, z);
    if (cells.length === 0) return t`Refused by the server.`;
    const where = cover(cells, true);
    return t`Covers ${where}.`;
  };

  type Row = { zone: Zone; number: number; kind: "preset" | "own"; ownIndex: number; refused: boolean };
  const rows: Row[] = [
    ...presetZones.map((zone, i) => ({ zone, number: i + 1, kind: "preset" as const, ownIndex: -1, refused: refusal?.list === "preset" && refusal.index === i })),
    ...own.map((zone, i) => ({ zone, number: presetZones.length + i + 1, kind: "own" as const, ownIndex: i, refused: refusal?.list === "own" && refusal.index === i })),
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
        <div className="flex min-w-0 flex-col gap-1">
          <SelectField
            label={t`Vendor preset`}
            value={value.preset ?? NONE}
            options={options}
            onChange={(id) => onChange({ ...value, preset: id === NONE ? null : id })}
            isDisabled={isDisabled}
          />
        </div>
        {preset && !isDisabled ? (
          <Button
            onPress={() => onChange({ preset: null, zones: [...presetZones, ...own] })}
            isDisabled={!copyFits}
            className={`${SECONDARY_BUTTON} sm:mb-0`}
          >
            {t`Unlink preset and keep a copy`}
          </Button>
        ) : null}
      </div>
      <p className={HINT}>
        {preset && !copyFits
          ? t`Areas of the preset and your own areas are all kept free. A copy would be more than ${MAX_ZONES} areas: remove some of your own first.`
          : t`Areas of the preset and your own areas are all kept free.`}
      </p>

      {rows.length > 0 ? (
        <ol aria-label={t`Preprinted areas`} className="m-0 flex list-none flex-col overflow-hidden rounded-r3 border border-line p-0">
          {rows.map((r) => {
            const label = name(r.zone, r.number);
            return (
              <li
                key={`${r.kind}-${r.number}`}
                className={`grid grid-cols-[1.5rem_minmax(0,1fr)_auto] gap-x-3 gap-y-1 border-line px-4 py-3 not-first:border-t ${r.refused ? "bg-bad/10" : ""}`}
              >
                <span className="pt-0.5 font-bold tabular-nums text-ink-2">{r.number}</span>
                <span className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="font-bold [overflow-wrap:anywhere]">{label}</span>
                  {r.kind === "preset" ? (
                    <span className="inline-flex items-center gap-1 rounded-full bg-info-soft px-2 py-0.5 text-small font-bold text-ink">
                      <Icon d={LOCK} size={14} />
                      {t`Preset`}
                    </span>
                  ) : null}
                </span>
                <span className="row-span-2 flex items-start gap-1">
                  {r.kind === "own" && !isDisabled ? (
                    <>
                      <Button aria-label={t`Edit ${label}`} onPress={() => setEditing({ index: r.ownIndex })} className={`${ICON_BUTTON} text-ink`}>
                        <Icon d={PENCIL} />
                      </Button>
                      <Button
                        aria-label={t`Remove ${label}`}
                        onPress={() => onChange({ ...value, zones: own.filter((_, i) => i !== r.ownIndex) })}
                        className={`${ICON_BUTTON} text-bad`}
                      >
                        <Icon d={TRASH} />
                      </Button>
                    </>
                  ) : null}
                </span>
                <span className="col-start-2 text-small text-ink-2 tabular-nums">
                  {t`x ${n(r.zone.x_mm)} · y ${n(r.zone.y_mm)} · width ${n(r.zone.w_mm)} · height ${n(r.zone.h_mm)} mm`}
                  {r.kind === "preset" ? ` · ${t`unlink the preset to change it`}` : null}
                </span>
                {r.refused && refusal ? (
                  <p className="col-start-2 m-0 flex items-start gap-1.5 text-small text-bad">
                    <Icon d={CROSS} size={16} />
                    <span>
                      {refusalText(r.zone)} <span className="font-mono">{refusal.code}</span>
                    </span>
                  </p>
                ) : null}
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="m-0 rounded-r2 bg-sunken p-3 text-small text-ink-2">{t`No preprinted areas. The whole margin is free for the sheet code.`}</p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="m-0 text-small text-ink-2">{t`${own.length} of ${MAX_ZONES} areas`}</p>
        {!isDisabled ? (
          <Button onPress={() => setEditing({ index: null })} isDisabled={own.length >= MAX_ZONES} className={`${SECONDARY_BUTTON} gap-2`}>
            <Icon d={PLUS} size={18} />
            {t`Add area`}
          </Button>
        ) : null}
      </div>

      {editing ? (
        <ZoneDialog
          geometry={geometry}
          title={editing.index === null ? t`New area ${presetZones.length + own.length + 1}` : t`Edit area ${presetZones.length + editing.index + 1}`}
          start={editing.index === null ? null : (own[editing.index] ?? null)}
          coverText={cover}
          onClose={() => setEditing(null)}
          onSave={(zone) => {
            const zones = editing.index === null ? [...own, zone] : own.map((z, i) => (i === editing.index ? zone : z));
            onChange({ ...value, zones });
            setEditing(null);
          }}
        />
      ) : null}
    </div>
  );
}

/** Text of a number in the dialog: the stored value as the reader writes it. */
const asText = (v: number | undefined, locale: string) => (v === undefined ? "" : formatTenth(v, locale));

function ZoneDialog({
  geometry,
  title,
  start,
  coverText,
  onClose,
  onSave,
}: {
  geometry: GeometryMm;
  title: string;
  start: Zone | null;
  coverText: (cells: number[], short?: boolean) => string;
  onClose: () => void;
  onSave: (zone: Zone) => void;
}) {
  const { t, i18n } = useLingui();
  const [note, setNote] = useState(start?.note ?? "");
  const [x, setX] = useState(asText(start?.x_mm, i18n.locale));
  const [y, setY] = useState(asText(start?.y_mm, i18n.locale));
  const [w, setW] = useState(asText(start?.w_mm, i18n.locale));
  const [h, setH] = useState(asText(start?.h_mm, i18n.locale));
  const [problem, setProblem] = useState<string | null>(null);

  const values = { x: parseDecimal(x), y: parseDecimal(y), w: parseDecimal(w), h: parseDecimal(h) };
  const complete = values.x !== null && values.y !== null && values.w !== null && values.h !== null;
  const zone: Zone | null = complete ? { x_mm: values.x!, y_mm: values.y!, w_mm: values.w!, h_mm: values.h! } : null;
  // Said before saving; the server has the last word when the details are saved.
  let warning: string | null = null;
  if (zone && zone.w_mm > 0 && zone.h_mm > 0) {
    const cells = coveredCells(geometry, zone);
    if (offPage(geometry, zone)) warning = t`It reaches outside the page. Keep it inside the sheet.`;
    else if (cells.length > 0) {
      const where = coverText(cells);
      const top = formatTenth(geometry.margin_top, i18n.locale);
      warning =
        cells.every((c) => rowCol(geometry, c).row === 1) && geometry.margin_top >= 1
          ? t`Covers ${where}. Move it above ${top} mm or shrink it so it stays out of the labels.`
          : t`Covers ${where}. Move it or shrink it so it stays out of the labels.`;
    }
  }

  function save() {
    const parsed = [values.x, values.y, values.w, values.h];
    if (parsed.some((v) => v === null || !Number.isFinite(v))) return setProblem(t`Enter each value as a number in mm, for example 12.5.`);
    if (values.x! < 0 || values.y! < 0) return setProblem(t`x and y start at the sheet's top-left corner: they cannot be negative.`);
    if (values.w! < 1 || values.h! < 1) return setProblem(t`An area is at least 1 mm wide and 1 mm high.`);
    if (note.length > NOTE_MAX) return setProblem(t`The note is too long: at most ${NOTE_MAX} characters.`);
    const trimmed = note.trim();
    onSave({ x_mm: values.x!, y_mm: values.y!, w_mm: values.w!, h_mm: values.h!, ...(trimmed !== "" ? { note: trimmed } : { note: null }) });
  }
  const field = (label: string, val: string, set: (v: string) => void): ReactNode => (
    <TextInput
      label={label}
      value={val}
      onChange={(v) => {
        set(v);
        setProblem(null);
      }}
      inputMode="decimal"
      invalid={problem !== null && parseDecimal(val) === null}
      validationBehavior="aria"
    />
  );

  return (
    <ModalDialog title={title} isOpen onClose={onClose} dismissable={false}>
      <form
        noValidate
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          save();
        }}
      >
        <p className="-mt-3 m-0 text-small text-ink-2">{t`From the sheet's top-left corner, in mm.`}</p>
        <TextInput label={t`Note`} value={note} onChange={setNote} maxLength={NOTE_MAX} />
        {/* Two columns on every width: with the keyboard open all four numbers stay in one screen (spec 12). */}
        <div className="grid grid-cols-2 gap-3">
          {field(t({ message: "x", context: "zone left edge" }), x, setX)}
          {field(t({ message: "y", context: "zone top edge" }), y, setY)}
          {field(t`Width`, w, setW)}
          {field(t`Height`, h, setH)}
        </div>
        {problem ? (
          <p role="alert" className="m-0 rounded-r2 border border-bad bg-bad/10 p-3 text-small font-bold text-ink">
            {problem}
          </p>
        ) : warning ? (
          <p role="status" className="m-0 flex items-start gap-2 rounded-r2 border border-bad bg-bad/10 p-3 text-small text-ink">
            <span className="text-bad">
              <Icon d={CROSS} size={18} />
            </span>
            <span>{warning}</span>
          </p>
        ) : null}
        {/* The footer stays in reach while the fields scroll inside the dialog (phone with the keyboard up). */}
        <div className="sticky -bottom-6 -mx-6 -mb-6 grid grid-cols-2 gap-3 border-t border-line bg-surface px-6 pt-3 pb-[calc(1.5rem+env(safe-area-inset-bottom,0px))] sm:flex sm:justify-end">
          <Button onPress={onClose} className={SECONDARY_BUTTON}>
            {t`Cancel`}
          </Button>
          <Button type="submit" className={PRIMARY_BUTTON}>
            {t`Save area`}
          </Button>
        </div>
      </form>
    </ModalDialog>
  );
}
