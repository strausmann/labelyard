// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import { useEffect, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { PRIMARY_BUTTON } from "../../shell/SignIn";
import { PANEL, SECONDARY_BUTTON } from "../ui";
import { SelectField, SwitchField, TextInput } from "../settings/controls";
import { SaveFeedback } from "../settings/feedback";
import { invalidGeometry } from "../../api/errorDetail";
import { SheetFrontPanel } from "./SheetFrontPanel";
import { useAddVersion, useCreateType, usePatchType, usePreprintPresets, type SheetTypeCreate, type SheetTypeDetail, type SheetTypePatch } from "./api";
import { IdPositionControls, positionOf, type PositionDraft } from "./IdPositionControls";
import { allZones, coveredCells, rowCol, zoneRefusal, type Zone } from "./preprint";
import { PreprintZonesEditor, type ZonesDraft } from "./PreprintZonesEditor";
import { useArchiveErrorText } from "./errors";
import { TextAreaField } from "./fields";
import { draftOf, EMPTY_DRAFT, geometryOf, sameGeometry, type FieldName, type GeometryDraft } from "./geometryDraft";
import { PREFIX, KEY } from "./patterns";
import { httpUrl, parseDecimal } from "./search";

// The server's field names for the measurement inputs, to mark the one it complained about.
const SERVER_FIELD: Record<string, FieldName> = {
  cols: "cols",
  rows: "rows",
  label_w: "labelW",
  label_h: "labelH",
  pitch_x: "pitchX",
  pitch_y: "pitchY",
  margin_top: "marginTop",
  margin_left: "marginLeft",
  corner_radius: "cornerRadius",
};
// The server's caps (spec 15), checked here so a person is told before the request.
/** An http(s) address that carries a user name or password: `httpUrl` refuses it, and the person is told why. */
function hasUserInfo(value: string): boolean {
  try {
    const url = new URL(value);
    const isHttp = url.protocol === "http:" || url.protocol === "https:";
    return isHttp && Boolean(url.username || url.password);
  } catch {
    return false;
  }
}
const MAX = { vendor: 200, description: 200, sourceName: 200, sourceUrl: 500, notes: 2000, changeNote: 500, codes: 50, code: 40 };

export type FormMode = { kind: "create" } | { kind: "details"; type: SheetTypeDetail } | { kind: "geometry"; type: SheetTypeDetail };

/** Same zones in the same order (the note counts: it is stored). */
function sameZones(a: readonly Zone[], b: readonly Zone[]): boolean {
  const key = (z: Zone) => [z.x_mm, z.y_mm, z.w_mm, z.h_mm, z.note ?? ""].join("|");
  return a.length === b.length && a.every((z, i) => key(z) === key(b[i]!));
}

const nullable = (value: string): string | null => (value.trim() === "" ? null : value.trim());
const splitCodes = (value: string): string[] => value.split(/[,\n]/).map((c) => c.trim()).filter((c) => c !== "");

function GeometryFields({
  draft,
  onChange,
  invalid,
}: {
  draft: GeometryDraft;
  onChange: (next: GeometryDraft) => void;
  invalid: FieldName | null;
}) {
  const { t } = useLingui();
  const set = (patch: Partial<GeometryDraft>) => onChange({ ...draft, ...patch });
  const num = (label: string, name: FieldName, key: keyof GeometryDraft, description?: string) => (
    <TextInput label={label} value={draft[key]} onChange={(v) => set({ [key]: v })} inputMode="decimal" invalid={invalid === name} description={description} />
  );
  const round = draft.shape === "round";
  return (
    <fieldset className="flex flex-col gap-5">
      <legend className="mb-3 text-section font-bold">{t`Measurements`}</legend>
      <div className="grid gap-5 sm:grid-cols-2">
        <SelectField
          label={t`Paper size`}
          value={draft.pageSize}
          options={[
            { id: "A4", label: "A4" },
            { id: "Letter", label: "Letter" },
          ]}
          onChange={(id) => set({ pageSize: id === "Letter" ? "Letter" : "A4" })}
        />
        <SelectField
          label={t`Label shape`}
          value={draft.shape}
          options={[
            { id: "rectangle", label: t`Rectangle` },
            { id: "round", label: t`Round` },
          ]}
          onChange={(id) => set({ shape: id === "round" ? "round" : "rectangle" })}
        />
        {num(t`Columns`, "cols", "cols")}
        {num(t`Rows`, "rows", "rows")}
        {num(round ? t`Diameter (mm)` : t`Label width (mm)`, "labelW", "labelW")}
        {round ? null : num(t`Label height (mm)`, "labelH", "labelH")}
        {num(t`Horizontal pitch (mm)`, "pitchX", "pitchX", t`From the left edge of one label to the left edge of the next.`)}
        {num(t`Vertical pitch (mm)`, "pitchY", "pitchY", t`From the top edge of one label to the top edge of the next.`)}
        {num(t`Top margin (mm)`, "marginTop", "marginTop")}
        {num(t`Left margin (mm)`, "marginLeft", "marginLeft")}
        {round ? null : num(t`Corner radius (mm)`, "cornerRadius", "cornerRadius", t`Leave empty if unknown.`)}
      </div>
    </fieldset>
  );
}

