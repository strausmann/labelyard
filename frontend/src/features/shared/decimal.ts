// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * A decimal typed by a person ("45,7" or "45.7", an optional sign); null when it is not one. Only digits, one decimal
 * separator and a leading sign count: an exponent ("1e1"), a hex or binary literal ("0x10") or "Infinity", which
 * `Number()` would read, is no typed decimal.
 */
export function parseDecimal(value: string): number | null {
  const trimmed = value.trim();
  if (!/^[+-]?(\d+([.,]\d*)?|[.,]\d+)$/.test(trimmed)) return null;
  const n = Number(trimmed.replace(",", "."));
  return Number.isFinite(n) ? n : null;
}
