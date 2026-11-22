// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

/** The server refuses a longer public web address (`integrations.<name>.web_url`, #247). */
export const MAX_WEB_URL_LENGTH = 2048;

export type WebUrlProblem = "scheme" | "credentials" | "query" | "length" | "invalid";

/**
 * Why a public web address would be refused on save, or `null`. The same rules as the server's settings validator
 * (http or https, a host, no user info, no query or fragment, no whitespace, at most 2048 characters), applied while
 * typing so the refusal is read before the Save button is pressed. The server stays the authority.
 */
export function webUrlProblem(raw: string): WebUrlProblem | null {
  const text = raw.trim();
  if (text === "") return null;
  if (text.length > MAX_WEB_URL_LENGTH) return "length";
  if (/\s/.test(text)) return "invalid";
  let parsed: URL;
  try {
    parsed = new URL(text);
  } catch {
    // "https://" alone has no host; any other unparsable http(s) text is simply not an address.
    if (/^https?:\/\/(\/.*)?$/i.test(text)) return "scheme";
    return /^https?:\/\//i.test(text) ? "invalid" : "scheme";
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "scheme";
  if (!parsed.hostname) return "scheme";
  if (parsed.username !== "" || parsed.password !== "") return "credentials";
  if (text.includes("?") || text.includes("#")) return "query";
  return null;
}

/** The host (with a port, if any) of a usable address, for the line that says where links point. */
export function webUrlHost(raw: string): string | null {
  const text = raw.trim();
  if (text === "" || webUrlProblem(text) !== null) return null;
  return new URL(text).host;
}