/** The words for the field a geometry draft got wrong. */
function useFieldMessage(draft: GeometryDraft): (field: FieldName) => string {
  const { t } = useLingui();
  return (field) => {
    const round = draft.shape === "round";
    const labels: Record<FieldName, string> = {
      cols: t`Columns`,
      rows: t`Rows`,
      labelW: round ? t`Diameter (mm)` : t`Label width (mm)`,
      labelH: t`Label height (mm)`,
      pitchX: t`Horizontal pitch (mm)`,
      pitchY: t`Vertical pitch (mm)`,
      marginTop: t`Top margin (mm)`,
      marginLeft: t`Left margin (mm)`,
      cornerRadius: t`Corner radius (mm)`,
    };
    const label = labels[field];
    if (field === "cols" || field === "rows") return t`${label} must be a whole number from 1 to 400.`;
    if (field === "marginTop" || field === "marginLeft" || field === "cornerRadius") return t`${label} must be a number from 0 to 1000.`;
    return t`${label} must be a number above 0 and up to 1000.`;
  };
}

/**
 * Create a type, change its details, or measure it again (a new version). Three forms in one file
 * because they share the measurement fields and the way they report what is wrong.
 */
export function TypeForm({ mode, onDone }: { mode: FormMode; onDone: (createdKey?: string) => void }) {
  const { t, i18n } = useLingui();
  const errorText = useArchiveErrorText();
  const create = useCreateType();
  const patchType = usePatchType();
  const addVersion = useAddVersion();
  const type = mode.kind === "create" ? null : mode.type;

  const [key, setKey] = useState("");
  const [prefix, setPrefix] = useState(type?.prefix ?? "");
  const [vendor, setVendor] = useState(type?.vendor ?? "");
  const [description, setDescription] = useState(type?.description ?? "");
  const [codes, setCodes] = useState((type?.product_codes ?? []).join(", "));
  const [sourceName, setSourceName] = useState(type?.source_name ?? "");
  const [sourceUrl, setSourceUrl] = useState(type?.source_url ?? "");
  const [notes, setNotes] = useState(type?.notes ?? "");
  const [verified, setVerified] = useState(type?.verified ?? false);
  const [allowQr, setAllowQr] = useState(type?.allow_qr ?? true);
  const [draft, setDraft] = useState<GeometryDraft>(type ? draftOf(type.geometry) : EMPTY_DRAFT);
  const [changeNote, setChangeNote] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  // Edit details also holds the sheet code position and the preprinted areas (#246, mockup M1).
  const [position, setPosition] = useState<PositionDraft>(() =>
    type ? positionOf(type, i18n.locale) : { band: "auto", align: "start", offset: "0" },
  );
  const [zonesDraft, setZonesDraft] = useState<ZonesDraft>({ preset: type?.preprint_preset?.key ?? null, zones: type?.preprint_zones ?? [] });
  const presets = usePreprintPresets(mode.kind === "details");
  const [badField, setBadField] = useState<FieldName | null>(null);
  const fieldMessage = useFieldMessage(draft);
  // Sheets already made on the current version keep that version when a new one is saved.
  const pinned = type?.versions.find((v) => v.version === type.current_version)?.sheets ?? 0;
  const version = type?.current_version ?? 0;
  const keepWarning = pinned > 0 ? plural(pinned, { one: `# sheet keeps version ${version}`, other: `# sheets keep version ${version}` }) : null;

  const pending = create.isPending || patchType.isPending || addVersion.isPending;
  const serverError = create.error ?? patchType.error ?? addVersion.error;
  const serverField = SERVER_FIELD[invalidGeometry(serverError)?.field ?? ""] ?? null;

  const tooLong = (label: string, value: string, max: number): string | null =>
    value.length > max ? t`${label} is too long: at most ${max} characters.` : null;

  /** Checks the descriptive fields; the first problem is returned as a sentence. */
  function checkDetails(): string | null {
    const list = splitCodes(codes);
    if (list.length > MAX.codes) return t`At most ${MAX.codes} product codes.`;
    if (list.some((c) => c.length > MAX.code)) return t`A product code is at most ${MAX.code} characters.`;
    if (sourceUrl.trim() !== "" && !httpUrl(sourceUrl.trim())) {
      return hasUserInfo(sourceUrl.trim())
        ? t`The source address must not contain a user name or a password.`
        : t`The source address must start with http:// or https://.`;
    }
    return (
      tooLong(t`Vendor`, vendor, MAX.vendor) ??
      tooLong(t`Description`, description, MAX.description) ??
      tooLong(t`Source name`, sourceName, MAX.sourceName) ??
      tooLong(t`Source address`, sourceUrl, MAX.sourceUrl) ??
      tooLong(t`Notes`, notes, MAX.notes)
    );
  }

  function submit() {
    setProblem(null);
    setBadField(null);

    if (mode.kind === "details") {
      const message = checkDetails();
      if (message) return setProblem(message);
      const patch: SheetTypePatch = {};
      const before = mode.type;
      if (nullable(vendor) !== before.vendor) patch.vendor = nullable(vendor);
      if (nullable(description) !== before.description) patch.description = nullable(description);
      if (nullable(sourceName) !== before.source_name) patch.source_name = nullable(sourceName);
      if (nullable(sourceUrl) !== before.source_url) patch.source_url = nullable(sourceUrl);
      if (nullable(notes) !== before.notes) patch.notes = nullable(notes);
      if (splitCodes(codes).join("\n") !== before.product_codes.join("\n")) patch.product_codes = splitCodes(codes);
      if (verified !== before.verified) patch.verified = verified;
      if (allowQr !== before.allow_qr) patch.allow_qr = allowQr;
      if (prefix !== before.prefix) {
        if (!PREFIX.test(prefix)) return setProblem(t`The code prefix is 2 to 10 capital letters or digits and starts with a letter.`);
        patch.prefix = prefix;
      }
      if (position.band !== before.id_margin_band) patch.id_margin_band = position.band;
      if (position.align !== before.id_margin_align) patch.id_margin_align = position.align;
      const offset = parseDecimal(position.offset);
      if (offset === null || offset < 0 || offset > 1000) return setProblem(t`The offset is a number of mm from 0 to 1000, for example 12.5.`);
      if (Math.round(offset * 100) !== Math.round(before.id_margin_offset_mm * 100)) patch.id_margin_offset_mm = Math.round(offset * 100) / 100;
      if (!sameZones(zonesDraft.zones, before.preprint_zones ?? [])) patch.preprint_zones = zonesDraft.zones;
      if (zonesDraft.preset !== (before.preprint_preset?.key ?? null)) patch.preprint_preset = zonesDraft.preset;
      if (Object.keys(patch).length === 0) return setProblem(t`Nothing changed.`);
      patchType.mutate({ key: before.key, patch }, { onSuccess: () => onDone() });
      return;
    }

    const parsed = geometryOf(draft, type?.geometry);
    if (mode.kind === "geometry") {
      if (changeNote.trim() === "") return setProblem(t`Describe what changed.`);
      if (changeNote.length > MAX.changeNote) return setProblem(t`The note is too long: at most ${MAX.changeNote} characters.`);
      if ("field" in parsed) {
        setBadField(parsed.field);
        return setProblem(fieldMessage(parsed.field));
      }
      addVersion.mutate({ key: mode.type.key, version: { geometry: parsed.geometry, change_note: changeNote.trim() } }, { onSuccess: () => onDone() });
      return;
    }

    // create
    if (!KEY.test(key)) {
      return setProblem(t`The key uses lowercase letters, digits and dashes, 3 to 64 characters, and starts and ends with a letter or digit.`);
    }
    if (!PREFIX.test(prefix)) return setProblem(t`The code prefix is 2 to 10 capital letters or digits and starts with a letter.`);
    const message = checkDetails();
    if (message) return setProblem(message);
    if ("field" in parsed) {
      setBadField(parsed.field);
      return setProblem(fieldMessage(parsed.field));
    }
    const body: SheetTypeCreate = {
      key,
      prefix,
      product_codes: splitCodes(codes),
      verified,
      geometry: parsed.geometry,
      ...(nullable(vendor) !== null && { vendor: nullable(vendor) }),
      ...(nullable(description) !== null && { description: nullable(description) }),
      ...(nullable(sourceName) !== null && { source_name: nullable(sourceName) }),
      ...(nullable(sourceUrl) !== null && { source_url: nullable(sourceUrl) }),
      ...(nullable(notes) !== null && { notes: nullable(notes) }),
    };
    create.mutate(body, { onSuccess: () => onDone(key) });
  }

  const heading =
    mode.kind === "create" ? t`New sheet type` : mode.kind === "details" ? t`Edit details of ${mode.type.key}` : t`Measure ${mode.type.key} again`;
  const submitLabel = mode.kind === "create" ? t`Create type` : mode.kind === "details" ? t`Save details` : t`Save new version`;
  const descriptive = mode.kind !== "geometry";
  // A new version with the measurements of the current one is refused by the server (`unchanged`): say so before the press (267-03).
  const measured = mode.kind === "geometry" ? geometryOf(draft, mode.type.geometry) : null;
  const noChange = mode.kind === "geometry" && measured !== null && "geometry" in measured && sameGeometry(measured.geometry, mode.type.geometry);

  const refusal = mode.kind === "details" ? zoneRefusal(patchType.error) : null;
  const linkedPreset = type?.preprint_preset ?? null;
  const presetList = presets.data ?? [];
  const presetZones =
    zonesDraft.preset === null
      ? []
      : (presetList.find((p) => p.key === zonesDraft.preset)?.zones ?? (linkedPreset?.key === zonesDraft.preset ? linkedPreset.zones : []));
  const shownZones = allZones(zonesDraft.zones, presetZones);
  const refusedIndex = refusal ? (refusal.list === "preset" ? refusal.index : presetZones.length + refusal.index) : null;
  const changeZones = (next: ZonesDraft) => {
    setZonesDraft(next);
    patchType.reset();
  };

  const form = (
    <form
      aria-label={heading}
      noValidate
      className={`${PANEL} flex max-w-2xl min-w-0 flex-col gap-6`}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <h2 className="text-section font-bold">{heading}</h2>
      {mode.kind === "details" && refusal && refusedIndex !== null ? (
        <ZoneRefusalNote geometry={mode.type.geometry} zone={shownZones[refusedIndex]} number={refusedIndex + 1} code={refusal.code} />
      ) : null}

      {mode.kind === "create" ? (
        <div className="grid gap-5 sm:grid-cols-2">
          <TextInput label={t`Key`} value={key} onChange={setKey} mono description={t`The name in addresses and exports. It cannot be changed later.`} />
          <TextInput label={t`Code prefix`} value={prefix} onChange={(v) => setPrefix(v.toUpperCase())} mono description={t`Starts every sheet code, for example L4736.`} />
        </div>
      ) : null}

      {descriptive ? (
        <fieldset className="flex flex-col gap-5">
          <legend className="mb-3 text-section font-bold">{t`Details`}</legend>
          {mode.kind === "details" ? (
            <TextInput
              label={t`Code prefix`}
              value={prefix}
              onChange={(v) => setPrefix(v.toUpperCase())}
              mono
              readOnly={mode.type.sheets_in_use > 0}
              description={mode.type.sheets_in_use > 0 ? t`The prefix is fixed once sheets of this type exist.` : undefined}
            />
          ) : null}
          <TextInput label={t`Vendor`} value={vendor} onChange={setVendor} />
          <TextInput label={t`Description`} value={description} onChange={setDescription} />
          <TextInput label={t`Product codes`} value={codes} onChange={setCodes} mono description={t`Separate several with commas.`} />
          <div className="grid gap-5 sm:grid-cols-2">
            <TextInput label={t`Source name`} value={sourceName} onChange={setSourceName} />
            <TextInput label={t`Source address`} value={sourceUrl} onChange={setSourceUrl} type="url" mono inputMode="url" />
          </div>
          <TextAreaField label={t`Notes`} value={notes} onChange={setNotes} />
          <SwitchField label={t`Verified against a real sheet`} isSelected={verified} onChange={setVerified} />
          {mode.kind === "details" ? <SwitchField label={t`QR codes on labels`} isSelected={allowQr} onChange={setAllowQr} description={t`Off for labels too small to scan.`} /> : null}
        </fieldset>
      ) : null}

      {mode.kind === "details" ? (
        <>
          <fieldset className="flex flex-col gap-4">
            <legend className="mb-1 text-section font-bold">{t`Sheet code position`}</legend>
            <p className="-mt-2 text-ink-2">{t`Where the margin sheet code is printed. Checked against every page printer on the type page.`}</p>
            <IdPositionControls
              value={position}
              onChange={(next) => {
                setPosition(next);
                patchType.reset();
              }}
              invalidOffset={parseDecimal(position.offset) === null}
            />
          </fieldset>
          <fieldset className="flex flex-col gap-4">
            <legend className="mb-1 text-section font-bold">{t`Preprinted areas`}</legend>
            <p className="-mt-2 text-ink-2">
              {t`Where the vendor printed on the front of the sheet. The hub keeps the sheet code, its QR code and the feed mark out of these areas.`}
            </p>
            <PreprintZonesEditor
              geometry={mode.type.geometry}
              presets={presetList}
              linked={linkedPreset}
              value={zonesDraft}
              onChange={changeZones}
              refusal={refusal}
            />
          </fieldset>
        </>
      ) : null}

      {mode.kind !== "details" ? <GeometryFields draft={draft} onChange={setDraft} invalid={badField ?? serverField} /> : null}

      {mode.kind === "geometry" ? (
        <div className="flex flex-col gap-3">
          {keepWarning ? (
            <p className="rounded-r2 border border-warn p-3 text-ink">
              <strong>{keepWarning}</strong>. {t`Sheets already made keep printing with the old measurements.`}
            </p>
          ) : null}
          <TextAreaField label={t`What changed`} value={changeNote} onChange={setChangeNote} rows={2} description={t`For example: measured again with a ruler.`} />
        </div>
      ) : null}

      <div className="flex flex-wrap gap-3">
        <Button type="submit" isDisabled={pending || noChange} aria-describedby={noChange ? "type-form-no-change" : undefined} className={PRIMARY_BUTTON}>
          {submitLabel}
        </Button>
        {noChange ? (
          <p id="type-form-no-change" className="m-0 self-center text-small text-ink-2">
            {t`No change: the measurements are the same as in the current version.`}
          </p>
        ) : null}
        <Button onPress={() => onDone()} className={SECONDARY_BUTTON}>
          {t`Cancel`}
        </Button>
      </div>
      {mode.kind === "details" ? <p className="m-0 text-small text-ink-2">{t`Saving checks the areas against the labels and refuses a bad one.`}</p> : null}
      {/* A refused zone is reported at the top of the form, next to the list it marks. */}
      <SaveFeedback error={problem ?? (serverError && !refusal ? errorText(serverError) : null)} saved={false} />
    </form>
  );

  if (mode.kind !== "details") return form;
  return (
    <div className="grid min-w-0 items-start gap-6 xl:grid-cols-[minmax(0,42rem)_minmax(0,20rem)]">
      {form}
      <SheetFrontPanel geometry={mode.type.geometry} typeKey={mode.type.key} zones={shownZones} refused={refusedIndex === null ? [] : [refusedIndex]} />
    </div>
  );
}

