// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { useId, type KeyboardEvent } from "react";
import { parseDecimal } from "../shared/decimal";
import { snapReading } from "./readings";

/** The minus sign and plus of the stepper: drawn, so they share one stroke width and sit on the same centre. */
const Minus = () => (
  <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" className="fill-none stroke-current stroke-2">
    <path d="M3.5 8h9" strokeLinecap="round" />
  </svg>
);
const Plus = () => (
  <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" className="fill-none stroke-current stroke-2">
    <path d="M3.5 8h9M8 3.5v9" strokeLinecap="round" />
  </svg>
);

/** A side button of the stepper: a 44 px target that answers the press at once (`press`), quiet at rest. */
const SIDE =
  "press flex w-10 shrink-0 items-center justify-center self-stretch text-ink select-none [touch-action:manipulation] active:bg-line disabled:opacity-60 [@media(hover:hover)_and_(pointer:fine)]:hover:bg-line";

export interface ReadingStepperProps {
  /** The accessible name of the field ("Across at S1 · Z1 (mm)"); the buttons are named after it. */
  label: string;
  /** The size as typed (no sign; the direction picker holds it). */
  text: string;
  onChange: (text: string) => void;
  onBlur?: () => void;
  /** mm per press and per arrow key: 0.1 (estimating between ticks) or the printed scale's 0.5. */
  step: number;
  /** Marks the field (its frame) when the value is past the printed scale. */
  invalid?: boolean;
  /** Ids of the hint and warning under the field. */
  describedBy?: string;
}

/**
 * The size of one reading: a decimal field between − and +. The buttons and the arrow keys move by `step`
 * from the value snapped to 0.1 mm and never below zero; the sign is the direction picker's (design 7.4).
 * The buttons are left out of the tab order, because the arrow keys do the same from the field.
 */
export function ReadingStepper({ label, text, onChange, onBlur, step, invalid, describedBy }: ReadingStepperProps) {
  const { t, i18n } = useLingui();
  const id = useId();
  const format = (v: number) => new Intl.NumberFormat(i18n.locale, { maximumFractionDigits: 1, useGrouping: false }).format(v);
  const move = (direction: 1 | -1) => {
    const now = snapReading(Math.abs(parseDecimal(text) ?? 0)).value;
    // In whole tenths, so 0.1 + 0.2 is 0.3 and not 0.30000000000000004.
    const next = Math.max(0, Math.round(now * 10) + direction * Math.round(step * 10)) / 10;
    onChange(format(next));
  };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      event.preventDefault();
      move(event.key === "ArrowUp" ? 1 : -1);
    }
  };
  return (
    <div
      className={`flex h-11 min-w-0 items-stretch overflow-hidden rounded-r2 border bg-sunken focus-within:outline-3 focus-within:outline-offset-2 focus-within:outline-link ${
        invalid ? "border-bad shadow-[inset_0_0_0_1px_var(--bad)]" : "border-line-strong"
      }`}
    >
      <button type="button" tabIndex={-1} aria-label={t`Less: ${label}`} onClick={() => move(-1)} className={`${SIDE} border-r border-line`}>
        <Minus />
      </button>
      <label htmlFor={id} className="sr-only">
        {label}
      </label>
      <div className="flex min-w-0 flex-1 items-baseline justify-center gap-1 bg-surface px-1">
        <input
          id={id}
          value={text}
          onChange={(event) => onChange(event.target.value)}
          onBlur={onBlur}
          onKeyDown={onKeyDown}
          inputMode="decimal"
          enterKeyHint="next"
          autoComplete="off"
          spellCheck={false}
          aria-invalid={invalid || undefined}
          aria-describedby={describedBy}
          placeholder="0"
          // 16 px: a smaller field makes iOS zoom the page in when it takes the focus.
          className="h-full w-0 min-w-0 flex-1 bg-transparent text-center text-[16px] sm:text-right font-bold tabular-nums text-ink outline-none placeholder:font-normal placeholder:text-ink-3"
        />
        <span aria-hidden="true" className="shrink-0 pr-1 text-small text-ink-2">
          mm
        </span>
        {/* From the small breakpoint the field is wide: the value and its unit sit together in the middle. */}
        <span aria-hidden="true" className="hidden flex-1 sm:block" />
      </div>
      <button type="button" tabIndex={-1} aria-label={t`More: ${label}`} onClick={() => move(1)} className={`${SIDE} border-l border-line`}>
        <Plus />
      </button>
    </div>
  );
}
