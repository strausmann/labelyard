// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderApp, SIGNED_IN, widenAppWaits } from "../../test-utils";
import { GEOMETRY_L4736, mockApi, PAGE_CAPABILITIES, PAGE_PRINTER, pageStatus, PRINTER_A, respond, SHEET_TYPE, type Call } from "../test-api";

// The probe print on plain paper (#241, design 7.3, mockup M9), opened from the type detail. Whole-app renders.
vi.setConfig({ testTimeout: 15_000 });
widenAppWaits();
afterEach(() => vi.unstubAllGlobals());

const P = PAGE_PRINTER.id;
const SECOND = { ...PAGE_PRINTER, id: "44444444-4444-4444-8444-444444444444", name: "Archive laser", slug: "archive-laser" };
const JOB = "dddddddd-0000-4000-8000-000000000031";
const DETAIL = {
  ...SHEET_TYPE,
  versions: [{ version: 1, change_note: null, created_at: "2026-11-01T08:00:00Z", geometry: GEOMETRY_L4736, geometry_hash: "abc", sheets: 1 }],
};
const calibration = (printerId: string) => `/api/v1/printers/${printerId}/calibration`;
const CORRECTED = { tray: "main", offset_x_mm: 0.41, offset_y_mm: -0.66, scale_x: 1, scale_y: 1, rotation_deg: 0, method: "manual", updated_at: "2026-11-21T14:00:00Z" };
const NONE = { tray: "bypass", offset_x_mm: null, offset_y_mm: null, scale_x: null, scale_y: null, rotation_deg: null, method: null, updated_at: null };

function routes(extra: Record<string, unknown> = {}) {
  return {
    "GET /api/v1/sheet-types": [SHEET_TYPE],
    "GET /api/v1/sheet-types/facets": { vendors: [], page_sizes: [] },
    "GET /api/v1/sheet-types/L4736REV": DETAIL,
    "GET /api/v1/printers": [PAGE_PRINTER, SECOND, PRINTER_A],
    [`GET /api/v1/printers/${P}/status`]: pageStatus(P),
    [`GET /api/v1/printers/${SECOND.id}/status`]: pageStatus(SECOND.id),
    [`GET /api/v1/printers/${P}/capabilities`]: PAGE_CAPABILITIES,
    [`GET /api/v1/printers/${SECOND.id}/capabilities`]: PAGE_CAPABILITIES,
    [`GET ${calibration(P)}`]: [CORRECTED, NONE],
    [`GET ${calibration(SECOND.id)}`]: [{ ...CORRECTED }, NONE],
    [`POST ${calibration(P)}/main/probe`]: respond(202, { job_id: JOB, run_no: 15 }),
    [`POST ${calibration(SECOND.id)}/bypass/probe`]: respond(202, { job_id: JOB, run_no: 16 }),
    ...extra,
  };
}

async function openDialog(extra: Record<string, unknown> = {}, button = "Probe print on plain paper", session = SIGNED_IN) {
  const api = mockApi(routes(extra));
  await renderApp({ path: "/sheets/archive?type=L4736REV", session });
  await userEvent.click(await screen.findByRole("button", { name: button }));
  const dialog = await screen.findByRole("dialog", { name: "Probe print on plain paper" });
  return { ...api, dialog: within(dialog) };
}

const posts = (calls: Call[]) => calls.filter((c) => c.method === "POST" && c.path.endsWith("/probe"));

async function choose(d: ReturnType<typeof within>, field: RegExp, option: string) {
  await userEvent.click(d.getByRole("button", { name: field }));
  await userEvent.click(await screen.findByRole("option", { name: option }));
}

