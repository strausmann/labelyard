// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { describe, expect, it } from "vitest";
import { webUrlHost, webUrlProblem } from "./webUrl";

describe("webUrlProblem (the server's rules, shown before the save, #247)", () => {
  it.each([
    "",
    "   ",
    "https://assets.example.com",
    "http://assets.example.com:8080/snipe/",
    "https://assets.example.com///",
  ])("accepts %j", (value) => expect(webUrlProblem(value)).toBeNull());

  it.each([
    ["https://user:secret@grocy.example.invalid", "credentials"],
    ["https://user@grocy.example.invalid", "credentials"],
    ["ftp://assets.example.com", "scheme"],
    ["assets.example.com", "scheme"],
    ["https://", "host"],
    ["https:///path", "host"],
    ["https://:8080", "host"],
    ["https://@assets.example.com", "credentials"],
    ["https://:@assets.example.com", "credentials"],
    ["https://assets.example.com@evil.example.com", "credentials"],
    ["https:/assets.example.com", "scheme"],
    ["https:assets.example.com", "scheme"],
    ["javascript:alert(1)", "scheme"],
    ["https://assets.example.com/?a=1", "query"],
    ["https://assets.example.com/#top", "query"],
    ["https://assets .example.com", "invalid"],
    [`https://assets.example.com/${"a".repeat(2048)}`, "length"],
  ])("refuses %j as %s", (value, problem) => expect(webUrlProblem(value)).toBe(problem));
});

describe("webUrlHost", () => {
  it("is the host of a valid address and nothing otherwise", () => {
    expect(webUrlHost("https://assets.example.com:8443/snipe")).toBe("assets.example.com:8443");
    expect(webUrlHost("https://user:x@assets.example.com")).toBeNull();
    expect(webUrlHost("")).toBeNull();
  });
});
