// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Button, Radio, RadioGroup } from "react-aria-components";
import { useSheetType, type SheetTypeRead } from "../archive/api";
import { parseDecimal } from "../shared/decimal";
import { HINT, LABEL, SelectField, type Option } from "../settings/controls";
import { PRIMARY_BUTTON, SECONDARY_BUTTON } from "../ui";
import { useCalibrationScale, useFit, type FitResponse } from "./api";
import { DirectionPicker } from "./DirectionPicker";
import { useCalibrationErrorText } from "./errors";
import { LiveFitPanel, useLiveFit, type FitResult } from "./LiveFit";
import { completeReadings, crossingName, emptyReading, MIN_READINGS, readingsRequest, suggestedCrossings, type ReadingRow } from "./model";
import { axisValue, READING_STEP_MM, snapReading, takeSign, type Axis, type AxisInput, type Direction } from "./readings";
import { ReadingStepper } from "./ReadingStepper";
import { RunPicker, useRunChoices } from "./RunPicker";
import { SuggestionCard } from "./SuggestionCard";

const MAX_ROWS = 200;

/** The first crossing, counting across then down, that no row names yet; S1 · Z1 when all are taken. */
function unusedCrossing(list: readonly ReadingRow[], cols: number, rows: number) {
  for (let z = 1; z < rows; z++) {
    for (let s = 1; s < cols; s++) {
      if (!list.some((r) => r.col_gap === s && r.row_gap === z)) return { col_gap: s, row_gap: z };
    }
  }
  return { col_gap: 1, row_gap: 1 };
}

/** Every S line of the sheet (S1 … S(cols−1)), or every Z line: a reading may be taken at any of them. */
const gapOptions = (prefix: "S" | "Z", n: number): Option[] =>
  Array.from({ length: n }, (_, i) => ({ id: String(i + 1), label: `${prefix}${i + 1}` }));

/** What a fit of the readings is, for the values form: the type version it was fitted on, the print and the readings. */
export interface FittedFrom {
  type_key: string;
  version: number;
  /** The print the readings belong to; null when no print was recorded for them. */
  run: { id: string; run_no: number; kind: string } | null;
  readings: { col_gap: number; row_gap: number; across_mm: number; down_mm: number }[];
}

/** The step of the stepper, as the segmented control offers it: the printed scale or the 0.1 mm estimate. */
type Step = "scale" | "fine";

const SEGMENT =
  "press flex min-h-10 flex-1 whitespace-nowrap cursor-default items-center justify-center rounded-r1 px-3 text-center text-small font-bold text-ink-2 select-none [touch-action:manipulation] transition-colors duration-150 data-[selected]:bg-ink data-[selected]:text-surface data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-1 data-[focus-visible]:outline-link";

const WarnIcon = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" className="mt-0.5 shrink-0 fill-none stroke-current stroke-2">
    <path d="M12 4 21 19.5H3L12 4Z" />
    <path d="M12 10v4.5M12 17h.01" />
  </svg>
);

interface ReadingCardProps {
  row: ReadingRow;
  /** The 1-based position shown to the person. */
  n: number;
  colOptions: Option[];
  rowOptions: Option[];
  step: number;
  /** The end of the printed scale in mm (`half_mm`); a reading past it is warned about. */
  half: number;
  onEdit: (id: number, patch: Partial<ReadingRow>) => void;
  onAxis: (id: number, axis: Axis, change: { text?: string; direction?: Direction; blur?: boolean }) => void;
  onRemove: (id: number) => void;
}

/**
 * One reading (mockup M8): the crossing, then per axis where the ink lies and how far. Memoised: every prop is stable
 * while this row is untouched, so editing one card does not re-render the others (each carries two selects).
 */
