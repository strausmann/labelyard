// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import type { Locator, Page } from "@playwright/test";
import { MAIN } from "./env";
import { expect, mainKey, resetPagePrinter, setPagePrinter, signInWith, test } from "./fixtures";

const TYPE = "avery-zweckform-l4736rev"; // 4 x 12; no gap between its rows
const PRINTER = "Office page printer";
const TRAY = "main-tray";

/** A box in page coordinates: a click scrolls the viewport, which would move a viewport-relative box without the page changing. */
const pageBox = (locator: Locator) =>
  locator.evaluate((el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x + window.scrollX, y: r.y + window.scrollY, width: r.width, height: r.height };
  });
/** The line block around a live status element: it also holds the job link and Retry. */
const around = (status: Locator) => status.locator("xpath=..");
const noSideScroll = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
const setLanguage = (page: Page, lang: "de" | "en") => page.evaluate((l) => localStorage.setItem("lph.lang", l), lang);

async function printerId(page: Page): Promise<string> {
  const printers = (await (await page.request.get("/api/v1/printers")).json()) as Array<{ id: string; name: string }>;
  return printers.find((p) => p.name === PRINTER)!.id;
}

/** What the capabilities say about the tray (the endpoint the print dialog reads). */
async function trayCalibration(page: Page, id: string) {
  const caps = (await (await page.request.get(`/api/v1/printers/${id}/capabilities`)).json()) as {
    trays: Array<{ key: string; calibration: { offset_x_mm: number | null; offset_y_mm: number | null } | null }>;
  };
  return caps.trays.find((t) => t.key === TRAY)!.calibration;
}
const hasCorrection = (c: { offset_x_mm: number | null; offset_y_mm: number | null } | null) => !!c && (c.offset_x_mm != null || c.offset_y_mm != null);

/** Sends or removes a correction for the tray through the API, with the session's CSRF token. */
async function setCorrection(page: Page, id: string, values: object | null) {
  return page.evaluate(
    async ([path, body]) => {
      const session = await (await fetch("/api/v1/auth/session")).json();
      const response = await fetch(path as string, {
        method: body ? "PUT" : "DELETE",
        headers: { "X-CSRF-Token": session.csrf_token, ...(body ? { "Content-Type": "application/json" } : {}) },
        body: body ? JSON.stringify(body) : undefined,
      });
      return response.status;
    },
    [`/api/v1/printers/${id}/calibration/${TRAY}`, values] as const,
  );
}

async function jobsOf(page: Page, id: string): Promise<string[]> {
  const jobs = (await (await page.request.get("/api/v1/jobs")).json()) as Array<{ id: string; printer_id: string }>;
  return jobs.filter((j) => j.printer_id === id).map((j) => j.id);
}

/**
 * Chooses the 4 x 12 type in the combobox the way a person does: tap it, type the key. The one option then sits at the
 * top of the list; Playwright would otherwise scroll the page or dialog to reach a far option, and a scroll of anything
 * that contains the field closes the (non-modal) list.
 */
async function chooseType(page: Page) {
  const box = page.getByRole("combobox", { name: /Sheet type|Bogentyp/ });
  await box.click();
  // Wait for the list and select the shown label explicitly, so the first character is typed over it and never lost to a
  // late re-selection (263-85); then check what the field holds before picking.
  await expect(page.getByRole("listbox")).toBeVisible();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.type(TYPE);
  await expect(box).toHaveValue(TYPE);
  await page.getByRole("option", { name: new RegExp(`\\(${TYPE}\\)`) }).click();
}

async function openCalibration(page: Page, id: string) {
  await page.goto(`/calibration?printer=${id}&tray=${TRAY}`);
  await expect(page.getByRole("heading", { level: 1, name: /Calibration|Kalibrierung/ })).toBeVisible();
}

/** Opens one of the page's tabs (mockups M7, M8): Print page, Readings, Result, History. */
async function showTab(page: Page, name: RegExp | string) {
  const tab = page.getByRole("tab", { name });
  await tab.click();
  await expect(tab).toHaveAttribute("aria-selected", "true");
}

