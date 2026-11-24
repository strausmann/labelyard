// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { ApiError } from "../../api/client";
import type { components } from "../../api/schema.gen";
import { cellRect, pageSizeMm, type GeometryMm } from "../sheets/cellState";

/** A keep-out zone on the front of a sheet, in mm from the page's top left (#246). */
export type Zone = components["schemas"]["PreprintZoneMm"];
export type MarginBand = components["schemas"]["SheetTypePatch"]["id_margin_band"] & string;
export type MarginAlign = components["schemas"]["SheetTypePatch"]["id_margin_align"] & string;

/** The server's cap on a type's own zones (`MAX_ZONES`). */
export const MAX_ZONES = 16;
/** The offset stepper's step. */
export const OFFSET_STEP_MM = 0.5;

/** A length as the zone list and the stepper show it: one decimal, two when it has them ("12.0", "9.85"). */
export function formatTenth(value: number, locale: string): string {
  return new Intl.NumberFormat(locale, { minimumFractionDigits: 1, maximumFractionDigits: 2, useGrouping: false }).format(
    Math.round(value * 100) / 100,
  );
}

/** The label cells a zone overlaps (a shared edge is no overlap, as on the server). */
export function coveredCells(g: GeometryMm, z: Zone): number[] {
  const out: number[] = [];
  for (let n = 1; n <= g.cols * g.rows; n++) {
    const c = cellRect(g, n);
    if (z.x_mm < c.x + c.w && c.x < z.x_mm + z.w_mm && z.y_mm < c.y + c.h && c.y < z.y_mm + z.h_mm) out.push(n);
  }
  return out;
}

/** Whether a zone reaches outside the page. */
export function offPage(g: GeometryMm, z: Zone): boolean {
  const page = pageSizeMm(g);
  return z.x_mm < 0 || z.y_mm < 0 || z.x_mm + z.w_mm > page.w + 1e-9 || z.y_mm + z.h_mm > page.h + 1e-9;
}

/** Row and column (1-based) of a cell. */
export function rowCol(g: GeometryMm, cell: number): { row: number; col: number } {
  return { row: Math.floor((cell - 1) / g.cols) + 1, col: ((cell - 1) % g.cols) + 1 };
}

export interface ZoneRefusal {
  /** `own`: the type's own list (`preprint_zones.N`); `preset`: the linked preset's (`preprint_preset.N`). */
  list: "own" | "preset";
  index: number;
  code: "preprint_zone_overlaps_cell" | "preprint_zone_off_page";
}

/** The zone a `422 preprint_zone_*` names, or null for any other error. */
export function zoneRefusal(error: unknown): ZoneRefusal | null {
  if (!(error instanceof ApiError)) return null;
  if (error.code !== "preprint_zone_overlaps_cell" && error.code !== "preprint_zone_off_page") return null;
  const field = error.errorDetail?.field;
  const match = typeof field === "string" ? /^(preprint_zones|preprint_preset)\.(\d+)$/.exec(field) : null;
  if (!match) return null;
  return { list: match[1] === "preprint_zones" ? "own" : "preset", index: Number(match[2]), code: error.code };
}

/** The zones the placement keeps free, as the server counts them: the preset's first, then the type's own. */
export function allZones(own: readonly Zone[], preset: readonly Zone[] | null | undefined): Zone[] {
  return [...(preset ?? []), ...own];
}
