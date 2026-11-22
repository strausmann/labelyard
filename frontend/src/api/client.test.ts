// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiFetch, apiFetchWithHeaders, fetchAllPages, nextLink, setCsrfToken, setOnUnauthorized } from "./client";

function respond(status: number, body?: unknown, contentType = "application/json"): Response {
  return new Response(body === undefined ? null : typeof body === "string" ? body : JSON.stringify(body), {
    status,
    headers: body === undefined ? {} : { "Content-Type": contentType },
  });
}

describe("apiFetch", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    setCsrfToken("test-csrf-token");
    setOnUnauthorized(null);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    setCsrfToken(null);
    setOnUnauthorized(null);
  });

  function lastInit(): RequestInit {
    return fetchMock.mock.calls.at(-1)![1] as RequestInit;
  }
  function lastHeaders(): Headers {
    return new Headers(lastInit().headers);
  }

  it("sends same-origin credentials and parses JSON", async () => {
    fetchMock.mockResolvedValue(respond(200, { ok: true }));
    await expect(apiFetch<{ ok: boolean }>("/api/v1/info")).resolves.toEqual({ ok: true });
    expect(fetchMock.mock.calls[0]![0]).toBe("/api/v1/info");
    expect(lastInit().credentials).toBe("same-origin");
  });

  it("sends csrf header on POST, not on GET", async () => {
    fetchMock.mockResolvedValue(respond(200, {}));
    await apiFetch("/api/v1/things");
    expect(lastHeaders().has("X-CSRF-Token")).toBe(false);

    await apiFetch("/api/v1/things", { method: "POST", json: { a: 1 } });
    expect(lastHeaders().get("X-CSRF-Token")).toBe("test-csrf-token");
    expect(lastHeaders().get("Content-Type")).toBe("application/json");
    expect(lastInit().body).toBe('{"a":1}');

    for (const method of ["PUT", "PATCH", "DELETE"]) {
      await apiFetch("/api/v1/things/1", { method });
      expect(lastHeaders().get("X-CSRF-Token"), method).toBe("test-csrf-token");
    }
  });

  it("omits the csrf header when no session token is known", async () => {
    setCsrfToken(null);
    fetchMock.mockResolvedValue(respond(200, {}));
    await apiFetch("/api/v1/things", { method: "POST" });
    expect(lastHeaders().has("X-CSRF-Token")).toBe(false);
  });

  it("returns undefined for 204", async () => {
    fetchMock.mockResolvedValue(respond(204));
    await expect(apiFetch("/api/v1/things/1", { method: "DELETE" })).resolves.toBeUndefined();
  });

  it("parses problem body into ApiError", async () => {
    fetchMock.mockResolvedValue(
      respond(409, { type: "tape-mismatch", title: "Tape Mismatch", status: 409, detail: "Loaded 12 mm, expected 24 mm" }, "application/problem+json"),
    );
    const err = await apiFetch("/api/v1/jobs").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 409, code: "tape-mismatch", detail: "Loaded 12 mm, expected 24 mm" });
  });

  it("parses the error_code / error_message detail shape", async () => {
    fetchMock.mockResolvedValue(respond(403, { detail: { error_code: "csrf_failed", error_message: "Missing header." } }));
    await expect(apiFetch("/api/v1/x", { method: "POST" })).rejects.toMatchObject({
      status: 403,
      code: "csrf_failed",
      detail: "Missing header.",
    });
  });

  it("reads the error envelope returned directly, not wrapped in `detail` (the label preview's 422)", async () => {
    fetchMock.mockResolvedValue(
      respond(422, { error_code: "qr_payload_too_long", error_message: "Too long.", error_detail: { length: 144, max_bytes: 122 } }),
    );
    await expect(apiFetch("/api/v1/render/preview", { method: "POST" })).rejects.toMatchObject({
      status: 422,
      code: "qr_payload_too_long",
      detail: "Too long.",
      errorDetail: { length: 144, max_bytes: 122 },
    });
  });

  it("reads a problem object that FastAPI wrapped in `detail` (the calibration page's printer-unavailable)", async () => {
    fetchMock.mockResolvedValue(
      respond(409, { detail: { type: "printer-unavailable", title: "Printer is not available to the print queue", status: 409, detail: "Printer x is not known" } }),
    );
    await expect(apiFetch("/api/v1/x", { method: "POST" })).rejects.toMatchObject({
      status: 409,
      code: "printer-unavailable",
      detail: "Printer x is not known",
    });
  });

  it("an about:blank problem inside `detail` has no code", async () => {
    fetchMock.mockResolvedValue(respond(409, { detail: { type: "about:blank", title: "Conflict", status: 409 } }));
    await expect(apiFetch("/api/v1/x", { method: "POST" })).rejects.toMatchObject({ status: 409, code: null, detail: "Conflict" });
  });

  it("falls back to a string detail, then to the status text", async () => {
    fetchMock.mockResolvedValueOnce(respond(404, { detail: "Not Found" }));
    await expect(apiFetch("/api/v1/x")).rejects.toMatchObject({ status: 404, code: null, detail: "Not Found" });

    fetchMock.mockResolvedValueOnce(respond(502, "<html>bad gateway</html>", "text/html"));
    await expect(apiFetch("/api/v1/x")).rejects.toMatchObject({ status: 502, code: null, detail: "HTTP 502" });
  });

  it("calls onUnauthorized on 401", async () => {
    const onUnauthorized = vi.fn();
    setOnUnauthorized(onUnauthorized);
    fetchMock.mockResolvedValue(respond(401, { detail: { error_code: "not_signed_in", error_message: "Sign in." } }));
    await expect(apiFetch("/api/v1/printers")).rejects.toMatchObject({ status: 401, code: "not_signed_in" });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("does not call onUnauthorized for other errors", async () => {
    const onUnauthorized = vi.fn();
    setOnUnauthorized(onUnauthorized);
    fetchMock.mockResolvedValue(respond(403, { detail: "no" }));
    await expect(apiFetch("/api/v1/printers")).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("network failure rejects with ApiError status 0", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const err = await apiFetch("/api/v1/info").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 0, code: "network_error" });
  });

  describe("retryAfterSeconds", () => {
    it("reads the Retry-After header of a refusal (#339)", async () => {
      fetchMock.mockResolvedValue(
        new Response(JSON.stringify({ detail: { error_code: "too-many-previews", error_message: "Busy.", retry_after_seconds: 9 } }), {
          status: 429,
          headers: { "Content-Type": "application/json", "Retry-After": "2" },
        }),
      );
      expect(await apiFetch("/x").catch((e: unknown) => e)).toMatchObject({ status: 429, code: "too-many-previews", retryAfterSeconds: 2 });
    });

    it("falls back to retry_after_seconds of the body, and is null without either", async () => {
      fetchMock.mockResolvedValueOnce(respond(429, { detail: { error_code: "too-many-previews", error_message: "Busy.", retry_after_seconds: 3 } }));
      expect(await apiFetch("/x").catch((e: unknown) => e)).toMatchObject({ retryAfterSeconds: 3 });
      fetchMock.mockResolvedValueOnce(respond(409, { detail: { error_code: "a", error_message: "b" } }));
      expect(await apiFetch("/x").catch((e: unknown) => e)).toMatchObject({ retryAfterSeconds: null });
    });

    it("ignores a Retry-After that is not a number of seconds", async () => {
      fetchMock.mockResolvedValue(
        new Response("{}", { status: 429, headers: { "Content-Type": "application/json", "Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT" } }),
      );
      expect(await apiFetch("/x").catch((e: unknown) => e)).toMatchObject({ retryAfterSeconds: null });
    });
  });

  describe("errorDetail", () => {
    it("keeps detail.error_detail from the envelope shape", async () => {
      fetchMock.mockResolvedValue(
        respond(409, { detail: { error_code: "insufficient_cells", error_message: "Short.", error_detail: { needed: 5, shortfall: 2 } } }),
      );
      const err = await apiFetch("/api/v1/sheets").catch((e: unknown) => e);
      expect(err).toMatchObject({ status: 409, code: "insufficient_cells", errorDetail: { needed: 5, shortfall: 2 } });
    });

    it("keeps extensions from the problem shape", async () => {
      fetchMock.mockResolvedValue(
        respond(503, { type: "printer-unavailable", title: "Printer unavailable", extensions: { printer: "p1" } }),
      );
      const err = await apiFetch("/api/v1/x").catch((e: unknown) => e);
      expect(err).toMatchObject({ code: "printer-unavailable", errorDetail: { printer: "p1" } });
    });

    it("a wrapped problem's extensions become the error detail", async () => {
      fetchMock.mockResolvedValue(
        respond(409, { detail: { type: "unsupported-tape", title: "Not this width.", status: 409, extensions: { tape_mm: 70 } } }),
      );
      const err = await apiFetch("/api/v1/printers/p1/test-print").catch((e: unknown) => e);
      expect(err).toMatchObject({ code: "unsupported-tape", errorDetail: { tape_mm: 70 } });
    });

    it("is null when the body carries no detail object", async () => {
      fetchMock.mockResolvedValueOnce(respond(404, { detail: "Not Found" }));
      expect(await apiFetch("/x").catch((e: unknown) => e)).toMatchObject({ errorDetail: null });
      fetchMock.mockResolvedValueOnce(respond(409, { detail: { error_code: "a", error_message: "b", error_detail: null } }));
      expect(await apiFetch("/x").catch((e: unknown) => e)).toMatchObject({ errorDetail: null });
      fetchMock.mockResolvedValueOnce(respond(409, { detail: { error_code: "a", error_message: "b", error_detail: ["no"] } }));
      expect(await apiFetch("/x").catch((e: unknown) => e)).toMatchObject({ errorDetail: null });
    });

    it("network and session errors have a null errorDetail", async () => {
      fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
      expect(await apiFetch("/x").catch((e: unknown) => e)).toMatchObject({ errorDetail: null });
    });
  });
});

