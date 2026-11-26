// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { Radio, RadioGroup } from "react-aria-components";
import type { Axis, Direction } from "./readings";

/** One half of the segmented control: ink-filled when chosen, quiet otherwise; 44 px tall for a thumb. */
const OPTION =
  "press flex min-h-10 flex-1 cursor-default items-center justify-center rounded-r1 px-2 text-center text-small font-bold text-ink-2 select-none [touch-action:manipulation] " +
  "transition-colors duration-150 data-[selected]:bg-ink data-[selected]:text-surface " +
  "data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-1 data-[focus-visible]:outline-link [@media(hover:hover)_and_(pointer:fine)]:hover:text-ink";

export interface DirectionPickerProps {
  axis: Axis;
  value: Direction;
  onChange: (direction: Direction) => void;
  /** The group's accessible name ("Across at S1 · Z1: where the ink lies"). */
  label: string;
}

/**
 * Where the ink lies, as two buttons: the sign of a reading is chosen, never typed (design 7.4).
 * Across: left or right of the middle of the gap; down: above or below the cut. Left and above are the
 * negative directions.
 */
export function DirectionPicker({ axis, value, onChange, label }: DirectionPickerProps) {
  const { t } = useLingui();
  const options: [Direction, string][] =
    axis === "across"
      ? [
          ["left", t`Left of the gap`],
          ["right", t`Right of the gap`],
        ]
      : [
          ["above", t`Above the cut`],
          ["below", t`Below the cut`],
        ];
  return (
    <RadioGroup
      aria-label={label}
      orientation="horizontal"
      value={value}
      onChange={(next) => onChange(next as Direction)}
      className="flex min-h-11 items-stretch gap-1 rounded-r2 border border-line-strong bg-surface p-0.5"
    >
      {options.map(([id, words]) => (
        <Radio key={id} value={id} className={OPTION}>
          {words}
        </Radio>
      ))}
    </RadioGroup>
  );
}