describe("the probe print dialog (#241, M9)", () => {
  it("dialog picks printer and tray and prints", async () => {
    const { calls, dialog: d } = await openDialog();
    // The tray's correction is named before the print: it is applied to the probe page.
    expect(await d.findByText("The current correction of this tray is applied: dx +0.41 mm, dy −0.66 mm.")).toBeVisible();
    await choose(d, /Printer/, "Archive laser");
    await choose(d, /Tray/, "Bypass");
    expect(await d.findByText("This tray has no correction stored: the page is printed as it comes.")).toBeVisible();
    await userEvent.click(d.getByRole("button", { name: "Print probe page" }));
    await waitFor(() => expect(posts(calls)).toHaveLength(1));
    expect(posts(calls)[0]!.path).toBe(`${calibration(SECOND.id)}/bypass/probe`);
    expect(JSON.parse(posts(calls)[0]!.body!)).toEqual({ type: "L4736REV", version: 1, grid: false });
    // Queued: the dialog closes, the notification stack follows the job.
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Probe print on plain paper" })).toBeNull());
  });

  it("waits for the trays' labels before it picks the default tray (Codex on #453)", async () => {
    // The hub lists calibration rows by tray key, so `bypass` comes before the default `main`; the capabilities that
    // name the default answer last.
    const late = () => new Promise((resolve) => setTimeout(() => resolve(PAGE_CAPABILITIES), 300));
    const { calls, dialog: d } = await openDialog({ [`GET ${calibration(P)}`]: [NONE, CORRECTED], [`GET /api/v1/printers/${P}/capabilities`]: late });
    // While the trays load, Print waits; then the default tray (with its correction) is the one chosen.
    expect(d.getByRole("button", { name: "Print probe page" })).toBeDisabled();
    expect(await d.findByText("The current correction of this tray is applied: dx +0.41 mm, dy −0.66 mm.")).toBeVisible();
    await userEvent.click(d.getByRole("button", { name: "Print probe page" }));
    await waitFor(() => expect(posts(calls)).toHaveLength(1));
    expect(posts(calls)[0]!.path).toBe(`${calibration(P)}/main/probe`);
  });

  it("measurement sheet option sends grid true", async () => {
    const { calls, dialog: d } = await openDialog();
    const grid = d.getByRole("checkbox", { name: "Measurement sheet: add a millimetre grid, no correction" });
    expect(grid).not.toBeChecked();
    await userEvent.click(grid);
    // The grid is drawn without correction, whatever the tray holds.
    expect(await d.findByText(/The grid is drawn without correction, so its millimetres are the page's own\./)).toBeVisible();
    await userEvent.click(d.getByRole("button", { name: "Print probe page" }));
    await waitFor(() => expect(posts(calls)).toHaveLength(1));
    expect(JSON.parse(posts(calls)[0]!.body!)).toEqual({ type: "L4736REV", version: 1, grid: true });
  });

  it("the Measurement sheet action opens the dialog with the grid chosen", async () => {
    const { dialog: d } = await openDialog({}, "Measurement sheet");
    expect(d.getByRole("checkbox", { name: "Measurement sheet: add a millimetre grid, no correction" })).toBeChecked();
  });

  it("offline refusal shown in words", async () => {
    const offline = respond(409, {
      detail: { type: "printer-offline", title: "The printer is offline", status: 409, detail: `The last status of printer ${P} says it is offline` },
    });
    const { dialog: d } = await openDialog({ [`POST ${calibration(P)}/main/probe`]: offline });
    await userEvent.click(d.getByRole("button", { name: "Print probe page" }));
    const alert = await d.findByRole("alert");
    expect(alert).toHaveTextContent("Office laser is offline");
    expect(alert).toHaveTextContent("Switch it on, or choose another printer.");
    expect(alert).not.toHaveTextContent("printer-offline");
    // The dialog stays open: the person can choose another printer.
    expect(d.getByRole("button", { name: "Print probe page" })).toBeVisible();
  });

  it("a fresh offline status says so before the print and holds the button", async () => {
    const { dialog: d } = await openDialog({ [`GET /api/v1/printers/${P}/status`]: pageStatus(P, { online: false, stale: false }) });
    expect(await d.findByText("Office laser is offline")).toBeVisible();
    expect(d.getByRole("button", { name: "Print probe page" })).toBeDisabled();
  });

  // A stale status never holds the print (design 7.3): a printer that is back must not wait for the next poll.
  it("a stale offline status does not hold the button", async () => {
    const { dialog: d } = await openDialog({ [`GET /api/v1/printers/${P}/status`]: pageStatus(P, { online: false, stale: true }) });
    await d.findByText("The current correction of this tray is applied: dx +0.41 mm, dy −0.66 mm.");
    expect(d.queryByText("Office laser is offline")).toBeNull();
    expect(d.getByRole("button", { name: "Print probe page" })).toBeEnabled();
  });

  it.each([
    ["disabled", 409, { detail: { error_code: "printer_disabled", error_message: "Printer is disabled" } }, "This printer is switched off."],
    ["unknown printer", 404, { detail: { error_code: "printer_not_found", error_message: "Printer not found" } }, "That printer does not exist."],
    ["read key", 403, { detail: { error_code: "insufficient_scope", error_message: "Key has scope 'read' but 'print' is required" } }, "Printing and marking cells need a print or admin key."],
    ["restricted key", 403, { detail: { error_code: "printer_not_allowed", error_message: "Key may not use this printer" } }, "This key may not use this printer."],
    [
      "queue unavailable",
      409,
      { detail: { type: "printer-unavailable", title: "Printer is not available to the print queue", status: 409, detail: "not known" } },
      "The print queue does not know this printer yet. Try again in a moment.",
    ],
    [
      "device swapped",
      409,
      { detail: { type: "printer-identity-mismatch", title: "Printer identity mismatch", status: 409, detail: "held" } },
      "This printer may be a different device now. An admin has to confirm it on the printer's page first.",
    ],
  ])("refusal %s shown in words", async (_name, code, body, words) => {
    const { dialog: d } = await openDialog({ [`POST ${calibration(P)}/main/probe`]: respond(code, body) });
    await userEvent.click(d.getByRole("button", { name: "Print probe page" }));
    expect(await d.findByRole("alert")).toHaveTextContent(words);
  });

  it("download link uses the pdf route", async () => {
    const { dialog: d } = await openDialog();
    const link = await d.findByRole("link", { name: "Download PDF" });
    expect(link).toHaveAttribute("href", `${calibration(P)}/main/probe/pdf?type=L4736REV&version=1`);
    expect(link).toHaveAttribute("download");
    await userEvent.click(d.getByRole("checkbox", { name: "Measurement sheet: add a millimetre grid, no correction" }));
    expect(link).toHaveAttribute("href", `${calibration(P)}/main/probe/pdf?type=L4736REV&version=1&grid=true`);
  });

  it("the download is fetched first, so a slow page is said in words", async () => {
    const timeout = respond(504, { detail: { type: "render-timeout", title: "Render timeout", status: 504, detail: "too slow" } });
    const { calls, dialog: d } = await openDialog({ [`GET ${calibration(P)}/main/probe/pdf`]: timeout });
    await userEvent.click(await d.findByRole("link", { name: "Download PDF" }));
    expect(await d.findByRole("alert")).toHaveTextContent("The page took too long to draw. Try again later.");
    expect(calls.some((c) => c.method === "GET" && c.path === `${calibration(P)}/main/probe/pdf` && c.search === "?type=L4736REV&version=1")).toBe(true);
  });

  it("a drawn page is handed to the browser as a file", async () => {
    const create = vi.fn(() => "blob:probe");
    const revoke = vi.fn();
    // jsdom has no object URLs: the two functions are put in for this test and taken out again.
    const saved = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
    URL.createObjectURL = create;
    URL.revokeObjectURL = revoke;
    try {
      const pdf = () => new Response(new Uint8Array([37, 80, 68, 70]), { status: 200, headers: { "Content-Type": "application/pdf" } });
      // The file goes to the browser through a click on a temporary link; jsdom cannot follow it, so the click is caught.
      const saved: string[] = [];
      const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
        saved.push(`${this.href} ${this.download}`);
      });
      const { dialog: d } = await openDialog({ [`GET ${calibration(P)}/main/probe/pdf`]: pdf });
      await userEvent.click(await d.findByRole("link", { name: "Download PDF" }));
      await waitFor(() => expect(revoke).toHaveBeenCalledWith("blob:probe"));
      // The same name as the server's Content-Disposition, so a middle click and a plain click save the same file.
      expect(saved).toEqual(["blob:probe probe-L4736REV-v1.pdf"]);
      expect(create).toHaveBeenCalledTimes(1);
      expect(d.queryByRole("alert")).toBeNull();
      click.mockRestore();
    } finally {
      URL.createObjectURL = saved.create;
      URL.revokeObjectURL = saved.revoke;
    }
  });

  it("the measurement sheet downloads under the server's name, the key made safe (parentheses become _)", async () => {
    const create = vi.fn(() => "blob:grid");
    const keep = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
    URL.createObjectURL = create;
    URL.revokeObjectURL = vi.fn();
    const saved: string[] = [];
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      saved.push(this.download);
    });
    try {
      const odd = { ...DETAIL, key: "L4736(REV)", current_version: 3 };
      const pdf = () => new Response(new Uint8Array([37, 80, 68, 70]), { status: 200, headers: { "Content-Type": "application/pdf" } });
      mockApi(routes({
        "GET /api/v1/sheet-types": [odd],
        "GET /api/v1/sheet-types/L4736(REV)": odd,
        [`GET ${calibration(P)}/main/probe/pdf`]: pdf,
      }));
      await renderApp({ path: "/sheets/archive?type=L4736(REV)" });
      await userEvent.click(await screen.findByRole("button", { name: "Measurement sheet" }));
      const d = within(await screen.findByRole("dialog", { name: "Probe print on plain paper" }));
      await userEvent.click(await d.findByRole("link", { name: "Download PDF" }));
      await waitFor(() => expect(saved).toEqual(["measurement-sheet-L4736_REV_-v3.pdf"]));
    } finally {
      click.mockRestore();
      URL.createObjectURL = keep.create;
      URL.revokeObjectURL = keep.revoke;
    }
  });

  it("a download in progress says so", async () => {
    let answer: (r: Response) => void = () => {};
    const slow = () => new Promise<Response>((resolve) => (answer = resolve));
    const { dialog: d } = await openDialog({ [`GET ${calibration(P)}/main/probe/pdf`]: slow });
    await userEvent.click(await d.findByRole("link", { name: "Download PDF" }));
    expect(await d.findByRole("link", { name: "Downloading…" })).toHaveAttribute("aria-disabled", "true");
    answer(new Response(JSON.stringify({ detail: { type: "render-timeout", title: "t", status: 504, detail: "x" } }), { status: 504, headers: { "Content-Type": "application/json" } }));
    expect(await d.findByRole("link", { name: "Download PDF" })).toBeVisible();
  });

  it("the measurement sheet says to print it on a real sheet and photograph it", async () => {
    const { dialog: d } = await openDialog({}, "Measurement sheet");
    expect(await d.findByText("Print it on a real sheet of this type and photograph it to read where the vendor's preprint sits. The grid is drawn without correction, so its millimetres are the page's own.")).toBeVisible();
    // The intro speaks of the archive, so it does not contradict printing on a real sheet.
    expect(d.getByText("Prints the outline of every label so you can hold the page against a real sheet. The archive uses no sheet and reserves no cell.")).toBeVisible();
  });

  it("the offline note is announced when it holds the button", async () => {
    const { dialog: d } = await openDialog({ [`GET /api/v1/printers/${SECOND.id}/status`]: pageStatus(SECOND.id, { online: false, stale: false }) });
    await d.findByText("The current correction of this tray is applied: dx +0.41 mm, dy −0.66 mm.");
    await choose(d, /Printer/, "Archive laser");
    const note = await d.findByText("Archive laser is offline");
    expect(note.closest("[aria-live='polite']")).not.toBeNull();
    expect(d.getByRole("button", { name: "Print probe page" })).toBeDisabled();
  });

  it("trays that cannot be loaded are said as an error, not as a printer without trays", async () => {
    const { dialog: d } = await openDialog({ [`GET ${calibration(P)}`]: respond(500, { detail: "boom" }) });
    expect(await d.findByRole("alert")).toHaveTextContent("The trays of this printer could not be loaded.");
    expect(d.queryByText("This printer has no tray to print from.")).toBeNull();
    expect(d.getByRole("button", { name: "Print probe page" })).toBeDisabled();
  });

  it("the swap note on the measurement sheet gives a reason without the correction", async () => {
    const { dialog: d } = await openDialog({ [`GET /api/v1/printers/${P}/status`]: pageStatus(P, { identity_mismatch: true }) }, "Measurement sheet");
    expect(await d.findByText("Held while the device is swapped")).toBeVisible();
    expect(d.getByText(/the hub prints no page for a device nobody has confirmed/)).toBeVisible();
    expect(d.queryByText(/stored correction/)).toBeNull();
  });

  it("a read key can download but not print", async () => {
    const { calls, dialog: d } = await openDialog({}, "Probe print on plain paper", { ...SIGNED_IN, scope: "read" as const });
    expect(await d.findByRole("link", { name: "Download PDF" })).toBeVisible();
    expect(d.getByRole("button", { name: "Print probe page" })).toBeDisabled();
    expect(d.getByText("Printing needs a print or admin key.")).toBeVisible();
    expect(posts(calls)).toHaveLength(0);
  });

  it("a device swap is said before the print and holds the button", async () => {
    const { dialog: d } = await openDialog({ [`GET /api/v1/printers/${P}/status`]: pageStatus(P, { identity_mismatch: true }) });
    expect(await d.findByText("Held while the device is swapped")).toBeVisible();
    expect(d.getByRole("button", { name: "Print probe page" })).toBeDisabled();
    // Downloading stays possible: the PDF is not a job.
    expect(d.getByRole("link", { name: "Download PDF" })).toBeVisible();
  });
});
