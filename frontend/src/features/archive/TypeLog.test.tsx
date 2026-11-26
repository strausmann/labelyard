// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../../api/schema.gen";
import { renderApp, SIGNED_IN, widenAppWaits } from "../../test-utils";
import { mockApi, SHEET_TYPE } from "../test-api";

vi.setConfig({ testTimeout: 15_000 });
widenAppWaits();
afterEach(() => vi.unstubAllGlobals());

type LogEntry = components["schemas"]["SheetTypeLogRead"];

const READ_SESSION = { ...SIGNED_IN, scope: "read" as const };
const PRINTER = { id: "11111111-1111-1111-1111-111111111111", name: "Office laser" };

const entry = (over: Partial<LogEntry>): LogEntry => ({
  id: "e0",
  kind: "note",
  type_version: 1,
  printer: null,
  tray: null,
  tray_label: null,
  run: null,
  correction: null,
  readings: null,
  verdict: "none",
  note: null,
  actor: "admin",
  created_at: "2026-11-20T18:40:00Z",
  ...over,
});

const CHECK = entry({
  id: "e1",
  kind: "check_print",
  printer: PRINTER,
  tray: "tray-1",
  tray_label: "Tray 1 (manual feed)",
  run: { id: "r13", run_no: 13, kind: "check_page", pass_no: 2 },
  correction: { id: "h3", seq: 3, offset_x_mm: 0.38, offset_y_mm: -0.62 },
  readings: [
    { col_gap: 1, row_gap: 1, across_mm: 0.2, down_mm: -0.3, residual_mm: 0.01 },
    { col_gap: 4, row_gap: 1, across_mm: 0.6, down_mm: -0.3, residual_mm: 0.02 },
    { col_gap: 4, row_gap: 6, across_mm: 0.8, down_mm: 0, residual_mm: 0.01 },
  ],
  verdict: "sheet_geometry",
  note: "Across grows evenly from column 1 to 4: that is the pitch, not the printer.",
});
const NEW_VERSION = entry({ id: "e2", kind: "new_version", type_version: 2, note: "Pitch across 48.20 → 47.55 mm." });
const VERIFIED = entry({ id: "e3", kind: "verified", type_version: 2 });
const FITS = entry({ id: "e4", kind: "check_print", verdict: "fits", printer: PRINTER, tray: "tray-1", type_version: 2 });
const API_NOTE = entry({ id: "e5", kind: "note", verdict: "feed_variance", note: "Sheets feed 0.3 mm differently.", actor: "key:n8n" });
const ENTRIES = [CHECK, NEW_VERSION, FITS, VERIFIED, API_NOTE];

const routes = (extra: Record<string, unknown> = {}) => ({
  "GET /api/v1/sheet-types": [SHEET_TYPE],
  "GET /api/v1/sheet-types/facets": { vendors: [], page_sizes: [] },
  "GET /api/v1/sheet-types/L4736REV": { ...SHEET_TYPE, versions: [] },
  "GET /api/v1/sheet-types/L4736REV/log": ENTRIES,
  "GET /api/v1/printers": [{ ...PRINTER, slug: "office", backend: "ipp", enabled: true }],
  [`GET /api/v1/printers/${PRINTER.id}/capabilities`]: { trays: [{ key: "tray-1", label: "Tray 1 (manual feed)", is_default: true }] },
  ...extra,
});

async function openLog(extra: Record<string, unknown> = {}, session = SIGNED_IN) {
  const api = mockApi(routes(extra));
  await renderApp({ path: "/sheets/archive?type=L4736REV", session });
  await userEvent.click(await screen.findByRole("tab", { name: "Log" }));
  const list = await screen.findByRole("list", { name: "Log" });
  return { ...api, list };
}