describe("bounded lists (#204, D31)", () => {
  const page = (rows: number[], link?: string, extra: Record<string, string> = {}) =>
    new Response(JSON.stringify(rows), { status: 200, headers: { "Content-Type": "application/json", ...(link ? { Link: link } : {}), ...extra } });

  afterEach(() => vi.unstubAllGlobals());

  it("apiFetchWithHeaders returns the Link and X-Used-Up-Hidden headers", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => page([1], '</api/v1/sheets?limit=1&offset=1>; rel="next"', { "X-Used-Up-Hidden": "7" })));
    const { data, headers } = await apiFetchWithHeaders<number[]>("/api/v1/sheets?limit=1");
    expect(data).toEqual([1]);
    expect(headers.get("X-Used-Up-Hidden")).toBe("7");
    expect(nextLink(headers)).toBe("/api/v1/sheets?limit=1&offset=1");
  });

  it("apiFetchWithHeaders fails like apiFetch", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => respond(409, { detail: { error_code: "sheet_limit_reached", error_message: "x", error_detail: { cap: 2, scope: "hub" } } })));
    const error = await apiFetchWithHeaders("/api/v1/sheets", { method: "GET" }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("sheet_limit_reached");
    expect((error as ApiError).errorDetail).toEqual({ cap: 2, scope: "hub" });
  });

  it("nextLink reads only rel=next", () => {
    expect(nextLink(new Headers())).toBeNull();
    expect(nextLink(new Headers({ Link: '</a>; rel="prev"' }))).toBeNull();
    expect(nextLink(new Headers({ Link: '</a>; rel="prev", </b>; rel="next"' }))).toBe("/b");
  });

  it("fetchAllPages follows Link to the last page", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const offset = new URL(String(input), "http://localhost").searchParams.get("offset") ?? "0";
      if (offset === "0") return page([1, 2], "</api/v1/sheets?state=active&limit=2&offset=2>; rel=\"next\"");
      if (offset === "2") return page([3, 4], "</api/v1/sheets?state=active&limit=2&offset=4>; rel=\"next\"");
      return page([5]);
    });
    vi.stubGlobal("fetch", fetchMock);
    expect(await fetchAllPages<number>("/api/v1/sheets?state=active", 20)).toEqual([1, 2, 3, 4, 5]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("fetchAllPages stops at the page bound and never leaves the API", async () => {
    const endless = vi.fn(async () => page([1], '</api/v1/sheets?offset=1>; rel="next"'));
    vi.stubGlobal("fetch", endless);
    expect(await fetchAllPages<number>("/api/v1/sheets", 3)).toEqual([1, 1, 1]);
    expect(endless).toHaveBeenCalledTimes(3);
    const away = vi.fn(async () => page([1], '<https://evil.example.invalid/x>; rel="next"'));
    vi.stubGlobal("fetch", away);
    expect(await fetchAllPages<number>("/api/v1/sheets", 3)).toEqual([1]);
    expect(away).toHaveBeenCalledTimes(1);
  });
});
