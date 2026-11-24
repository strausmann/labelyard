// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import type { Page } from "@playwright/test";
import { MAIN } from "./env";
import { expect, mainKey, signInWith, test } from "./fixtures";

// #246: a preprinted area entered in Edit details is kept free, and the print preview shows it hatched.
const TYPE = "avery-zweckform-l4736rev";
const SHEET = "L4736-0001"; // 17 of 48 cells used
const KEY = () => ({ "X-Label-Hub-Key": mainKey() });
const setLanguage = (page: Page, lang: "de" | "en") => page.evaluate((l) => localStorage.setItem("lph.lang", l), lang);
const clearZones = (page: Page) =>
  page.request.patch(`/api/v1/sheet-types/${TYPE}`, { data: { preprint_zones: [], preprint_preset: null }, headers: KEY() });

/** The cart holds one Grocy label and the page printer is the chosen one; then `path` opens. */
async function openCart(page: Page, path: string) {
  const printers = (await (await page.request.get("/api/v1/printers")).json()) as Array<{ id: string; name: string }>;
  const printerId = printers.find((p) => p.name === "Office page printer")!.id;
  await page.goto("/");
  await page.evaluate((id) => {
    localStorage.setItem("lph.printer", id);
    localStorage.setItem("lph.cart", JSON.stringify([{ app: "grocy", id: "7", name: "Flour", content_type: "qr_two_lines", copies: 1 }]));
  }, printerId);
  await page.goto(path);
}

test.describe.serial("preprinted areas", () => {
  let page: Page;
  test.beforeAll(async ({ browser }) => {
    page = await (await browser.newContext({ baseURL: MAIN.url })).newPage();
    await page.goto("/");
    await signInWith(page, mainKey());
    await expect(page.getByRole("navigation", { name: /^(Main|Tabs)$/ }).first()).toBeVisible();
  });
  test.afterAll(async () => {
    await clearZones(page);
    await page.evaluate(() => localStorage.removeItem("lph.cart"));
    await page.context().close();
  });

  test("zone added → preview hatched", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 1440, height: 900 });
    expect((await clearZones(page)).ok()).toBe(true);
    await page.goto(`/sheets/archive?type=${TYPE}`);
    await page.getByRole("button", { name: "Edit details" }).click();
    await page.getByRole("button", { name: "Add area" }).click();
    const dialog = page.getByRole("dialog", { name: "New area 1" });
    await dialog.getByRole("textbox", { name: "Note" }).fill("Logo");
    // A decimal comma, as a German keyboard types it.
    await dialog.getByRole("textbox", { name: "x" }).fill("10");
    await dialog.getByRole("textbox", { name: "y" }).fill("6,5");
    await dialog.getByRole("textbox", { name: "Width" }).fill("34");
    await dialog.getByRole("textbox", { name: "Height" }).fill("9");
    await dialog.getByRole("button", { name: "Save area" }).click();
    await expect(dialog).toHaveCount(0);
    // The diagram beside the form draws it hatched before saving.
    await expect(page.getByRole("img", { name: /^Sheet front with/ }).locator('[data-zone="kept"]')).toHaveCount(1);
    await page.getByRole("button", { name: "Save details" }).click();
    await expect(page.getByRole("button", { name: "Edit details" })).toBeVisible();

    const stored = (await (await page.request.get(`/api/v1/sheet-types/${TYPE}`, { headers: KEY() })).json()) as { preprint_zones: unknown[] };
    expect(stored.preprint_zones).toEqual([{ x_mm: 10, y_mm: 6.5, w_mm: 34, h_mm: 9, note: "Logo" }]);

    const preview = page.waitForResponse((r) => r.url().includes("/preview") && r.status() === 200);
    await openCart(page, `/cart?sheet=${SHEET}`);
    await preview;
    const grid = page.getByRole("grid", { name: SHEET });
    await expect(grid).toBeVisible();
    const zone = grid.locator("[data-zones] [data-zone]");
    await expect(zone).toHaveCount(1);
    expect(await zone.evaluate((el) => [el.getAttribute("x"), el.getAttribute("y"), el.getAttribute("width"), el.getAttribute("height")])).toEqual([
      "10",
      "6.5",
      "34",
      "9",
    ]);
    expect(await zone.evaluate((el) => el.getAttribute("fill"))).toMatch(/^url\(#.+-zone\)$/);
    await expect(page.getByText("Hatched: preprinted areas of the sheet. Nothing is printed there.")).toBeVisible();
    const shots = process.env.E2E_SHOTS_DIR;
    if (shots) await page.screenshot({ path: `${shots}/task-10b-preview-zone-1440-light.png`, fullPage: false });
  });

  test("a zone over a label is refused and marked in the list", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 1440, height: 900 });
    expect((await clearZones(page)).ok()).toBe(true);
    await page.goto(`/sheets/archive?type=${TYPE}`);
    await page.getByRole("button", { name: "Edit details" }).click();
    await page.getByRole("button", { name: "Add area" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("textbox", { name: "Note" }).fill("Dust cover");
    await dialog.getByRole("textbox", { name: "x" }).fill("12");
    await dialog.getByRole("textbox", { name: "y" }).fill("22");
    await dialog.getByRole("textbox", { name: "Width" }).fill("30");
    await dialog.getByRole("textbox", { name: "Height" }).fill("6");
    await expect(dialog.getByText(/^Covers label 1 \(row 1, column 1\)\./)).toBeVisible();
    await dialog.getByRole("button", { name: "Save area" }).click();
    await page.getByRole("button", { name: "Save details" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "1 area was not saved" })).toBeVisible();
    await expect(page.getByRole("list", { name: "Preprinted areas" }).getByText("preprint_zone_overlaps_cell")).toBeVisible();
    await expect(page.getByRole("img", { name: /^Sheet front with/ }).locator('[data-zone="refused"]')).toHaveCount(1);
    const shots = process.env.E2E_SHOTS_DIR;
    if (shots) {
      for (const scheme of ["light", "dark"] as const) {
        await page.emulateMedia({ colorScheme: scheme });
        for (const [width, height] of [
          [1440, 900],
          [390, 844],
        ] as const) {
          await page.setViewportSize({ width, height });
          await page.screenshot({ path: `${shots}/task-10b-edit-details-${width}-${scheme}.png`, fullPage: true });
        }
      }
      await page.emulateMedia({ colorScheme: "light" });
    }
  });

  test("the type page shows the code position and where it fits per printer", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 1440, height: 900 });
    expect((await clearZones(page)).ok()).toBe(true);
    await page.goto(`/sheets/archive?type=${TYPE}`);
    await page.getByRole("tab", { name: "Where the code goes" }).click();
    await expect(page.getByRole("heading", { name: "Position in the margin" })).toBeVisible();
    const printers = page.getByRole("list", { name: "Printers" });
    await expect(printers.getByRole("listitem").first()).toContainText("Office page printer");
    await expect(printers.getByText("Fits").first()).toBeVisible();
    await expect(page.getByRole("img", { name: /^Sheet front with/ }).locator("[data-id-rect]")).toHaveCount(1);
    const shots = process.env.E2E_SHOTS_DIR;
    if (shots) {
      for (const scheme of ["light", "dark"] as const) {
        await page.emulateMedia({ colorScheme: scheme });
        for (const [width, height] of [
          [1440, 900],
          [390, 844],
        ] as const) {
          await page.setViewportSize({ width, height });
          await page.screenshot({ path: `${shots}/task-10b-sheet-code-${width}-${scheme}.png`, fullPage: true });
        }
      }
      await page.emulateMedia({ colorScheme: "light" });
    }
  });
});