describe("sheet-type log (#351, mockup M12)", () => {
  it("log lists verdicts", async () => {
    const { list } = await openLog();
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(5);
    const first = items[0]!;
    expect(within(first).getByText("Check print")).toBeVisible();
    expect(within(first).getByText("Sheet geometry")).toBeVisible();
    expect(within(first).getByText("Office laser · Tray 1 (manual feed) · print #13 · pass 2 · version 1")).toBeVisible();
    expect(within(first).getByText(/history entry 3 \(dx \+0\.38, dy −0\.62 mm\)/)).toBeVisible();
    const readings = within(first).getByRole("table", { name: "Readings" });
    expect(within(readings).getByText("S4 · Z6")).toBeVisible();
    expect(within(readings).getByText("+0.80 mm")).toBeVisible();
    expect(within(items[1]!).getByText("New version 2")).toBeVisible();
    expect(within(items[2]!).getByText("Fits")).toBeVisible();
    expect(within(items[3]!).getByText("Marked as verified")).toBeVisible();
    // No verdict, no badge.
    expect(within(items[3]!).queryByText("No verdict")).toBeNull();
    expect(within(items[4]!).getByText("Feed variance")).toBeVisible();
    expect(screen.getByText("5 of 5 entries. The newest 500 per type are kept.")).toBeVisible();
    expect(screen.getByRole("note")).toHaveTextContent("Internal only");
  });

  it("filters by printer and kind through the API", async () => {
    const { calls } = await openLog();
    await userEvent.click(screen.getByRole("button", { name: /Printer$/ }));
    await userEvent.click(await screen.findByRole("option", { name: "Office laser" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/log") && c.search.includes(`printer=${PRINTER.id}`))).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: /Kind$/ }));
    await userEvent.click(await screen.findByRole("option", { name: "Check prints" }));
    await waitFor(() => expect(calls.some((c) => c.search.includes("kind=check_print") && c.search.includes(`printer=${PRINTER.id}`))).toBe(true));
  });

  it("hand entry dialog", async () => {
    const { calls } = await openLog({ "POST /api/v1/sheet-types/L4736REV/log": entry({ id: "new" }) });
    await userEvent.click(screen.getByRole("button", { name: "Add a note" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a note" });
    await userEvent.click(within(dialog).getByRole("button", { name: /Printer$/ }));
    await userEvent.click(await screen.findByRole("option", { name: "Office laser" }));
    await userEvent.click(within(dialog).getByRole("button", { name: /Tray$/ }));
    await userEvent.click(await screen.findByRole("option", { name: "Tray 1 (manual feed)" }));
    await userEvent.click(within(dialog).getByRole("button", { name: /Verdict$/ }));
    await userEvent.click(await screen.findByRole("option", { name: "Feed variance" }));
    const note = within(dialog).getByRole("textbox", { name: "Note" });
    await userEvent.type(note, "Feeds differently");
    expect(within(dialog).getByText("17 of 2000 characters")).toBeVisible();
    await userEvent.click(within(dialog).getByRole("button", { name: "Add note" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add a note" })).toBeNull());
    const post = calls.find((c) => c.method === "POST" && c.path.endsWith("/log"))!;
    expect(JSON.parse(post.body!)).toEqual({ verdict: "feed_variance", note: "Feeds differently", printer_id: PRINTER.id, tray: "tray-1" });
  });

  it("a note longer than 2000 characters cannot be sent", async () => {
    await openLog();
    await userEvent.click(screen.getByRole("button", { name: "Add a note" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a note" });
    const note = within(dialog).getByRole("textbox", { name: "Note" });
    await userEvent.click(note);
    await userEvent.paste("x".repeat(2001));
    expect(within(dialog).getByText("2001 of 2000 characters")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Add note" })).toBeDisabled();
  });

  it("an admin sets the verdict and note of an entry", async () => {
    const { calls } = await openLog({ "PATCH /api/v1/sheet-types/L4736REV/log/e3": { ...VERIFIED, verdict: "fits" } });
    const items = within(screen.getByRole("list", { name: "Log" })).getAllByRole("listitem");
    await userEvent.click(within(items[3]!).getByRole("button", { name: "Add verdict and note" }));
    const dialog = await screen.findByRole("dialog", { name: "Verdict and note" });
    // Only the verdict and the note: no printer or tray to choose.
    expect(within(dialog).queryByRole("button", { name: /Printer$/ })).toBeNull();
    await userEvent.click(within(dialog).getByRole("button", { name: /Verdict$/ }));
    await userEvent.click(await screen.findByRole("option", { name: "Fits" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(JSON.parse(patch.body!)).toEqual({ verdict: "fits", note: null });
  });

  it("a reader sees the log without the admin actions", async () => {
    await openLog({}, READ_SESSION);
    expect(screen.queryByRole("button", { name: "Add a note" })).toBeNull();
    expect(screen.queryByRole("button", { name: /verdict and note/ })).toBeNull();
  });
});
