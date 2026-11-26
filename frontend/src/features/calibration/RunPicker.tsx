// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { useMemo } from "react";
import { HINT, LABEL, SelectField } from "../settings/controls";
import { useCalibrationRuns, useOpenCheckSheet, type CalibrationRun } from "./api";

/** A print the readings can belong to: a calibration page, or a pass of the open check sheet (design 7.4). */
export interface RunChoice {
  id: string;
  run_no: number;
  kind: CalibrationRun["kind"];
  /** The sheet type version the page was printed for: the fit of its readings must name it. */
  version: number;
  label: string;
  section: string;
  /** The run as the hub lists it; missing for a pass whose run is no longer listed. */
  run?: CalibrationRun;
}

/**
 * The prints of this printer, tray and type that readings can come from, newest calibration page first,
 * then the passes of the open check sheet. `fallbackVersion` is the version the open check sheet was
 * asked for (its passes were printed for it).
 */
export function useRunChoices(printerId: string, tray: string, type: { key: string; version: number }) {
  const { t, i18n } = useLingui();
  const runs = useCalibrationRuns(printerId, tray);
  const sheet = useOpenCheckSheet(printerId, tray, type);
  const choices = useMemo(() => {
    const when = (at: string) => {
      const date = new Date(at);
      return Number.isNaN(date.getTime()) ? at : date.toLocaleString(i18n.locale, { dateStyle: "medium", timeStyle: "short" });
    };
    const pages: RunChoice[] = (runs.data ?? [])
      .filter((r) => r.kind === "calibration_page" && r.type_key === type.key && r.version !== null)
      .map((r) => {
        const runNo = r.run_no;
        const no = r.retry ? t`#${runNo} (retry)` : `#${runNo}`;
        const time = when(r.created_at);
        return {
          id: r.id,
          run_no: r.run_no,
          kind: r.kind,
          version: r.version!,
          label: t`${no} · calibration page · ${time}`,
          section: t`Calibration pages`,
          run: r,
        };
      });
    // The passes of the open check sheet, or, once its last pass closed it, of the newest check sheet in the run
    // history: the readings of the last pass are entered after it printed (and closed the sheet).
    const open = sheet.data ?? null;
    const checkRuns = (runs.data ?? []).filter((r) => r.kind === "check_page" && r.type_key === type.key && r.check_sheet_id !== null && r.pass_no !== null);
    const sheetId = open?.check_sheet_id ?? checkRuns[0]?.check_sheet_id ?? null;
    const fromRuns = checkRuns.filter((r) => r.check_sheet_id === sheetId).map((r) => ({ pass_no: r.pass_no!, run_id: r.id, run_no: r.run_no }));
    const listed = [...(open?.passes ?? []), ...fromRuns.filter((p) => !open?.passes.some((o) => o.run_id === p.run_id))];
    const first = listed.find((p) => p.pass_no === 1)?.run_no ?? listed.reduce((min, p) => Math.min(min, p.run_no), Infinity);
    const section = open ? t`Open check sheet` : t`Last check sheet`;
    const passes: RunChoice[] = listed
      .slice()
      .sort((a, b) => a.pass_no - b.pass_no)
      .map((p) => {
        const r = runs.data?.find((x) => x.id === p.run_id);
        const pass = p.pass_no;
        const runNo = p.run_no;
        return {
          id: p.run_id,
          run_no: p.run_no,
          kind: "check_page" as const,
          version: r?.version ?? type.version,
          label: t`Check sheet of run #${first} — pass ${pass}, run #${runNo}`,
          section,
          run: r,
        };
      });
    return [...pages, ...passes];
  }, [runs.data, sheet.data, type.key, type.version, t, i18n.locale]);
  // Operator ruling (2026-11-26): the default is the newest run of this type and its current version, a check pass
  // included, not only the newest calibration page (design 7.4 names the page): check readings are entered right after
  // the check print. With no run of the current version, the newest of the type.
  const newest = (list: RunChoice[]) => list.reduce<RunChoice | null>((best, c) => (best === null || c.run_no > best.run_no ? c : best), null);
  const defaultChoice = newest(choices.filter((c) => c.version === type.version)) ?? newest(choices);
  return { choices, defaultChoice, isPending: runs.isPending, isError: runs.isError };
}

export interface RunPickerProps {
  choices: RunChoice[];
  value: RunChoice | null;
  onChange: (id: string) => void;
  isPending: boolean;
  /** The prints could not be loaded: the readings cannot be tied to one. */
  isError: boolean;
  /** The choice made when nobody chose; the hint calls it the newest. */
  defaultId: string | null;
  /** The label of a tray key, for the line about where the page came from. */
  labelOf: (key: string) => string;
}

/** "Readings from print #12": the readings and the fit belong to that print (design 7.4). */
export function RunPicker({ choices, value, onChange, isPending, isError, defaultId, labelOf }: RunPickerProps) {
  const { t } = useLingui();
  if (isError) {
    return (
      <div className="flex min-w-0 flex-col gap-1.5">
        <span className={LABEL}>{t`Readings from print`}</span>
        <p role="alert" className="m-0 text-small text-bad">
          {t`The prints of this tray could not be loaded, so the readings cannot be tied to one. Reload the page to try again.`}
        </p>
      </div>
    );
  }
  if (choices.length === 0 || !value) {
    return (
      <div className="flex min-w-0 flex-col gap-1.5">
        <span className={LABEL}>{t`Readings from print`}</span>
        <p className={`${HINT} m-0`}>
          {isPending ? t`Loading…` : t`No calibration page of this type was printed on this tray yet. The readings are saved without a print.`}
        </p>
      </div>
    );
  }
  const run = value.run;
  const requested = run ? labelOf(run.requested_tray) : "";
  const actual = run?.actual_media_source ? labelOf(run.actual_media_source) : "";
  const newest = value.id === defaultId;
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <SelectField
        label={t`Readings from print`}
        value={value.id}
        options={choices.map((c) => ({ id: c.id, label: c.label, section: c.section }))}
        onChange={onChange}
      />
      <p className={`${HINT} m-0`}>
        {newest ? `${t`The newest print of this printer, tray and type.`} ` : ""}
        {!run
          ? ""
          : actual
            ? t`Printed from ${actual}, requested ${requested}.`
            : run.media_source_reported
              ? t`Printed from ${requested}, as requested.`
              : t`Requested from ${requested}; the printer did not say which tray it took.`}
      </p>
    </div>
  );
}
