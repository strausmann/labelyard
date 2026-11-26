// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { parseDecimal } from "../shared/decimal";

/** Moved to `features/shared/decimal.ts` (the calibration readings parse with it too); kept here for its callers. */
export { parseDecimal };

/** The address of the archive page: filters of the list, and the type that is open. */
export interface ArchiveSearch {
  q?: string | undefined;
  page?: "A4" | "Letter" | undefined;
  vendor?: string | undefined;
  /** Label width in mm. */
  w?: number | undefined;
  /** Label height in mm. */
  h?: number | undefined;
  archived?: true | undefined;
  /** The key of the open type. */
  type?: string | undefined;
  /** The tab: the installed types (no value) or the catalog. */
  tab?: "catalog" | undefined;
  /** Catalog: only entries checked on a real sheet. */
  verified?: true | undefined;
  /** Installed: usage state filter. */
  state?: "in_use" | "used_before" | "unused" | undefined;
  /** Installed: sort order (the server's default is by key). */
  sort?: "usage" | "last_used" | undefined;
  /** Installed: not used since this day (YYYY-MM-DD). */
  since?: string | undefined;
}

/** What the API accepts (`GET /api/v1/sheet-types`): longer values answer 422. */
export const MAX_QUERY = 100;
export const MAX_VENDOR = 200;

// The router parses `?q=123` as the number 123, so text arrives as a string or a number.
const text = (value: unknown): string | undefined => {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return typeof value === "string" && value.trim() !== "" ? value : undefined;
};

const size = (value: unknown): number | undefined => {
  const n = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
  return Number.isFinite(n) && n > 0 && n <= 1000 ? n : undefined;
};

/** Every key stays in the result even when rejected: the router merges it over the raw query string. */
export function parseArchiveSearch(search: Record<string, unknown>): ArchiveSearch {
  const page = text(search.page);
  const state = text(search.state);
  const sort = text(search.sort);
  const since = text(search.since);
  return {
    q: text(search.q)?.slice(0, MAX_QUERY),
    page: page === "A4" || page === "Letter" ? page : undefined,
    vendor: text(search.vendor)?.slice(0, MAX_VENDOR),
    w: size(search.w),
    h: size(search.h),
    archived: search.archived === true || search.archived === "true" || search.archived === 1 ? true : undefined,
    type: text(search.type),
    tab: search.tab === "catalog" ? "catalog" : undefined,
    verified: search.verified === true || search.verified === "true" || search.verified === 1 ? true : undefined,
    state: state === "in_use" || state === "used_before" || state === "unused" ? state : undefined,
    sort: sort === "usage" || sort === "last_used" ? sort : undefined,
    since: since && /^\d{4}-\d{2}-\d{2}$/.test(since) && !Number.isNaN(Date.parse(since)) ? since : undefined,
  };
}

/**
 * The catalog search box takes text or a label size: "45,7 x 21.2" asks for width and height (±0.5 mm) instead of
 * matching the text.
 */
export function catalogQuery(value: string | undefined): { q?: string; w?: number; h?: number } {
  const text = value?.trim();
  if (!text) return {};
  const size = /^(\d+(?:[.,]\d+)?)\s*(?:mm)?\s*[x×*]\s*(\d+(?:[.,]\d+)?)\s*(?:mm)?$/i.exec(text);
  if (size) {
    const w = parseDecimal(size[1]!);
    const h = parseDecimal(size[2]!);
    if (w !== null && h !== null && w > 0 && h > 0 && w <= 1000 && h <= 1000) return { w, h };
  }
  return { q: text.slice(0, MAX_QUERY) };
}


/** A source address worth a link: only http and https, and never one that carries user info (#204). */
export function httpUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return url.username || url.password ? null : url.href;
  } catch {
    return null;
  }
}

/**
 * The host of a stored link as the browser reads it, shown next to the link text (#204): `URL.host`
 * is the punycode form (`xn--…`) for an international name and carries the port, so a look-alike
 * cannot pass for the name it imitates. `null` for anything `httpUrl` would not link.
 */
export function linkHost(value: string | null | undefined): string | null {
  const href = httpUrl(value);
  return href ? new URL(href).host : null;
}
