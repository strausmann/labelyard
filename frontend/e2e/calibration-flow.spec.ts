// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import type { Page } from "@playwright/test";
import { MAIN } from "./env";
import { expect, mainKey, resetPagePrinter, setPagePrinter, signInWith, test } from "./fixtures";

// The whole calibration with a check sheet (#240, design 7.2): calibration page, readings, fit and save, then the check
// page as pass 1 of a sheet and pass 2 on the same sheet, whose readings are stored against its own run.

const TYPE = "avery-zweckform-l4736rev"; // 4 x 12 labels: 3 x 11 crossings, two check passes
const PRINTER = "Office page printer";
const TRAY = "main-tray";

type Run = { id: string; run_no: number; kind: string; job_id: string; check_sheet_id: string | null; pass_no: number | null; retry: boolean };

async function printerId(page: Page): Promise<string> {
  const printers = (await (await page.request.get("/api/v1/printers")).json()) as Array<{ id: string; name: string }>;
  return printers.find((p) => p.name === PRINTER)!.id;
}

async function runs(page: Page, id: string): Promise<Run[]> {
  return ((await (await page.request.get(`/api/v1/printers/${id}/calibration/${TRAY}/runs`)).json()) as Run[]).filter((r) => !r.retry);
}

/** A write through the API with the session's CSRF token, as the page sends it. */
async function send(page: Page, method: string, path: string, body: unknown): Promise<number> {
  return page.evaluate(
    async ([m, p, b]) => {
      const session = await (await fetch("/api/v1/auth/session")).json();
      const response = await fetch(p as string, {
        method: m as string,
        headers: { "X-CSRF-Token": session.csrf_token, ...(b ? { "Content-Type": "application/json" } : {}) },
        body: b ? JSON.stringify(b) : undefined,
      });
      return response.status;
    },
    [method, path, body] as const,
  );
}

async function chooseType(page: Page) {
  const box = page.getByRole("combobox", { name: /Sheet type|Bogentyp/ });
  await box.click();
  await expect(page.getByRole("listbox")).toBeVisible();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.type(TYPE);
  await expect(box).toHaveValue(TYPE);
  await page.getByRole("option", { name: new RegExp(`\\(${TYPE}\\)`) }).click();
}

async function showTab(page: Page, name: string) {
  const tab = page.getByRole("tab", { name });
  await tab.click();
  await expect(tab).toHaveAttribute("aria-selected", "true");
}

/** Waits until the tray has one more run than `before`, and returns the newest. */
async function nextRun(page: Page, id: string, before: number): Promise<Run> {
  await expect.poll(async () => (await runs(page, id)).length, { timeout: 15_000 }).toBe(before + 1);
  return (await runs(page, id)).sort((a, b) => b.run_no - a.run_no)[0]!;
}