// One sign-in for the whole file: the hub limits sign-ins per minute.
test.describe.serial("calibration", () => {
  let page: Page;
  let id: string;
  test.beforeAll(async ({ browser }) => {
    page = await (await browser.newContext({ baseURL: MAIN.url })).newPage();
    await page.goto("/");
    await signInWith(page, mainKey());
    await expect(page.getByRole("navigation", { name: /^(Main|Tabs)$/ }).first()).toBeVisible();
    id = await printerId(page);
  });
  test.afterAll(async () => {
    await page.context().close();
  });
  test.afterEach(async () => {
    await resetPagePrinter(page);
  });

  test("prints a calibration page to the fake printer: a job appears", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 1280, height: 800 });
    await setPagePrinter(page, { job_script: [3, 5, 9] });
    const before = await jobsOf(page, id);
    await openCalibration(page, id);
    await chooseType(page);
    await page.getByRole("button", { name: "Print the calibration page (without correction)" }).click();
    const link = page.getByRole("link", { name: "Show the job" });
    await expect(link).toBeVisible();
    const jobId = (await link.getAttribute("href"))!.split("/").pop()!;
    // Relative: other specs print to the same fake.
    await expect.poll(async () => (await jobsOf(page, id)).length, { timeout: 15_000 }).toBeGreaterThan(before.length);
    expect(await jobsOf(page, id)).toContain(jobId);
    // The download is the same page as a PDF.
    const pdf = page.getByRole("link", { name: "Download PDF" });
    expect(await pdf.getAttribute("href")).toMatch(/\/page\/pdf\?/);
    const response = await page.request.get((await pdf.getAttribute("href"))!);
    expect(response.headers()["content-type"]).toContain("application/pdf");
  });

  test("the reading guide opens on a phone and fits the width", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 390, height: 844 });
    await openCalibration(page, id);
    await chooseType(page);
    await showTab(page, "Readings");
    const trigger = page.getByRole("button", { name: "How to read the calibration page" });
    await expect(trigger).toHaveAttribute("aria-expanded", "false");
    await trigger.click();
    await expect(trigger).toHaveAttribute("aria-expanded", "true");
    const one = page.getByRole("img", { name: "How to read one crossing" });
    const sheet = page.getByRole("img", { name: /^The crossings to read on / });
    await expect(one).toBeVisible();
    await expect(sheet).toBeVisible();
    // Neither drawing is wider than the screen, and the page does not scroll sideways.
    for (const figure of [one, sheet]) {
      const box = await figure.boundingBox();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(390);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    // The guide's link leaves the app in a new tab.
    const link = page.getByRole("link", { name: /More on labelyard\.app/ });
    await expect(link).toHaveAttribute("href", "https://labelyard.app/using/calibration/");
    await expect(link).toHaveAttribute("target", "_blank");
    // The card under Current values reads at this width too.
    await showTab(page, "Result");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    const stillOff = page.getByRole("heading", { name: "Still off after the check print?" });
    await stillOff.scrollIntoViewIfNeeded();
    await expect(stillOff).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  });

  test("the print result sits below the buttons: they do not move, and the line reaches Done without a reload (#239)", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 1280, height: 800 });
    await setPagePrinter(page, { job_script: [3, 5, 9] });
    await openCalibration(page, id);
    await chooseType(page);
    const print = page.getByRole("button", { name: "Print the calibration page (without correction)" });
    const pdf = page.getByRole("link", { name: "Download PDF" });
    await expect(pdf).toBeVisible();
    const before = { print: await pageBox(print), pdf: await pageBox(pdf) };
    // A marker on the window: a reload would erase it.
    await page.evaluate(() => ((window as unknown as { __kept: boolean }).__kept = true));
    await print.click();
    const line = page.getByRole("status", { name: "Calibration page status" });
    await expect(around(line).getByRole("link", { name: "Show the job" })).toBeVisible();
    const after = { print: await pageBox(print), pdf: await pageBox(pdf) };
    expect(after).toEqual(before);
    // The line is under the button row, not in it.
    const row = await pageBox(pdf);
    expect((await pageBox(line)).y).toBeGreaterThanOrEqual(row.y + row.height);
    await expect(line.getByText("Done", { exact: true })).toBeVisible({ timeout: 45_000 });
    await expect(line.getByText(/measure the crossings/)).toBeVisible();
    expect({ print: await pageBox(print), pdf: await pageBox(pdf) }).toEqual(before);
    expect(await page.evaluate(() => (window as unknown as { __kept?: boolean }).__kept)).toBe(true);
  });

  test("typing 4736 narrows the sheet types and Enter chooses one: the legend follows (#237)", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 1280, height: 800 });
    await openCalibration(page, id);
    const box = page.getByRole("combobox", { name: "Sheet type" });
    const legend = page.getByRole("img", { name: /^Calibration page of / });

    // Start on another type, so the change below is a change.
    await box.click();
    const all = await page.getByRole("option").allTextContents();
    expect(all.length).toBeGreaterThan(2);
    await page.getByRole("option").filter({ hasNotText: `(${TYPE})` }).first().click();
    await expect(legend).not.toHaveAttribute("aria-label", new RegExp(`\\(${TYPE}\\)`));

    // The field still has the focus (a tap on it opens the list again); the tap selects the shown label, typing replaces it.
    await box.click();
    await expect(page.getByRole("listbox")).toBeVisible();
    await page.keyboard.type("4736");
    await expect(box).toHaveValue("4736");
    const shown = await page.getByRole("option").allTextContents();
    expect(shown.length).toBeGreaterThan(0);
    expect(shown.length).toBeLessThan(all.length);
    expect(shown.every((text) => /4736/i.test(text))).toBe(true);
    await page.keyboard.press("Enter");

    await expect(legend).toHaveAttribute("aria-label", new RegExp(`\\(${TYPE}\\)`));
    await expect(box).toHaveValue(new RegExp(`\\(${TYPE}\\)`));
    // Measured, not read from classes: the field is 16 px or more (no iOS zoom), an option is 44 px or more.
    expect(await box.evaluate((el) => parseFloat(getComputedStyle(el).fontSize))).toBeGreaterThanOrEqual(16);
    await page.keyboard.press("Escape");
    await box.click();
    // Wait for the option to have its size.
    await expect.poll(async () => (await page.getByRole("option").first().boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);

    // On a phone: no sideways scroll, and the open list ends above the fixed tab bar.
    await page.keyboard.press("Escape");
    // A popover that is still leaving (it waits for its enter animation to end) keeps the width of the wide window: resize after it is gone.
    await expect(page.getByRole("listbox")).toHaveCount(0);
    await page.setViewportSize({ width: 390, height: 800 });
    expect(await noSideScroll(page)).toBe(true);
    // Bring the field itself into view first, and let the scroll settle. The heading above it is already visible at 390 px, so
    // scrolling to the heading does nothing, and the click then scrolls the page to reach the field (it sits below the fold, behind
    // the tab bar). A scroll closes the non-modal list (React Aria), and its event arrives after the click opened the list:
    // on a slow runner the list was gone before it could be measured. A person taps a field that is already on screen.
    await box.evaluate((el) => el.scrollIntoView({ block: "center" }));
    await page.evaluate(() => new Promise<void>((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
    const scrolledTo = await page.evaluate(() => window.scrollY);
    await box.click();
    const listbox = page.getByRole("listbox");
    await expect(listbox).toBeVisible();
    // The list fades in for 140 ms: wait until its box stopped changing instead of sleeping.
    await expect
      .poll(async () => {
        const first = await listbox.boundingBox();
        await page.evaluate(() => new Promise<void>((done) => requestAnimationFrame(() => done())));
        const second = await listbox.boundingBox();
        return first && second ? first.height === second.height && first.y === second.y : false;
      })
      .toBe(true);
    expect(await page.evaluate(() => window.scrollY), "the click must not scroll the page").toBe(scrolledTo);
    const list = (await listbox.boundingBox())!;
    const bar = (await page.locator("[data-tabbar]").boundingBox())!;
    expect(list.y + list.height).toBeLessThanOrEqual(bar.y);
  });

  test("a failed calibration print shows its error and a Retry under the buttons (#239)", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 1280, height: 800 });
    // IPP job-state 8 = aborted: the hub marks the job failed.
    await setPagePrinter(page, { job_script: [3, 8] });
    await openCalibration(page, id);
    await chooseType(page);
    const pdf = page.getByRole("link", { name: "Download PDF" });
    const before = await pageBox(pdf);
    await page.getByRole("button", { name: "Print the calibration page (without correction)" }).click();
    const line = page.getByRole("status", { name: "Calibration page status" });
    await expect(line.getByText("Failed", { exact: true })).toBeVisible({ timeout: 45_000 });
    await expect(line.getByText("The printer aborted the job before printing.")).toBeVisible();
    await expect(around(line).getByRole("button", { name: "Retry" })).toBeVisible();
    // The live element holds only the sentence; the controls are beside it.
    await expect(line.getByRole("button")).toHaveCount(0);
    expect(await pageBox(pdf)).toEqual(before);
  });

  test("screenshots: the status line done and failed, light and dark, desktop and phone (#239)", async () => {
    test.skip(!process.env.E2E_SHOTS_DIR, "screenshots are an artifact, taken only with E2E_SHOTS_DIR");
    test.setTimeout(170_000);
    const dir = process.env.E2E_SHOTS_DIR!;
    mkdirSync(dir, { recursive: true });
    await setLanguage(page, "en");
    for (const scheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      for (const [width, height] of [
        [1280, 800],
        [390, 844],
      ] as const) {
        await page.setViewportSize({ width, height });
        for (const outcome of ["done", "failed"] as const) {
          await setPagePrinter(page, { job_script: outcome === "done" ? [3, 5, 9] : [3, 8] });
          await openCalibration(page, id);
          await chooseType(page);
          await page.getByRole("button", { name: "Print the calibration page (without correction)" }).click();
          const line = page.getByRole("status", { name: "Calibration page status" });
          await expect(line.getByText(outcome === "done" ? "Done" : "Failed", { exact: true })).toBeVisible({ timeout: 45_000 });
          // Centred, so the notification stack at the bottom of a phone does not cover the line.
          await line.evaluate((el) => el.scrollIntoView({ block: "center" }));
          expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
          await page.screenshot({ path: resolve(dir, `task-19b-${outcome}-${width}-${scheme}.png`) });
          await resetPagePrinter(page);
        }
      }
    }
    await page.emulateMedia({ colorScheme: "light" });
  });

  test("the chosen sheet type survives a reload and a tray change (#238)", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 1280, height: 800 });
    await openCalibration(page, id);
    await chooseType(page);
    const box = page.getByRole("combobox", { name: "Sheet type" });
    const named = new RegExp(`\\(${TYPE}\\)`);
    await expect(box).toHaveValue(named);
    await expect(async () => expect(new URL(page.url()).searchParams.get("type")).toBe(TYPE)).toPass();

    // A reload keeps it: the address carries it.
    await page.reload();
    await expect(page.getByRole("combobox", { name: "Sheet type" })).toHaveValue(named);

    // A tray change keeps it. The fixture printer has two trays (the main tray and "Automatic"), so this is not optional.
    const trayButton = page.getByRole("button", { name: / Tray$/ });
    await expect(trayButton).toBeVisible();
    await trayButton.click();
    await page.getByRole("option", { name: "Automatic" }).click();
    await expect(async () => expect(new URL(page.url()).searchParams.get("tray")).toBe("auto")).toPass();
    await expect(page.getByRole("combobox", { name: "Sheet type" })).toHaveValue(named);
    expect(new URL(page.url()).searchParams.get("type")).toBe(TYPE);

    // A key the hub does not know opens on the default type instead of breaking the page.
    await page.goto(`/calibration?printer=${id}&tray=${TRAY}&type=no-such-type`);
    await expect(page.getByRole("combobox", { name: "Sheet type" })).not.toHaveValue("");
    // ... and the unknown key leaves the address (263-57).
    await expect(async () => expect(new URL(page.url()).searchParams.get("type")).toBeNull()).toPass();
  });

  test("screenshots: the kept type after a tray change, light and dark, desktop and phone (#238)", async () => {
    test.skip(!process.env.E2E_SHOTS_DIR, "screenshots are an artifact, taken only with E2E_SHOTS_DIR");
    test.setTimeout(120_000);
    const dir = process.env.E2E_SHOTS_DIR!;
    mkdirSync(dir, { recursive: true });
    await setLanguage(page, "en");
    for (const scheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      for (const [width, height] of [
        [1280, 800],
        [390, 844],
      ] as const) {
        await page.setViewportSize({ width, height });
        await openCalibration(page, id);
        await chooseType(page);
        const trayButton = page.getByRole("button", { name: / Tray$/ });
        if (await trayButton.count()) {
          await trayButton.click();
          await page.getByRole("option").filter({ hasNotText: (await trayButton.textContent()) ?? "" }).first().click();
        }
        await expect(page.getByRole("combobox", { name: "Sheet type" })).toHaveValue(new RegExp(`\\(${TYPE}\\)`));
        await page.getByRole("heading", { level: 2, name: "Step 1: Print the calibration page" }).scrollIntoViewIfNeeded();
        expect(await noSideScroll(page)).toBe(true);
        await page.screenshot({ path: resolve(dir, `task-19d-tray-change-${width}-${scheme}.png`) });
      }
    }
    await page.emulateMedia({ colorScheme: "light" });
  });

  test("the Calibrate link on the printer page opens the calibration with that printer and tray chosen", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto(`/printers/${id}`);
    const links = page.getByRole("link", { name: /^Calibrate .+/ });
    await expect(links.last()).toBeVisible();
    const name = (await links.last().getAttribute("aria-label"))!;
    const trayLabel = name.replace(/^Calibrate /, "");
    // Keyboard only: the link is reached and opened with Tab and Enter.
    await links.last().focus();
    await expect(links.last()).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { level: 1, name: "Calibration" })).toBeVisible();
    const url = new URL(page.url());
    expect(url.pathname).toBe("/calibration");
    expect(url.searchParams.get("printer")).toBe(id);
    expect(url.searchParams.get("tray")).toBeTruthy();
    // The choice is shown, not only in the address: printer and tray are preselected.
    await expect(page.getByRole("button", { name: / Printer$/ })).toContainText(PRINTER);
    await expect(page.getByRole("button", { name: / Tray$/ })).toContainText(trayLabel);
  });

  test("a correction for another tray than the printed calibration page is warned about (#344)", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto(`/calibration?printer=${id}&tray=auto`);
    await expect(page.getByRole("heading", { level: 1, name: "Calibration" })).toBeVisible();
    await chooseType(page);
    await page.getByRole("button", { name: "Print the calibration page (without correction)" }).click();
    await expect(page.getByRole("link", { name: "Show the job" })).toBeVisible();

    // The values are about to be saved for the main tray, but the page came from "Automatic".
    const trayButton = page.getByRole("button", { name: / Tray$/ });
    await trayButton.click();
    await page.getByRole("option", { name: "Main tray" }).click();
    await showTab(page, "Readings");
    await expect(page.getByText(/The calibration page was printed from Automatic, but you are saving for Main tray/)).toBeVisible();

    // Back on the tray the page came from, nothing to warn about.
    await trayButton.click();
    await page.getByRole("option", { name: "Automatic" }).click();
    await expect(page.getByText("Saves for: Automatic")).toBeVisible();
    await expect(page.getByText(/The calibration page was printed from/)).toHaveCount(0);
  });

  test("fits four readings, saves them, the capabilities show the values; reset puts the tray back", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 1280, height: 800 });
    expect(hasCorrection(await trayCalibration(page, id))).toBe(false);
    try {
      await openCalibration(page, id);
      await chooseType(page);
      await showTab(page, "Readings");
      // The newest print of the tray may be a check pass of another spec (its readings are never a correction):
      // these readings belong to a calibration page.
      const picker = page.getByRole("button", { name: /Readings from print/ });
      if (await picker.isVisible()) {
        await picker.click();
        await page.getByRole("option", { name: /· calibration page ·/ }).first().click();
      }
      // The same deviation at all four outermost crossings: 0.5 mm to the right, 0.3 mm too low.
      for (const crossing of ["S1 · Z1", "S3 · Z1", "S1 · Z11", "S3 · Z11"]) {
        await page.getByRole("textbox", { name: `Across at ${crossing} (mm)` }).fill("0,5");
        await page.getByRole("textbox", { name: `Down at ${crossing} (mm)` }).fill("0,3");
      }
      await page.getByRole("button", { name: "Fit", exact: true }).click();
      await expect(page.getByRole("table", { name: "Result of the fit" })).toBeVisible();
      await expect(page.getByText("probable misread", { exact: true })).toHaveCount(0);
      await expect(page.getByRole("textbox", { name: "Offset X (mm)" })).toHaveValue("0.5");
      await expect(page.getByRole("textbox", { name: "Offset Y (mm)" })).toHaveValue("0.3");

      await page.getByRole("button", { name: "Save for this printer and tray" }).click();
      await expect(page.getByText("Saved. New prints use this correction.")).toBeVisible();
      await expect.poll(async () => (await trayCalibration(page, id))?.offset_x_mm, { timeout: 10_000 }).toBeCloseTo(0.5, 2);
      expect((await trayCalibration(page, id))?.offset_y_mm).toBeCloseTo(0.3, 2);
      // The stored values are shown, with the date of the change.
      await showTab(page, "Result");
      const current = page.getByRole("region", { name: "Current values" });
      await expect(current.getByText("0.5 mm", { exact: true })).toBeVisible();
      await expect(current.getByText(/Last changed/)).toBeVisible();

      // The check print is offered now, and goes to the queue.
      await page.getByRole("button", { name: "Print the check page (with correction)" }).click();
      // It asks first which sheet: L4736 takes two check passes, this is pass 1 of a new sheet (#240).
      await page.getByRole("dialog", { name: "Print the check page" }).getByRole("button", { name: "Print pass 1 of 2" }).click();
      await expect(around(page.getByRole("status", { name: "Check page status" })).getByRole("link", { name: "Show the job" })).toBeVisible();

      // Back to no correction, through the page.
      await showTab(page, "Readings");
      await page.getByRole("button", { name: "Reset to no correction" }).click();
      await page.getByRole("button", { name: "Yes, reset" }).click();
      await expect(page.getByText("Reset. This tray prints without correction.")).toBeVisible();
      await expect.poll(async () => hasCorrection(await trayCalibration(page, id)), { timeout: 10_000 }).toBe(false);
    } finally {
      // Later specs share this hub: leave the tray without a correction whatever happened above.
      const status = await page.evaluate(
        async ([p]) => {
          const session = await (await fetch("/api/v1/auth/session")).json();
          return (await fetch(p as string, { method: "DELETE", headers: { "X-CSRF-Token": session.csrf_token } })).status;
        },
        [`/api/v1/printers/${id}/calibration/${TRAY}`] as const,
      );
      // Not asserted here: a failing check in `finally` would replace the error of the test body. The state is asserted below.
      void status;
    }
    expect(hasCorrection(await trayCalibration(page, id))).toBe(false);
  });

  test("with a stored correction the first print action on the page is the check page (#349)", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 1280, height: 800 });
    const check = page.getByRole("button", { name: "Print the check page (with correction)" });
    const again = page.getByRole("button", { name: "Print the calibration page (without correction)" });
    try {
      // Without a correction the calibration page is the main action and the check waits, disabled.
      expect(await setCorrection(page, id, null)).toBeLessThan(500);
      await openCalibration(page, id);
      // Without a correction the page opens on Print page; the check sits under Result.
      await expect(page.getByRole("tab", { name: "Print page" })).toHaveAttribute("aria-selected", "true");
      await expect(again).toBeEnabled();
      await showTab(page, "Result");
      await expect(check).toBeDisabled();
      await expect(page.getByText("Measure and save values first, then check.")).toBeVisible();

      expect(await setCorrection(page, id, { offset_x_mm: 0.4, offset_y_mm: -0.2, scale_x: 1, scale_y: 1, rotation_deg: 0 })).toBe(200);
      await openCalibration(page, id);
      await chooseType(page);
      // With a correction the page opens on Result: the check page is the print action shown, on the first screen.
      await expect(page.getByRole("tab", { name: "Result" })).toHaveAttribute("aria-selected", "true");
      await expect(check).toBeEnabled();
      const printActions = page.getByRole("button", { name: /^Print the (check|calibration) page/ });
      await expect(printActions.first()).toHaveAccessibleName("Print the check page (with correction)");
      await expect(again).toBeHidden();
      const box = (await check.boundingBox())!;
      expect(box.y + box.height).toBeLessThanOrEqual(800);

      // Measuring again asks first; nothing is queued until the dialog is answered.
      const before = await jobsOf(page, id);
      await showTab(page, "Print page");
      await again.click();
      const dialog = page.getByRole("alertdialog", { name: "Print without correction?" });
      await expect(dialog).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      expect(await jobsOf(page, id)).toEqual(before);
    } finally {
      await setCorrection(page, id, null);
    }
    expect(hasCorrection(await trayCalibration(page, id))).toBe(false);
  });

  test("the dialog fits a phone: every button on screen, no sideways scroll (#349)", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 390, height: 640 });
    try {
      expect(await setCorrection(page, id, { offset_x_mm: 0.4, offset_y_mm: -0.2, scale_x: 1, scale_y: 1, rotation_deg: 0 })).toBe(200);
      await openCalibration(page, id);
      await showTab(page, "Print page");
      await page.getByRole("button", { name: "Print the calibration page (without correction)" }).click();
      const dialog = page.getByRole("alertdialog", { name: "Print without correction?" });
      await expect(dialog).toBeVisible();
      for (const name of ["Cancel", "Print the check page", "Measure again anyway"]) {
        const b = await dialog.getByRole("button", { name }).boundingBox();
        expect(b, name).not.toBeNull();
        expect(b!.x).toBeGreaterThanOrEqual(0);
        expect(b!.x + b!.width).toBeLessThanOrEqual(390);
        // ... and in the height: a button below the 640 px edge would need a scroll inside the dialog to be pressed.
        expect(b!.y).toBeGreaterThanOrEqual(0);
        expect(b!.y + b!.height).toBeLessThanOrEqual(640);
      }
      expect(await noSideScroll(page)).toBe(true);
      await page.keyboard.press("Escape");
    } finally {
      await setCorrection(page, id, null);
    }
  });

  test("screenshots: history, runs, restore dialog and type versions, light and dark, desktop and phone (#336, M7)", async () => {
    test.skip(!process.env.E2E_SHOTS_DIR, "screenshots are an artifact, taken only with E2E_SHOTS_DIR");
    test.setTimeout(180_000);
    const dir = process.env.E2E_SHOTS_DIR!;
    mkdirSync(dir, { recursive: true });
    await setLanguage(page, "en");
    // The answers of the mockup (M7), so the screenshots can be held against it; the page itself is the real one.
    const v = (x: number, y: number) => ({ offset_x_mm: x, offset_y_mm: y, scale_x: 1.00012, scale_y: 0.99992, rotation_deg: 0.021 });
    const zero = { offset_x_mm: 0, offset_y_mm: 0, scale_x: 1, scale_y: 1, rotation_deg: 0 };
    const base = { method: "manual", measured_on: { type_key: TYPE, version: 1 }, measured_on_deleted: false, run_id: null, run_no: null, readings: null, residuals_mm: null, reverted_from_id: null, actor: "admin", note: null };
    const history = [
      { ...base, id: "e4", seq: 4, action: "revert", source: null, values: v(0.41, -0.66), previous: v(0.38, -0.62), reverted_from_id: "e2", note: "The new tray feed drifted back; entry 3 was a bad fit.", jobs_count: 3, created_at: "2026-11-21T14:05:00Z", diff: { offset_x_mm: { old: 0.38, new: 0.41 }, offset_y_mm: { old: -0.62, new: -0.66 } } },
      { ...base, id: "e3", seq: 3, action: "save", source: "fit", values: v(0.38, -0.62), previous: v(0.41, -0.66), run_id: "r12", run_no: 12, readings: Array.from({ length: 12 }, (_, i) => ({ col_gap: 1 + (i % 3), row_gap: 1 + i, across_mm: 0.4, down_mm: -0.6 })), residuals_mm: [0.05, 0.18, 0.02], jobs_count: 5, created_at: "2026-11-20T18:22:00Z", diff: { offset_x_mm: { old: 0.41, new: 0.38 }, offset_y_mm: { old: -0.66, new: -0.62 } } },
      { ...base, id: "e2", seq: 2, action: "save", source: "manual", values: v(0.41, -0.66), previous: zero, jobs_count: 14, created_at: "2026-11-15T10:40:00Z", diff: { offset_x_mm: { old: 0, new: 0.41 }, offset_y_mm: { old: 0, new: -0.66 }, scale_x: { old: 1, new: 1.00012 }, scale_y: { old: 1, new: 0.99992 }, rotation_deg: { old: 0, new: 0.021 } } },
      { ...base, id: "e1", seq: 1, action: "imported", source: null, values: zero, previous: null, measured_on: null, actor: null, jobs_count: 0, created_at: "2026-11-01T08:00:00Z", diff: {} },
    ];
    const run = { job_id: "j", retry: false, submitted: true, requested_tray: TRAY, actual_media_source: null, media_source_reported: true, check_sheet_id: null, pass_no: null, type_key: TYPE, version: 1 };
    const runs = [
      { ...run, id: "r14", run_no: 14, kind: "check_page", created_at: "2026-11-21T14:44:00Z" },
      { ...run, id: "r13", run_no: 13, kind: "check_page", created_at: "2026-11-21T14:32:00Z", actual_media_source: "auto" },
      { ...run, id: "r12", run_no: 12, kind: "calibration_page", created_at: "2026-11-21T14:20:00Z", media_source_reported: false },
      { ...run, id: "r11", run_no: 11, kind: "probe_page", created_at: "2026-11-20T18:02:00Z", retry: true },
    ];
    await page.route(`**/api/v1/printers/${id}/calibration/${TRAY}/history*`, (route) => route.fulfill({ json: history }));
    await page.route(`**/api/v1/printers/${id}/calibration/${TRAY}/runs`, (route) => route.fulfill({ json: runs }));
    const g = { page_size: "A4", page_w: 210, page_h: 297, label_w: 45.7, label_h: 21.2, cols: 4, rows: 12, pitch_x: 48.2, pitch_y: 21.2, margin_top: 21.3, margin_left: 9.85, corner_radius: null, shape: "rectangle" };
    const version = { geometry: g, geometry_hash: "h", sheets: 0, created_by: "admin", changed_fields: {}, restorable: false };
    await page.route(`**/api/v1/sheet-types/${TYPE}/versions`, (route) =>
      route.fulfill({
        json: [
          { ...version, version: 3, change_note: "Pitch measured on the probe page", created_at: "2026-11-20T18:55:00Z", changed_fields: { pitch_x: { old: 48.2, new: 47.55 }, margin_left: { old: 9.85, new: 9.9 } } },
          { ...version, version: 2, change_note: "Measured again", created_at: "2026-11-12T09:10:00Z", sheets: 2, changed_fields: { margin_top: { old: 21.3, new: 21.5 } }, restorable: true },
          { ...version, version: 1, change_note: null, created_at: "2026-11-01T08:00:00Z", created_by: "system:seed", sheets: 1 },
        ],
      }),
    );
    try {
      for (const scheme of ["light", "dark"] as const) {
        await page.emulateMedia({ colorScheme: scheme });
        for (const [width, height] of [
          [1280, 900],
          [390, 844],
        ] as const) {
          await page.setViewportSize({ width, height });
          await page.goto(`/calibration?printer=${id}&tray=${TRAY}&view=history&type=${TYPE}`);
          await expect(page.getByRole("list", { name: "Corrections for this tray" })).toBeVisible();
          await expect(page.getByRole("table", { name: "Pages printed from this tray" })).toBeVisible();
          expect(await noSideScroll(page)).toBe(true);
          await page.screenshot({ path: resolve(dir, `t13c-history-${width}-${scheme}.png`), fullPage: true });
          await page.getByRole("button", { name: "Restore these values (entry 2)" }).click();
          const dialog = page.getByRole("dialog", { name: "Use these values again?" });
          await expect(dialog).toBeVisible();
          await dialog.getByRole("textbox", { name: "Note (optional)" }).fill("Back to the values of 11-15");
          await page.screenshot({ path: resolve(dir, `t13c-restore-${width}-${scheme}.png`) });
          await page.keyboard.press("Escape");
          await expect(dialog).toHaveCount(0);
          await page.goto(`/sheets/archive?type=${TYPE}`);
          await page.getByRole("tab", { name: "Versions" }).click();
          await expect(page.getByText("Horizontal pitch: 48.2 → 47.55 mm")).toBeVisible();
          expect(await noSideScroll(page)).toBe(true);
          await page.screenshot({ path: resolve(dir, `t13c-versions-${width}-${scheme}.png`), fullPage: true });
        }
      }
    } finally {
      await page.unroute(`**/api/v1/printers/${id}/calibration/${TRAY}/history*`);
      await page.unroute(`**/api/v1/printers/${id}/calibration/${TRAY}/runs`);
      await page.unroute(`**/api/v1/sheet-types/${TYPE}/versions`);
      await page.emulateMedia({ colorScheme: "light" });
    }
  });

  test("screenshots: guided readings, live fit and geometry suggestion, light and dark, desktop, phone and short (#240, M8)", async () => {
    test.skip(!process.env.E2E_SHOTS_DIR, "screenshots are an artifact, taken only with E2E_SHOTS_DIR");
    test.setTimeout(240_000);
    const dir = process.env.E2E_SHOTS_DIR!;
    mkdirSync(dir, { recursive: true });
    await setLanguage(page, "en");
    // The answers of the mockup (M8), so the screenshots can be held against it; the page itself is the real one.
    const run = { job_id: "j", retry: false, submitted: true, requested_tray: TRAY, actual_media_source: null, media_source_reported: true, type_key: TYPE, version: 1 };
    const runs = [
      { ...run, id: "r14", run_no: 14, kind: "check_page", created_at: "2026-11-21T15:10:00Z", check_sheet_id: "s1", pass_no: 2 },
      { ...run, id: "r13", run_no: 13, kind: "check_page", created_at: "2026-11-21T14:50:00Z", check_sheet_id: "s1", pass_no: 1 },
      { ...run, id: "r12", run_no: 12, kind: "calibration_page", created_at: "2026-11-21T14:20:00Z", check_sheet_id: null, pass_no: null },
    ];
    const sheet = { check_sheet_id: "s1", next_pass: 3, passes: [{ pass_no: 1, run_id: "r13", run_no: 13 }, { pass_no: 2, run_id: "r14", run_no: 14 }], passes_total: 3, passes_used: 2, run_line_slots: 2, passes_without_run_line: [] };
    const fit = {
      values: { offset_x_mm: 0.41, offset_y_mm: -0.66, scale_x: 1.00012, scale_y: 0.99992, rotation_deg: 0.021 },
      residuals_mm: [0.08, 0.12, 0.41],
      flagged: [2],
      out_of_range: [],
      suggestion: { pitch_x_hmm: 4755, pitch_y_hmm: null, margin_left_hmm: 890, margin_top_hmm: null, uneven: false },
      suggestion_unavailable: null,
    };
    await page.route(`**/api/v1/printers/${id}/calibration/${TRAY}/runs`, (route) => route.fulfill({ json: runs }));
    await page.route(`**/api/v1/printers/${id}/calibration/${TRAY}/check-sheets/open*`, (route) => route.fulfill({ json: sheet }));
    await page.route(`**/api/v1/printers/${id}/calibration/${TRAY}/fit`, (route) => route.fulfill({ json: fit }));
    // The stored correction was measured on another type: the check readings give a suggestion.
    await page.route(`**/api/v1/printers/${id}/calibration`, async (route) => {
      const rows = (await (await route.fetch()).json()) as Array<{ tray: string }>;
      const other = { offset_x_mm: 0.41, offset_y_mm: -0.66, scale_x: 1.00012, scale_y: 0.99992, rotation_deg: 0.021, method: "fit", updated_at: "2026-11-21T14:30:00Z", measured_on: { type_key: "avery-zweckform-6121", version: 1 } };
      await route.fulfill({ json: rows.map((r) => (r.tray === TRAY ? { ...r, ...other } : r)) });
    });
    const fill = async () => {
      await page.goto(`/calibration?printer=${id}&tray=${TRAY}&view=readings&type=${TYPE}`);
      await page.getByRole("button", { name: /Readings from print/ }).click();
      await page.getByRole("option", { name: /pass 2, run #14/ }).click();
      const card = (n: number) => page.getByRole("group", { name: `Reading ${n}` });
      await card(1).getByText("Left of the gap").click();
      await page.getByRole("textbox", { name: "Across at S1 · Z1 (mm)" }).fill("0,4");
      await page.getByRole("textbox", { name: "Down at S1 · Z1 (mm)" }).fill("0,7");
      await page.getByRole("textbox", { name: "Across at S3 · Z1 (mm)" }).fill("0,43");
      await page.getByRole("textbox", { name: "Down at S3 · Z1 (mm)" }).fill("-0,5");
      await page.getByRole("textbox", { name: "Across at S1 · Z11 (mm)" }).fill("-2,6");
      await page.getByRole("textbox", { name: "Down at S1 · Z11 (mm)" }).fill("0,3");
      await page.getByRole("button", { name: "Remove reading 4" }).click();
      await expect(page.getByRole("region", { name: "Geometry suggestion" })).toBeVisible();
    };
    try {
      for (const scheme of ["light", "dark"] as const) {
        await page.emulateMedia({ colorScheme: scheme });
        for (const [width, height] of [
          [1280, 900],
          [390, 844],
        ] as const) {
          await page.setViewportSize({ width, height });
          await fill();
          await page.getByRole("button", { name: "Fit", exact: true }).click();
          await expect(page.getByText("probable misread", { exact: true })).toBeVisible();
          expect(await noSideScroll(page)).toBe(true);
          // The window as tall as the page: a full-page shot would stitch the sticky bars into the middle.
          const tall = await page.evaluate(() => document.documentElement.scrollHeight);
          await page.setViewportSize({ width, height: tall });
          await page.screenshot({ path: resolve(dir, `t15b-readings-${width}-${scheme}.png`) });
        }
      }
      // The entry view: a phone with the keyboard up (the layout viewport shrinks, as Chromium does).
      await page.emulateMedia({ colorScheme: "light" });
      await page.setViewportSize({ width: 390, height: 844 });
      await fill();
      await page.setViewportSize({ width: 390, height: 500 });
      await page.getByRole("textbox", { name: "Across at S3 · Z1 (mm)" }).click();
      await expect(page.getByRole("button", { name: "Fit", exact: true })).toBeInViewport();
      await page.screenshot({ path: resolve(dir, "t15b-readings-390x500-light.png") });
    } finally {
      await page.unroute(`**/api/v1/printers/${id}/calibration/${TRAY}/runs`);
      await page.unroute(`**/api/v1/printers/${id}/calibration/${TRAY}/check-sheets/open*`);
      await page.unroute(`**/api/v1/printers/${id}/calibration/${TRAY}/fit`);
      await page.unroute(`**/api/v1/printers/${id}/calibration`);
      await page.emulateMedia({ colorScheme: "light" });
    }
  });

  test("screenshots: the line naming the type of the kept fit, three states (#220, M8)", async () => {
    test.skip(!process.env.E2E_SHOTS_DIR, "screenshots are an artifact, taken only with E2E_SHOTS_DIR");
    test.setTimeout(120_000);
    const dir = process.env.E2E_SHOTS_DIR!;
    mkdirSync(dir, { recursive: true });
    await setLanguage(page, "en");
    const run = { job_id: "j", retry: false, submitted: true, requested_tray: TRAY, actual_media_source: null, media_source_reported: true, type_key: TYPE, version: 1 };
    const runs = [{ ...run, id: "r12", run_no: 12, kind: "calibration_page", created_at: "2026-11-21T14:20:00Z", check_sheet_id: null, pass_no: null }];
    const fit = {
      values: { offset_x_mm: 0.41, offset_y_mm: -0.66, scale_x: 1.00012, scale_y: 0.99992, rotation_deg: 0.021 },
      residuals_mm: [0.08, 0.12, 0.1],
      flagged: [],
      out_of_range: [],
      suggestion: null,
      suggestion_unavailable: null,
    };
    await page.route(`**/api/v1/printers/${id}/calibration/${TRAY}/runs`, (route) => route.fulfill({ json: runs }));
    await page.route(`**/api/v1/printers/${id}/calibration/${TRAY}/fit`, (route) => route.fulfill({ json: fit }));
    const values = page.getByRole("heading", { name: "Or enter the values directly" }).locator("xpath=..");
    const shoot = async (name: string, width: number) => {
      await values.scrollIntoViewIfNeeded();
      await values.screenshot({ path: resolve(dir, `t16-kept-fit-${name}-${width}.png`) });
    };
    try {
      for (const width of [1280, 390]) {
        await page.setViewportSize({ width, height: width < 500 ? 2200 : 900 });
        await page.goto(`/calibration?printer=${id}&tray=${TRAY}&view=readings&type=${TYPE}`);
        // Typed by hand: no fit yet.
        await expect(page.getByText(/^Typed by hand, saved for /)).toBeVisible();
        await shoot("typed", width);
        await page.getByRole("button", { name: /Readings from print/ }).click();
        await page.getByRole("option", { name: /^#12 / }).click();
        const fields: [string, string][] = [
          ["Across at S1 · Z1 (mm)", "0,4"],
          ["Down at S1 · Z1 (mm)", "0,7"],
          ["Across at S3 · Z1 (mm)", "0,4"],
          ["Down at S3 · Z1 (mm)", "-0,5"],
          ["Across at S1 · Z11 (mm)", "0,6"],
          ["Down at S1 · Z11 (mm)", "0,3"],
        ];
        for (const [label, value] of fields) await page.getByRole("textbox", { name: label }).fill(value);
        await page.getByRole("button", { name: "Fit", exact: true }).click();
        await expect(page.getByText(/^Values from the readings of .*, print #12\.$/)).toBeVisible();
        await shoot("fitted", width);
        // Another type chosen above: the line turns into a warning.
        const box = page.getByRole("combobox", { name: /Sheet type/ });
        await box.click();
        await expect(page.getByRole("listbox")).toBeVisible();
        await page.getByRole("option").filter({ hasNotText: `(${TYPE})` }).first().click();
        await expect(page.getByText(/not the sheet type chosen above\.$/)).toBeVisible();
        await shoot("other-type", width);
      }
    } finally {
      await page.unroute(`**/api/v1/printers/${id}/calibration/${TRAY}/runs`);
      await page.unroute(`**/api/v1/printers/${id}/calibration/${TRAY}/fit`);
    }
  });

  for (const [lang, size] of [
    ["de", { width: 360, height: 780 }],
    ["en", { width: 1280, height: 800 }],
  ] as const) {
    for (const scheme of ["light", "dark"] as const) {
      test(`layout and screenshots, ${lang} at ${size.width} px, ${scheme}`, async () => {
        const testInfo = test.info();
        const de = lang === "de";
        await setLanguage(page, lang);
        await page.emulateMedia({ colorScheme: scheme });
        await page.setViewportSize(size);
        const shot = (name: string) => page.screenshot({ path: testInfo.outputPath(`${name}-${size.width}-${lang}-${scheme}.png`), fullPage: true });

        // Worst case: a printer name and a sheet type that are as long as the hub accepts, and a
        // tray without a correction; the answers are the real ones with the names stretched.
        const long = "Büro-Laserdrucker im Erdgeschoss neben dem Besprechungsraum der Geschäftsführung";
        await page.route("**/api/v1/printers", async (route) => {
          const list = (await (await route.fetch()).json()) as Array<{ id: string; name: string }>;
          await route.fulfill({ json: list.map((p) => (p.id === id ? { ...p, name: long } : p)) });
        });
        await page.route("**/api/v1/sheet-types", async (route) => {
          const list = (await (await route.fetch()).json()) as Array<{ key: string; vendor: string | null }>;
          await route.fulfill({ json: list.map((t) => (t.key === TYPE ? { ...t, vendor: "Zweckform Etiketten und Bürobedarf Vertriebsgesellschaft" } : t)) });
        });
        await openCalibration(page, id);
        await expect(page.getByRole("heading", { level: 2, name: de ? "Schritt 1: Kalibrierseite drucken" : "Step 1: Print the calibration page" })).toBeVisible();
        await chooseType(page);
        expect(await noSideScroll(page)).toBe(true);
        await shot("calibration-empty");

        // A fitted result with a flagged reading, the signs, and the stored values.
        await page.unroute("**/api/v1/printers");
        await page.unroute("**/api/v1/sheet-types");
        await page.route(`**/api/v1/printers/${id}/calibration/${TRAY}/fit`, (route) =>
          route.fulfill({
            json: {
              values: { offset_x_mm: 0.35, offset_y_mm: -0.15, scale_x: 1.0012, scale_y: 0.9995, rotation_deg: 0.03 },
              residuals_mm: [0.05, 0.42, 0.1, 0.02],
              flagged: [1],
              out_of_range: [],
            },
          }),
        );
        await page.reload();
        // The step heading in the page's language is there once the language file has arrived.
        await expect(page.getByRole("heading", { level: 2, name: de ? "Schritt 1: Kalibrierseite drucken" : "Step 1: Print the calibration page" })).toBeVisible();
        await chooseType(page);
        await showTab(page, de ? "Messwerte" : "Readings");
        for (const crossing of ["S1 · Z1", "S3 · Z1", "S1 · Z11", "S3 · Z11"]) {
          await page.getByRole("textbox", { name: new RegExp(`^(Across|Quer) (at|bei) ${crossing} \\(mm\\)$`) }).fill("0,3");
          await page.getByRole("textbox", { name: new RegExp(`^(Down|Abwärts) (at|bei) ${crossing} \\(mm\\)$`) }).fill("-0,2");
        }
        await page.getByRole("button", { name: de ? "Berechnen" : "Fit", exact: true }).click();
        await expect(page.getByRole("table", { name: de ? "Ergebnis der Berechnung" : "Result of the fit" })).toBeVisible();
        // The box around the table scrolls sideways: it is a named region and the keyboard reaches it (axe: scrollable-region-focusable).
        const fitRegion = page.getByRole("region", { name: de ? "Ergebnis der Berechnung" : "Result of the fit" });
        await expect(fitRegion).toHaveAttribute("tabindex", "0");
        await fitRegion.focus();
        await expect(fitRegion).toBeFocused();
        // The flag on the far reading is on screen without scrolling the table sideways.
        const flag = await page.getByText(de ? "wohl falsch abgelesen" : "probable misread", { exact: true }).boundingBox();
        expect(flag, "the flag is rendered").not.toBeNull();
        expect(flag!.x).toBeGreaterThanOrEqual(0);
        expect(flag!.x + flag!.width).toBeLessThanOrEqual(size.width);
        expect(await noSideScroll(page)).toBe(true);
        await shot("calibration-fitted");
        await page.unroute(`**/api/v1/printers/${id}/calibration/${TRAY}/fit`);
      });
    }
  }
});
