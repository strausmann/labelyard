// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { describe, expect, it } from "vitest";
import { ApiError } from "../../api/client";
import { GEOMETRY_L4736 } from "../test-api";
import { allZones, coveredCells, formatTenth, offPage, rowCol, zoneRefusal } from "./preprint";

// L4736 fixture: first row from y 21.5 to 42.7 mm, first column from x 8.4 to 54.1 mm, 210 x 297 mm page.
const G = GEOMETRY_L4736;
const zone = (x_mm: number, y_mm: number, w_mm: number, h_mm: number) => ({ x_mm, y_mm, w_mm, h_mm });
const refusal = (code: string, field: unknown) => new ApiError(422, code, "x", { field });

describe("coveredCells", () => {
  it("a zone ending exactly at the first row's top edge covers nothing (a shared edge is no overlap, as on the server)", () => {
    expect(coveredCells(G, zone(10, 10, 30, 11.5))).toEqual([]);
    // ... and one beside the first label, ending at its left edge.
    expect(coveredCells(G, zone(0, 30, 8.4, 5))).toEqual([]);
  });

  it("a zone reaching into the first label covers it, one over two labels covers both", () => {
    expect(coveredCells(G, zone(10, 10, 30, 11.6))).toEqual([1]);
    expect(coveredCells(G, zone(40, 22, 30, 5))).toEqual([1, 2]);
  });
});

describe("offPage", () => {
  it.each([
    ["left", zone(-0.1, 5, 10, 5)],
    ["top", zone(5, -0.1, 10, 5)],
    ["right", zone(200.1, 5, 10, 5)],
    ["bottom", zone(5, 292.1, 10, 5)],
  ])("a zone past the %s edge is off the page", (_edge, z) => {
    expect(offPage(G, z)).toBe(true);
  });

  it("a zone touching all four edges is on the page", () => {
    expect(offPage(G, zone(0, 0, 210, 297))).toBe(false);
  });
});

describe("zoneRefusal", () => {
  it("maps preprint_zones.N to the own list and preprint_preset.N to the preset's", () => {
    expect(zoneRefusal(refusal("preprint_zone_overlaps_cell", "preprint_zones.3"))).toEqual({
      list: "own",
      index: 3,
      code: "preprint_zone_overlaps_cell",
    });
    expect(zoneRefusal(refusal("preprint_zone_off_page", "preprint_preset.0"))).toEqual({
      list: "preset",
      index: 0,
      code: "preprint_zone_off_page",
    });
  });

  it("is null for another code, a field without an index, another field or no ApiError", () => {
    expect(zoneRefusal(refusal("id_position_overlaps_cell", "preprint_zones.0"))).toBeNull();
    expect(zoneRefusal(refusal("preprint_zone_overlaps_cell", "preprint_zones"))).toBeNull();
    expect(zoneRefusal(refusal("preprint_zone_overlaps_cell", "zones.0"))).toBeNull();
    expect(zoneRefusal(refusal("preprint_zone_overlaps_cell", 3))).toBeNull();
    expect(zoneRefusal(new Error("preprint_zone_overlaps_cell"))).toBeNull();
  });
});

describe("helpers", () => {
  it("rowCol is 1-based, allZones puts the preset's first and formatTenth keeps one decimal", () => {
    expect(rowCol(G, 6)).toEqual({ row: 2, col: 2 });
    expect(allZones([zone(1, 1, 1, 1)], [zone(2, 2, 2, 2)])).toEqual([zone(2, 2, 2, 2), zone(1, 1, 1, 1)]);
    expect(formatTenth(12, "en")).toBe("12.0");
    expect(formatTenth(9.85, "de")).toBe("9,85");
  });
});