test.describe.serial("calibration with a check sheet", () => {
  let page: Page;
  let id: string;
  test.beforeAll(async ({ browser }) => {
    page = await (await browser.newContext({ baseURL: MAIN.url })).newPage();
    await page.goto("/");
    await signInWith(page, mainKey());
    id = await printerId(page);
  });
  test.afterAll(async () => {
    await send(page, "DELETE", `/api/v1/printers/${id}/calibration/${TRAY}`, null);
    await page.context().close();
  });
  test.afterEach(async () => {
    await resetPagePrinter(page);
  });

  test("calibration page → readings → fit → check print → run number +1; pass 2 offered and printed without codes, its readings stored against the pass-2 run", async () => {
    await page.evaluate(() => localStorage.setItem("lph.lang", "en"));
    await page.setViewportSize({ width: 1280, height: 800 });
    await setPagePrinter(page, { job_script: [3, 5, 9] });
    await page.goto(`/calibration?printer=${id}&tray=${TRAY}`);
    await expect(page.getByRole("heading", { level: 1, name: "Calibration" })).toBeVisible();
    await chooseType(page);

    // 1. The calibration page.
    const before = (await runs(page, id)).length;
    await showTab(page, "Print page");
    await page.getByRole("button", { name: "Print the calibration page (without correction)" }).click();
    const calibration = await nextRun(page, id, before);
    expect(calibration.kind).toBe("calibration_page");

    // 2. Readings, fit and save.
    await showTab(page, "Readings");
    for (const crossing of ["S1 · Z1", "S3 · Z1", "S1 · Z11", "S3 · Z11"]) {
      await page.getByRole("textbox", { name: `Across at ${crossing} (mm)` }).fill("0,5");
      await page.getByRole("textbox", { name: `Down at ${crossing} (mm)` }).fill("0,3");
    }
    await page.getByRole("button", { name: "Fit", exact: true }).click();
    await expect(page.getByRole("textbox", { name: "Offset X (mm)" })).toHaveValue("0.5");
    await page.getByRole("button", { name: "Save for this printer and tray" }).click();
    await expect(page.getByText("Saved. New prints use this correction.")).toBeVisible();

    // 3. The check page: pass 1 of a new sheet, the next run number.
    await showTab(page, "Result");
    await page.getByRole("button", { name: "Print the check page (with correction)" }).click();
    let dialog = page.getByRole("dialog", { name: "Print the check page" });
    // A sheet left open by an interrupted earlier attempt is offered first; this test starts a new one.
    const fresh = dialog.getByText("This sheet takes 2 check passes. This is pass 1, on a fresh sheet: keep the sheet for the next check.");
    const newSheet = dialog.getByRole("radio", { name: /New sheet/ });
    await expect(fresh.or(newSheet)).toBeVisible();
    if (await newSheet.isVisible()) await newSheet.click();
    await dialog.getByRole("button", { name: "Print pass 1 of 2" }).click();
    const pass1 = await nextRun(page, id, before + 1);
    expect(pass1.run_no).toBe(calibration.run_no + 1);
    expect([pass1.kind, pass1.pass_no]).toEqual(["check_page", 1]);

    // 4. Pass 2 is offered on the same sheet and printed without codes.
    await expect(page.getByRole("button", { name: "Print the check page (with correction)" })).toBeEnabled();
    await page.getByRole("button", { name: "Print the check page (with correction)" }).click();
    dialog = page.getByRole("dialog", { name: "Print the check page" });
    await expect(dialog.getByRole("radio", { name: /Print on the same sheet \(pass 2 of 2\)/ })).toBeChecked();
    await dialog.getByRole("button", { name: "Print pass 2 of 2" }).click();
    const pass2 = await nextRun(page, id, before + 2);
    expect([pass2.run_no, pass2.pass_no, pass2.check_sheet_id]).toEqual([pass1.run_no + 1, 2, pass1.check_sheet_id]);
    const job = (await (await page.request.get(`/api/v1/jobs/${pass2.job_id}`)).json()) as { payload: { detail: Record<string, unknown> } };
    expect(job.payload.detail.pass_no).toBe(2);
    // The sheet is full now: nothing more to offer.
    const open = await page.request.get(`/api/v1/printers/${id}/calibration/${TRAY}/check-sheets/open?type=${TYPE}`);
    expect(await open.json()).toBeNull();

    // 5. The readings of pass 2 are stored against its run.
    const crossings = job.payload.detail.pass_crossings as Array<[number, number]>;
    const readings = crossings.map(([col, row], i) => ({ col_gap: col, row_gap: row, across_mm: 0.1 * i, down_mm: -0.1 }));
    const type = (await (await page.request.get(`/api/v1/sheet-types/${TYPE}`)).json()) as { current_version: number };
    const status = await send(page, "PUT", `/api/v1/printers/${id}/calibration/${TRAY}`, {
      offset_x_mm: 0.6,
      offset_y_mm: 0.2,
      measured_on: { type_key: TYPE, version: type.current_version },
      run_id: pass2.id,
      readings,
    });
    expect(status).toBe(200);
    const history = (await (await page.request.get(`/api/v1/printers/${id}/calibration/${TRAY}/history?limit=1`)).json()) as Array<{ run_id: string | null }>;
    expect(history[0]!.run_id).toBe(pass2.id);
  });

  /** The dialog with an open sheet whose later passes have no run line, and a page with two page codes (M3 answers). */
  async function dialogWithNotes() {
    const sheet = { check_sheet_id: "s1", passes_total: 3, passes_used: 1, next_pass: 2, run_line_slots: 0, passes: [{ pass_no: 1, run_no: 12, run_id: "r12" }] };
    const layout = { page_codes: 2, run_line_slots: 0, passes_total: 3, margin_text: { top: "reduced", bottom: "omitted" } };
    await page.route(`**/api/v1/printers/${id}/calibration/${TRAY}/check-sheets/open*`, (route) => route.fulfill({ json: sheet }));
    await page.route(`**/api/v1/printers/${id}/calibration/${TRAY}/page/layout*`, (route) => route.fulfill({ json: layout }));
    await send(page, "PUT", `/api/v1/printers/${id}/calibration/${TRAY}`, { offset_x_mm: 0.41, offset_y_mm: -0.66 });
    await page.goto(`/calibration?printer=${id}&tray=${TRAY}`);
    await chooseType(page);
    await showTab(page, "Result");
    await page.getByRole("button", { name: "Print the check page (with correction)" }).click();
    const dialog = page.getByRole("dialog", { name: "Print the check page" });
    await expect(dialog.getByRole("radio", { name: /Print on the same sheet \(pass 2 of 3\)/ })).toBeChecked();
    return dialog;
  }

  test("on a phone with the keyboard's height the dialog keeps its print button on screen (spec 12)", async () => {
    await page.evaluate(() => localStorage.setItem("lph.lang", "en"));
    await page.setViewportSize({ width: 390, height: 500 });
    try {
      const dialog = await dialogWithNotes();
      const print = dialog.getByRole("button", { name: "Print pass 2 of 3" });
      await expect(print).toBeInViewport();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
      await dialog.getByRole("button", { name: "Cancel" }).click();
    } finally {
      await page.unrouteAll({ behavior: "ignoreErrors" });
    }
  });

  test("screenshots: the check print dialog, light and dark, desktop and phone (M3, #240)", async () => {
    test.skip(!process.env.E2E_SHOTS_DIR, "screenshots are an artifact, taken only with E2E_SHOTS_DIR");
    const dir = resolve(process.env.E2E_SHOTS_DIR!);
    mkdirSync(dir, { recursive: true });
    await page.evaluate(() => localStorage.setItem("lph.lang", "en"));
    try {
      for (const [w, h] of [[1280, 800], [390, 844], [390, 500]] as const) {
        for (const scheme of ["light", "dark"] as const) {
          if (h === 500 && scheme === "dark") continue;
          await page.emulateMedia({ colorScheme: scheme });
          await page.setViewportSize({ width: w, height: h });
          await dialogWithNotes();
          await page.screenshot({ path: `${dir}/check-dialog-${w}x${h}-${scheme}.png` });
          await page.keyboard.press("Escape");
        }
      }
    } finally {
      await page.unrouteAll({ behavior: "ignoreErrors" });
      await page.emulateMedia({ colorScheme: "light" });
    }
  });
});
