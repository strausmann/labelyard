// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

/** The server refuses a longer public web address (`integrations.<name>.web_url`, #247). */
export const MAX_WEB_URL_LENGTH = 2048;

export type WebUrlProblem = "scheme" | "host" | "credentials" | "query" | "length" | "invalid";

/**
 * Why a public web address would be refused on save, or `null`. The same rules as the server's settings validator
 * (http or https, a host, no user info, no query or fragment, no whitespace, at most 2048 characters), applied while
 * typing so the refusal is read before the Save button is pressed. The server stays the authority.
 *
 * The text is read, not only the parsed URL: the URL parser forgives `https:/host` and `https:host` and reports an
 * empty user name (`https://@host`) as none, so each of them is checked on the text.
 */
export function webUrlProblem(raw: string): WebUrlProblem | null {
  const text = raw.trim();
  if (text === "") return null;
  if (text.length > MAX_WEB_URL_LENGTH) return "length";
  if (/\s/.test(text)) return "invalid";
  if (!/^https?:\/\//i.test(text)) return "scheme";
  // The authority runs from after "//" to the first "/", "?" or "#"; an "@" in it is user info, however empty.
  const authority = text.slice(text.indexOf("//") + 2).split(/[/?#]/, 1)[0]!;
  if (authority.includes("@")) return "credentials";
  if (text.includes("?") || text.includes("#")) return "query";
  // The parser reads `https:///path` as host `path`: an empty authority is checked on the text.
  if (authority === "" || authority.startsWith(":")) return "host";
  let parsed: URL;
  try {
    parsed = new URL(text);
  } catch {
    return "invalid";
  }
  if (parsed.hostname === "") return "host";
  return null;
}

/** The host (with a port, if any) of a usable address, for the line that says where links point. */
export function webUrlHost(raw: string): string | null {
  const text = raw.trim();
  if (text === "" || webUrlProblem(text) !== null) return null;
  return new URL(text).host;
}