/** "1 area was not saved": the zone the server refused, in words, above the form (mockup M1). */
function ZoneRefusalNote({ geometry, zone, number, code }: { geometry: SheetTypeDetail["geometry"]; zone: Zone | undefined; number: number; code: string }) {
  const { t } = useLingui();
  const cells = zone ? coveredCells(geometry, zone) : [];
  const first = cells[0];
  let text: string;
  if (code === "preprint_zone_off_page") {
    text = t`Area ${number} reaches outside the page. Move it onto the sheet or remove it.`;
  } else if (first !== undefined) {
    const { row, col } = rowCol(geometry, first);
    text = t`Area ${number} covers label ${first} (row ${row}, column ${col}). Preprint sits in the margins, so a label there would be printed on top of it. Move the area out of the labels or remove it.`;
  } else {
    text = t`Area ${number} covers a label. Preprint sits in the margins, so a label there would be printed on top of it. Move the area out of the labels or remove it.`;
  }
  // Save is at the foot of a long form: bring the note into sight when it appears.
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.scrollIntoView?.({ block: "nearest" });
  }, [number, code]);
  return (
    <div ref={ref} role="alert" className="flex scroll-mt-20 items-start gap-3 rounded-r3 border border-bad bg-bad/10 p-4">
      <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" className="mt-0.5 shrink-0 fill-none stroke-bad stroke-2 [stroke-linecap:round]">
        <path d="M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM9 9l6 6M15 9l-6 6" />
      </svg>
      <div className="flex min-w-0 flex-col gap-1">
        <p className="m-0 font-bold">{t`1 area was not saved`}</p>
        <p className="m-0 text-small">{text}</p>
      </div>
    </div>
  );
}
