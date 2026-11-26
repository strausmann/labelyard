// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { describe, expect, it } from "vitest";
import { axisValue, signedReading, snapReading, takeSign } from "./readings";

describe("snapReading", () => {
  it("rounds to 0.1 mm and says when it did", () => {
    expect(snapReading(0.43)).toEqual({ value: 0.4, snapped: true });
    expect(snapReading(0.45)).toEqual({ value: 0.5, snapped: true });
    expect(snapReading(1.96)).toEqual({ value: 2, snapped: true });
  });

  it("leaves a value on the 0.1 mm grid alone, floating point noise included", () => {
    expect(snapReading(0.4)).toEqual({ value: 0.4, snapped: false });
    expect(snapReading(0.1 + 0.2)).toEqual({ value: 0.3, snapped: false });
    expect(snapReading(0)).toEqual({ value: 0, snapped: false });
  });
});

describe("signedReading", () => {
  it("right and below are positive, left and above negative", () => {
    expect(signedReading(0.4, "right")).toBe(0.4);
    expect(signedReading(0.4, "left")).toBe(-0.4);
    expect(signedReading(0.7, "below")).toBe(0.7);
    expect(signedReading(0.7, "above")).toBe(-0.7);
  });

  it("takes the size of a signed number: the direction alone decides the sign", () => {
    expect(signedReading(-0.4, "right")).toBe(0.4);
    expect(signedReading(-0.4, "left")).toBe(-0.4);
  });

  it("zero has no sign", () => {
    expect(Object.is(signedReading(0, "left"), 0)).toBe(true);
  });
});

describe("takeSign", () => {
  it("a typed minus picks the negative direction and leaves the size", () => {
    expect(takeSign("-0,4", "across")).toEqual({ text: "0,4", direction: "left" });
    expect(takeSign("−0,4", "down")).toEqual({ text: "0,4", direction: "above" });
  });

  it("a typed plus picks the positive direction", () => {
    expect(takeSign("+0.4", "across")).toEqual({ text: "0.4", direction: "right" });
    expect(takeSign(" +0.4", "down")).toEqual({ text: "0.4", direction: "below" });
  });

  it("text without a sign keeps the direction that is set", () => {
    expect(takeSign("0.4", "across")).toEqual({ text: "0.4", direction: null });
  });
});

describe("axisValue", () => {
  it("parses comma and point, snaps, and signs by the direction", () => {
    expect(axisValue({ text: "0,43", direction: "left" })).toEqual({ value: -0.4, snapped: true });
    expect(axisValue({ text: "0.4", direction: "right" })).toEqual({ value: 0.4, snapped: false });
  });

  it("is null for an empty or unreadable field", () => {
    expect(axisValue({ text: "", direction: "right" })).toBeNull();
    expect(axisValue({ text: "abc", direction: "right" })).toBeNull();
  });
});
