// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { describe, expect, it } from "vitest";
import { httpUrl, linkHost, parseDecimal } from "./search";

describe("httpUrl", () => {
  it("httpUrl returns the normalised href", () => {
    expect(httpUrl("HTTPS://Example.invalid/a b")).toBe("https://example.invalid/a%20b");
  });
  it.each(["javascript:alert(1)", "data:text/html,x", "", null, undefined, "not a url"])("refuses %s", (value) => {
    expect(httpUrl(value)).toBeNull();
  });
});

describe("linkHost (#204)", () => {
  it("is the host as the browser reads it, punycode and port included", () => {
    expect(linkHost("https://example.invalid/a")).toBe("example.invalid");
    expect(linkHost("http://example.invalid:8080/a")).toBe("example.invalid:8080");
    expect(linkHost("https://bücher.example.invalid/")).toBe("xn--bcher-kva.example.invalid");
  });

  it("is null for what httpUrl does not link", () => {
    expect(linkHost("javascript:alert(1)")).toBeNull();
    expect(linkHost("https://user:secret@example.invalid/")).toBeNull();
    expect(linkHost("not a url")).toBeNull();
    expect(linkHost(null)).toBeNull();
  });
});

describe("parseDecimal from the archive search", () => {
  it("the zone editor and the size search take no exponent or hex number (shared/decimal.ts)", () => {
    expect(parseDecimal("1e1")).toBeNull();
    expect(parseDecimal("0x10")).toBeNull();
    expect(parseDecimal("12,5")).toBe(12.5);
  });
});