export const ReadingCard = memo(function ReadingCard({ row: r, n, colOptions, rowOptions, step, half, onEdit, onAxis, onRemove }: ReadingCardProps) {
  const { t, i18n } = useLingui();
  const id = useId();
  const name = crossingName(r.col_gap, r.row_gap);
  const format = (v: number) => new Intl.NumberFormat(i18n.locale, { maximumFractionDigits: 1, useGrouping: false }).format(v);
  const halfText = new Intl.NumberFormat(i18n.locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(half);
  const outside = (["across", "down"] as const).some((axis) => {
    const v = axisValue(r[axis]);
    return v !== null && Math.abs(v.value) > half + 1e-9;
  });
  const warningId = `${id}-warn`;

  const column = (axis: Axis) => {
    const input: AxisInput = r[axis];
    const parsed = parseDecimal(input.text);
    const snap = parsed === null ? null : snapReading(Math.abs(parsed));
    // The hint stays after the field took the snapped value, so the person sees what happened to what they typed.
    const typed = input.typed ?? (snap?.snapped ? input.text : undefined);
    const savedAs = snap ? format(snap.value) : "";
    const hintId = `${id}-${axis}-hint`;
    const label = axis === "across" ? t`Across at ${name} (mm)` : t`Down at ${name} (mm)`;
    const value = axisValue(input);
    const past = value !== null && Math.abs(value.value) > half + 1e-9;
    return (
      <div data-axis={axis} className="flex min-w-0 flex-col gap-2">
        <span aria-hidden="true" className={LABEL}>
          {axis === "across" ? t`Across` : t`Down`}
        </span>
        <DirectionPicker
          axis={axis}
          value={input.direction}
          onChange={(direction) => onAxis(r.id, axis, { direction })}
          label={axis === "across" ? t`Across at ${name}: where the ink lies` : t`Down at ${name}: where the ink lies`}
        />
        <ReadingStepper
          label={label}
          text={input.text}
          onChange={(text) => onAxis(r.id, axis, { text })}
          onBlur={() => onAxis(r.id, axis, { blur: true })}
          step={step}
          invalid={past}
          describedBy={[typed !== undefined && hintId, past && warningId].filter(Boolean).join(" ") || undefined}
        />
        {typed !== undefined && snap && (
          <p id={hintId} className={`${HINT} m-0`}>
            {t`You typed ${typed}. Readings snap to 0.1 mm, so it is saved as ${savedAs} mm.`}
          </p>
        )}
      </div>
    );
  };

  return (
    <li className="list-none">
      <fieldset aria-label={t`Reading ${n}`} className="m-0 flex min-w-0 flex-col gap-4 rounded-r3 border border-line bg-surface p-4">
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
          <div className="flex items-center gap-2">
            <span aria-hidden="true" className="font-bold whitespace-nowrap">
              {t`Reading ${n}`}
            </span>
            <div className="w-21">
              <SelectField hideLabel label={t`Gap S of reading ${n}`} value={String(r.col_gap)} options={colOptions} onChange={(v) => onEdit(r.id, { col_gap: Number(v) })} />
            </div>
            <div className="w-21">
              <SelectField hideLabel label={t`Gap Z of reading ${n}`} value={String(r.row_gap)} options={rowOptions} onChange={(v) => onEdit(r.id, { row_gap: Number(v) })} />
            </div>
          </div>
          <Button
            aria-label={t`Remove reading ${n}`}
            onPress={() => onRemove(r.id)}
            className="press min-h-11 rounded-r1 px-2 text-small font-bold text-link underline underline-offset-2 data-[focus-visible]:outline-3 data-[focus-visible]:outline-link"
          >
            {t`Remove`}
          </Button>
        </div>
        <div className="grid grid-cols-2 gap-3 sm:gap-4">
          {column("across")}
          {column("down")}
        </div>
        {outside && (
          <p id={warningId} className="m-0 flex gap-2 text-small text-bad">
            <WarnIcon />
            <span>{t`The print is off by more than the scale shows (±${halfText} mm). Check the tray, the sheet type or the printer.`}</span>
          </p>
        )}
      </fieldset>
    </li>
  );
});

export interface ReadingsHelperProps {
  printerId: string;
  tray: string;
  type: SheetTypeRead;
  /** The label of any tray of this printer. */
  labelOf: (key: string) => string;
  /** The type the stored correction was measured on, as a label; null when not known. */
  measuredOn: string | null;
  admin: boolean;
  /** Called with the fitted values and what they were fitted from, when they are inside the allowed ranges. */
  onFitted: (values: FitResponse["values"], from: FittedFrom) => void;
}

/**
 * The guided readings (#240 B, design 7.4, mockup M8). The person picks the print the readings come from, reads the
 * deviation at named crossings with an explicit direction, and sees the fit as they type. "Fit" fills the values
 * below; nothing is stored here.
 */
export function ReadingsHelper({ printerId, tray, type, labelOf, measuredOn, admin, onFitted }: ReadingsHelperProps) {
  const { t, i18n } = useLingui();
  const locale = i18n.locale;
  const errorText = useCalibrationErrorText();
  const scale = useCalibrationScale().data;
  const half = scale?.half_mm ?? 2;
  const fit = useFit(printerId);
  const initial = useState(() => suggestedCrossings(type.geometry.cols, type.geometry.rows).map((c, i) => emptyReading(i, c)))[0];
  // Row ids come from one counter that only counts up: a removed row's id is never handed out again.
  const nextId = useRef(initial.length);
  const [list, setList] = useState<ReadingRow[]>(initial);
  const [stepKind, setStepKind] = useState<Step>("fine");
  const step = stepKind === "fine" ? READING_STEP_MM : (scale?.step_mm ?? 0.5);
  const [chosenRun, setChosenRun] = useState<string | null>(null);
  const [fitted, setFitted] = useState<FitResult | null>(null);
  const [outcome, setOutcome] = useState<"filled" | "check" | null>(null);

  const { choices, defaultChoice, isPending: runsPending, isError: runsFailed, refreshFailed } = useRunChoices(printerId, tray, {
    key: type.key,
    version: type.current_version,
  });
  const run = choices.find((c) => c.id === chosenRun) ?? defaultChoice;
  // Once a reading is typed it belongs to the print shown: a refetch that brings a newer print does not move it.
  const typed = list.some((r) => r.across.text !== "" || r.down.text !== "");
  if (typed && chosenRun === null && run) setChosenRun(run.id);
  // A fit with a run must name the version the run was printed for (else 422 run_version_mismatch).
  const version = run?.version ?? type.current_version;
  // The crossings are those of that version's grid: an older print may have other rows or columns than the type now.
  const detail = useSheetType(type.key, version !== type.current_version);
  const printed = version !== type.current_version ? detail.data?.versions.find((v) => v.version === version)?.geometry : undefined;
  // Until that version's grid is known (or when it cannot be loaded) nothing is read: not on the current grid either.
  const gridState: "ready" | "loading" | "error" = version === type.current_version || printed
      ? "ready"
      : // A detail that loaded without the print's version (deleted since) is as final as a failed load.
        detail.isError || detail.isSuccess
        ? "error"
        : "loading";
  const { cols, rows } = printed ?? type.geometry;
  // Another grid: the readings start again at its suggested crossings (they belong to another page).
  const [grid, setGrid] = useState(`${cols}x${rows}`);
  if (grid !== `${cols}x${rows}`) {
    setGrid(`${cols}x${rows}`);
    // New ids above every one in use, so no card keeps the state of a removed one.
    const from = list.reduce((max, r) => Math.max(max, r.id + 1), 0);
    setList(suggestedCrossings(cols, rows).map((c, i) => emptyReading(from + i, c)));
  }
  const colOptions = useMemo(() => gapOptions("S", Math.max(cols - 1, 1)), [cols]);
  const rowOptions = useMemo(() => gapOptions("Z", Math.max(rows - 1, 1)), [rows]);
  const request = useMemo(() => readingsRequest(type.key, version, list, run?.id ?? null), [type.key, version, list, run?.id]);
  const complete = request.readings.length;
  // No fit without the print the readings belong to, when the prints could not be loaded; none on an unknown grid.
  const canFit = !runsFailed && gridState === "ready";
  const live = useLiveFit(printerId, tray, canFit ? request : null);
  // The request the form shows now, for an answer that arrives after the readings or the print changed.
  const latest = useRef(request);
  useEffect(() => {
    latest.current = request;
  }, [request]);

  const { reset: resetFit } = fit;
  const changed = useCallback(() => {
    setFitted(null);
    setOutcome(null);
    resetFit();
  }, [resetFit]);
  // Stable between renders: they are props of the memoised cards.
  const edit = useCallback(
    (id: number, patch: Partial<ReadingRow>) => {
      setList((now) => now.map((r) => (r.id === id ? { ...r, ...patch } : r)));
      changed();
    },
    [changed],
  );
  const axisEdit = useCallback(
    (id: number, axis: Axis, change: { text?: string; direction?: Direction; blur?: boolean }) => {
      setList((now) =>
        now.map((r) => {
          if (r.id !== id) return r;
          const before = r[axis];
          let next: AxisInput = before;
          if (change.text !== undefined) {
            const signed = takeSign(change.text, axis);
            next = { text: signed.text, direction: signed.direction ?? before.direction };
          } else if (change.direction) {
            next = { ...before, direction: change.direction };
          } else if (change.blur) {
            // Leaving the field shows the value that is used; the hint keeps what was typed.
            const parsed = parseDecimal(before.text);
            const snap = parsed === null ? null : snapReading(Math.abs(parsed));
            if (!snap?.snapped) return r;
            next = { ...before, typed: before.text, text: new Intl.NumberFormat(locale, { maximumFractionDigits: 1, useGrouping: false }).format(snap.value) };
          }
          return next === before ? r : { ...r, [axis]: next };
        }),
      );
      if (!change.blur) changed();
    },
    [changed, locale],
  );
  const add = () => {
    const id = list.reduce((max, r) => Math.max(max, r.id + 1), nextId.current);
    nextId.current = id + 1;
    setList((now) => [...now, emptyReading(id, unusedCrossing(now, cols, rows))]);
  };
  const remove = useCallback(
    (id: number) => {
      setList((now) => now.filter((r) => r.id !== id));
      changed();
    },
    [changed],
  );

  const runNo = run?.run_no ?? 0;
  const press = () => {
    setOutcome(null);
    const sent = request;
    fit.mutate(
      { tray, request: sent },
      {
        onSuccess: (response) => {
          // The readings or the print changed while the fit ran: the answer belongs to neither. Today every change also
          // resets the mutation (`changed`), which drops this callback first; this check keeps it so if that ever moves.
          if (JSON.stringify(latest.current) !== JSON.stringify(sent)) return;
          setFitted({ request: sent, response });
          // A check print shows what is left with the stored correction: its fit is no new correction.
          if (run?.kind === "check_page") return setOutcome("check");
          if (response.out_of_range.length === 0) {
            onFitted(response.values, {
              type_key: type.key,
              version: sent.version ?? type.current_version,
              run: run ? { id: run.id, run_no: run.run_no, kind: run.kind } : null,
              readings: sent.readings,
            });
            setOutcome("filled");
          }
        },
      },
    );
  };

  // The server numbers the complete readings it got; the person sees rows, empty ones included.
  const completeRows = list.flatMap((r, i) => (completeReadings([r]).length === 1 ? [i + 1] : []));
  const shown = fitted ?? live.result;
  // The suggestion acts on one print and one set of readings: while the readings or the print differ from the fit
  // shown (recalculating, or the new fit failed), it is not offered.
  const shownIsCurrent = shown !== null && JSON.stringify(shown.request) === JSON.stringify(request);

  // A reading's field takes the focus, or the keyboard opens under it: its direction picker and the field come into the
  // part of the screen between the top bar and the actions bar (mockup M8, entry view). The browser only scrolls the
  // field itself into view, and only against the layout viewport, which iOS does not shrink for the keyboard.
  const listRef = useRef<HTMLUListElement>(null);
  const barRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const vv = window.visualViewport;
    const keep = () => {
      const active = document.activeElement;
      if (!(active instanceof HTMLInputElement) || !listRef.current?.contains(active)) return;
      const column = active.closest("[data-axis]");
      const bar = barRef.current;
      if (!column || !bar) return;
      const box = column.getBoundingClientRect();
      const header = document.querySelector("header")?.getBoundingClientRect().bottom ?? 0;
      const top = (vv?.offsetTop ?? 0) + header + 8;
      const bottom = bar.getBoundingClientRect().top - 8;
      if (box.bottom > bottom) window.scrollBy({ top: Math.min(box.bottom - bottom, box.top - top) });
      else if (box.top < top) window.scrollBy({ top: box.top - top });
    };
    // After the browser's own scroll and after viewport.ts has moved the actions bar to the new visible area.
    const later = () => requestAnimationFrame(() => requestAnimationFrame(keep));
    const node = listRef.current;
    node?.addEventListener("focusin", later);
    vv?.addEventListener("resize", later);
    return () => {
      node?.removeEventListener("focusin", later);
      vv?.removeEventListener("resize", later);
    };
  }, [gridState]);

  // With two columns (or two rows) every crossing lies on one line: nothing to fit.
  if (cols <= 2 || rows <= 2) {
    return <p className={HINT}>{t`On this sheet all crossings lie on one line, so the readings cannot be fitted. Enter the values directly.`}</p>;
  }

  return (
    <div className="grid min-w-0 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(17rem,24rem)] lg:items-start">
      <div className="flex min-w-0 flex-col gap-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <RunPicker
            choices={choices}
            value={run}
            onChange={(id) => {
              setChosenRun(id);
              changed();
            }}
            isPending={runsPending}
            isError={runsFailed}
            refreshFailed={refreshFailed}
            defaultId={defaultChoice?.id ?? null}
            labelOf={labelOf}
          />
          <div className="flex min-w-0 flex-col gap-1.5">
            <RadioGroup
              aria-label={t`Step`}
              orientation="horizontal"
              value={stepKind}
              onChange={(v) => setStepKind(v as Step)}
              className="flex min-w-0 flex-col gap-1.5"
            >
              <span aria-hidden="true" className={LABEL}>{t`Step`}</span>
              <div className="flex min-h-11 w-fit max-w-full items-stretch gap-1 rounded-r2 border border-line-strong bg-surface p-0.5">
                <Radio value="scale" className={SEGMENT}>{t`0.5 mm (the scale)`}</Radio>
                <Radio value="fine" className={SEGMENT}>{t`0.1 mm`}</Radio>
              </div>
            </RadioGroup>
            <p className={`${HINT} m-0`}>{stepKind === "fine" ? t`Estimate between two ticks to 0.1 mm.` : t`Each press moves one tick of the printed scale.`}</p>
          </div>
        </div>

        {gridState !== "ready" && run && (
          <p role={gridState === "error" ? "alert" : "status"} className={`m-0 ${gridState === "error" ? "text-bad" : HINT}`}>
            {gridState === "error"
              ? t`The sheet of print #${runNo} (version ${version}) could not be loaded, so its crossings are not known. Reload the page to try again.`
              : t`Loading the sheet of print #${runNo} (version ${version})…`}
          </p>
        )}
        {gridState === "ready" && (
        <ul ref={listRef} aria-label={t`Readings`} className="m-0 flex flex-col gap-4 p-0">
          {list.map((r, index) => (
            <ReadingCard
              key={r.id}
              row={r}
              n={index + 1}
              colOptions={colOptions}
              rowOptions={rowOptions}
              step={step}
              half={half}
              onEdit={edit}
              onAxis={axisEdit}
              onRemove={remove}
            />
          ))}
        </ul>
        )}

        {fit.isError && (
          <p role="alert" className="m-0 text-bad">
            {errorText(fit.error, t`The fit failed.`, completeRows)}
          </p>
        )}
        {outcome === "filled" && (
          <p role="status" className="m-0 text-ok">
            {t`The values are filled in below. Nothing is saved yet.`}
          </p>
        )}
        {outcome === "check" && (
          <p role="status" className="m-0">
            {t`These are check readings: they show what is left with the stored correction, so they are not filled in as a new one.`}
          </p>
        )}

        {/* The two actions stay in sight while a reading is typed (mockup M8, entry view): above the tab bar on a phone, and
            above the on-screen keyboard wherever it only covers the page (--vv-top/--vvh, src/shell/viewport.ts). */}
        <div
          ref={barRef}
          className="sticky bottom-[max(calc(5.5rem_+_env(safe-area-inset-bottom,0px)),calc(100dvh_-_var(--vv-top)_-_var(--vvh)_+_0.5rem))] z-[5] -mx-4 flex flex-wrap items-center gap-3 border-t border-line bg-bg px-4 py-3 lg:bottom-[max(0px,calc(100dvh_-_var(--vv-top)_-_var(--vvh)))] lg:mx-0 lg:px-0"
        >
          <Button onPress={add} isDisabled={list.length >= MAX_ROWS} className={`${SECONDARY_BUTTON} gap-2`}>
            <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" className="fill-none stroke-current stroke-2">
              <path d="M12 5v14M5 12h14" strokeLinecap="round" />
            </svg>
            {t`Add reading`}
          </Button>
          <Button onPress={press} isDisabled={!canFit || complete < MIN_READINGS || fit.isPending} className={PRIMARY_BUTTON}>
            {t`Fit`}
          </Button>
          <span className={HINT}>{complete < MIN_READINGS ? t`Enter at least three readings to fit.` : t`${complete} readings`}</span>
        </div>
      </div>

      <div className="flex min-w-0 flex-col gap-4 lg:sticky lg:top-20">
        <LiveFitPanel result={shown} error={fit.isIdle ? live.error : null} updating={!fitted && live.updating} complete={complete} readingRows={completeRows} />
        {shown && shownIsCurrent && (
          <SuggestionCard
            type={type}
            run={run ? { id: run.id, run_no: run.run_no, kind: run.kind, version: run.version } : null}
            response={shown.response}
            readings={shown.request.readings.length}
            measuredOn={measuredOn}
            admin={admin}
          />
        )}
      </div>
    </div>
  );
}
