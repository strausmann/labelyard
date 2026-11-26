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
    // Only digits, one separator and an optional sign: what Number() also reads is no typed decimal.
    ["+1,5", 1.5],
    ["-2", -2],
    [".5", 0.5],
    ["1e1", null],
    ["0x10", null],
    ["0b1", null],
    ["Infinity", null],
    ["1.2.3", null],
    ["1,", 1],
    ["--1", null],
  ])("%s → %s", (value, expected) => {
    expect(parseDecimal(value)).toBe(expected);
  });

  it("is still reachable from the archive search, where it lived before", () => {
    expect(fromSearch).toBe(parseDecimal);
  });
});
