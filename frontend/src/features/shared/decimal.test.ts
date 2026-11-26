// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { describe, expect, it } from "vitest";
import { parseDecimal } from "./decimal";
import { parseDecimal as fromSearch } from "../archive/search";

describe("parseDecimal", () => {
  it.each([
    ["12.5", 12.5],
    ["12,5", 12.5],
    [" 0,25 ", 0.25],
    ["7", 7],
    ["", null],
    ["abc", null],
    // Only the first comma becomes a point: a grouped number is no number (pinned; the zone editor relies on it).
    ["1.234,5", null],
  ])("%s → %s", (value, expected) => {
    expect(parseDecimal(value)).toBe(expected);
  });

  it("is still reachable from the archive search, where it lived before", () => {
    expect(fromSearch).toBe(parseDecimal);
  });
});
