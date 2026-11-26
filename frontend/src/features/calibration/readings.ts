// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { parseDecimal } from "../shared/decimal";

/**
 * Where the ink lies against the middle of the gap (across) or the cut (down). Right and below are the
 * positive directions of the fit (`ReadingIn.across_mm` / `down_mm`), left and above the negative ones.
 */
export type AcrossDirection = "right" | "left";
export type DownDirection = "below" | "above";
export type Direction = AcrossDirection | DownDirection;
export type Axis = "across" | "down";

/** The fine step of the readings: the guide asks for 0.1 mm estimates between the 0.5 mm ticks (design 7.4). */
export const READING_STEP_MM = 0.1;

/** A reading rounded to 0.1 mm; `snapped` says whether that changed the typed value. */
export function snapReading(value: number): { value: number; snapped: boolean } {
  const rounded = Math.round(value * 10) / 10;
  // 0.1 + 0.2 is 0.30000000000000004: noise below a micrometre is no change the person made.
  return { value: rounded === 0 ? 0 : rounded, snapped: Math.abs(rounded - value) > 1e-9 };
}

const NEGATIVE: ReadonlySet<Direction> = new Set(["left", "above"]);

/** The value the fit takes: the size of the reading with the sign of its direction. */
export function signedReading(magnitude: number, direction: Direction): number {
  const size = Math.abs(magnitude);
  if (size === 0) return 0;
  return NEGATIVE.has(direction) ? -size : size;
}

/**
 * A sign typed into the field moves into the direction picker: "-0,4" means 0.4 to the left (or above),
 * "+0,4" 0.4 to the right (or below). The field keeps only the size; `direction` is null when nothing
 * was typed that changes it.
 */
export function takeSign(text: string, axis: Axis): { text: string; direction: Direction | null } {
  const match = /^\s*([-−+])\s*/.exec(text);
  if (!match) return { text, direction: null };
  const negative = match[1] !== "+";
  const direction: Direction = axis === "across" ? (negative ? "left" : "right") : negative ? "above" : "below";
  return { text: text.slice(match[0].length), direction };
}

/** One axis of a reading as the form holds it: the typed size and the chosen direction. */
export interface AxisInput {
  text: string;
  direction: Direction;
  /** What was typed before the field was set to the snapped value on leaving it, for the hint. */
  typed?: string;
}

/** The signed, snapped value of one axis, or null while the field is empty or holds no number. */
export function axisValue(input: AxisInput): { value: number; snapped: boolean } | null {
  const parsed = parseDecimal(input.text);
  if (parsed === null) return null;
  const snap = snapReading(Math.abs(parsed));
  return { value: signedReading(snap.value, input.direction), snapped: snap.snapped };
}
