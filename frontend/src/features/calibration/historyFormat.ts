// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import type { CalibrationHistoryEntry } from "./api";

/** The five values of a correction, in the order the history shows them (mockup M7). */
export const HISTORY_FIELDS = ["offset_x_mm", "offset_y_mm", "scale_x", "scale_y", "rotation_deg"] as const;
export type HistoryField = (typeof HISTORY_FIELDS)[number];
export type HistoryValues = CalibrationHistoryEntry["values"];

/** A value as the page shows it: offsets in mm, scales as the deviation in percent, rotation in degrees. */
export const UNIT: Record<HistoryField, "mm" | "%" | "°"> = { offset_x_mm: "mm", offset_y_mm: "mm", scale_x: "%", scale_y: "%", rotation_deg: "°" };
const DIGITS: Record<HistoryField, number> = { offset_x_mm: 2, offset_y_mm: 2, scale_x: 3, scale_y: 3, rotation_deg: 3 };

/** A missing value is no correction: offset 0, scale 1, rotation 0. */
const shown = (field: HistoryField, raw: number | null | undefined): number => {
  if (field === "scale_x" || field === "scale_y") return ((raw ?? 1) - 1) * 100;
  return raw ?? 0;
};

/** "+0.41", "−0.66", "0.00": the sign always written (a real minus sign), zero without one. */
export function signed(n: number, digits: number, locale: string): string {
  const rounded = Number(n.toFixed(digits));
  return new Intl.NumberFormat(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits, signDisplay: "exceptZero", useGrouping: false })
    .format(rounded === 0 ? 0 : rounded)
    .replace("-", "−");
}

/** The value of one field in its display unit, without the unit. */
export const valueText = (field: HistoryField, values: HistoryValues, locale: string): string => signed(shown(field, values[field]), DIGITS[field], locale);

/** The change of one field against the entry before, or null when it rounds to nothing. */
export function changeText(field: HistoryField, entry: CalibrationHistoryEntry, locale: string): string | null {
  const change = entry.diff[field];
  if (!change) return null;
  const delta = shown(field, change.new) - shown(field, change.old);
  const text = signed(delta, DIGITS[field], locale);
  return Number(delta.toFixed(DIGITS[field])) === 0 ? null : text;
}

/** The largest residual of a fit, in mm, or null without residuals. */
export const largestResidual = (entry: CalibrationHistoryEntry): number | null =>
  entry.residuals_mm && entry.residuals_mm.length > 0 ? Math.max(...entry.residuals_mm.map(Math.abs)) : null;
