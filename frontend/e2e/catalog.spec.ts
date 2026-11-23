// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import type { Locator, Page } from "@playwright/test";
import { MAIN } from "./env";
import { expect, mainKey, signInWith, test } from "./fixtures";

// #335: the sheet-type catalog in the sheet archive, and the links between a type and its sheets.

const KEY = () => ({ "X-Label-Hub-Key": mainKey() });
const setLanguage = (page: Page, lang: "de" | "en") => page.evaluate((l) => localStorage.setItem("lph.lang", l), lang);
const SEEDED_TYPE = "avery-zweckform-l4736rev";
const SEEDED_SHEET = "L4736-0001";

interface TypeRead {
  key: string;
  catalog_key?: string | null;
  usage_state: string;
  id_placement_resolved: string;
  geometry: { page_size: string };
}

/** The sheet type field is a combobox: type the key so its option is at the top, then pick it. */
async function chooseType(scope: Locator, option: RegExp) {
  await scope.getByRole("combobox", { name: /Sheet type/ }).click();
  await expect(scope.page().getByRole("listbox")).toBeVisible();
  await scope.page().keyboard.press("ControlOrMeta+A");
  await scope.page().keyboard.type(option.source);
  await scope.page().getByRole("option", { name: option }).click();
}

test.describe.serial("sheet catalog", () => {
  let page: Page;
  test.beforeAll(async ({ browser }) => {
    page = await (await browser.newContext({ baseURL: MAIN.url })).newPage();
    await page.goto("/");
    await signInWith(page, mainKey());
    await expect(page.getByRole("navigation", { name: /^(Main|Tabs)$/ }).first()).toBeVisible();
  });
  test.afterAll(async () => {
    await page.context().close();
  });

  test("fresh install → add from catalog → create a sheet", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 1280, height: 800 });
    // A type the demo hub never used stands in for a fresh installation: it goes, and comes back from the catalog.
    const unused = (await (await page.request.get("/api/v1/sheet-types?usage_state=unused", { headers: KEY() })).json()) as TypeRead[];
    const type = unused.find((t) => t.catalog_key === t.key && t.id_placement_resolved === "margin" && t.geometry.page_size === "A4");
    expect(type, "an unused catalog type with its code in the margin").toBeDefined();
    const key = type!.key;
    expect((await page.request.delete(`/api/v1/sheet-types/${key}`, { headers: KEY() })).status()).toBe(204);

    await page.goto(`/sheets/archive?tab=catalog&q=${key}`);
    const entry = page.getByRole("article", { name: key });
    await expect(entry).toBeVisible();
    await entry.getByRole("button", { name: "Add" }).click();
    await expect(entry.getByText("Installed", { exact: true })).toBeVisible();

    await page.getByRole("tab", { name: /Installed/ }).click();
    await page.goto(`/sheets/archive?type=${key}`);
    await expect(page.getByRole("article", { name: key })).toBeVisible();

    await page.goto("/sheets");
    await page.getByRole("button", { name: "New sheet" }).click();
    const dialog = page.getByRole("dialog", { name: "Add a sheet" });
    await chooseType(dialog, new RegExp(key));
    await dialog.getByRole("button", { name: "Add sheet" }).click();
    const added = dialog.getByRole("status").filter({ hasText: /^Sheet .+ was added\.$/ });
    await expect(added).toBeVisible();
    const addedText = (await added.textContent())!;
    await dialog.getByRole("button", { name: "Close" }).filter({ hasText: "Close" }).click();

    const after = (await (await page.request.get(`/api/v1/sheet-types/${key}`, { headers: KEY() })).json()) as TypeRead & { catalog_key: string };
    expect(after.catalog_key).toBe(key);
    expect(after.usage_state).toBe("in_use");

    // The new sheet has every cell free: left active, the planner of later specs would pick it over the seeded sheets.
    const code = /^Sheet (.+) was added\.$/.exec(addedText)![1]!;
    expect((await page.request.post(`/api/v1/sheets/${code}/discard`, { headers: KEY() })).status()).toBe(200);
  });

  test("type ↔ sheets links both ways", async () => {
    await setLanguage(page, "en");
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto(`/sheets/archive?type=${SEEDED_TYPE}`);
    const sheets = page.getByRole("list", { name: "Its sheets" });
    await expect(sheets.getByRole("link", { name: SEEDED_SHEET })).toBeVisible();
    await expect(page.getByRole("link", { name: "Show in the sheets list" })).toHaveAttribute("href", `/sheets?type=${SEEDED_TYPE}`);

    // Type → sheet.
    await sheets.getByRole("link", { name: SEEDED_SHEET }).click();
    await expect(page).toHaveURL(new RegExp(`/sheets/${SEEDED_SHEET}$`));
    await expect(page.getByRole("heading", { level: 1, name: SEEDED_SHEET })).toBeVisible();

    // Sheet → type, with the version the sheet was made with.
    await page.getByRole("link", { name: /\(v\d+\)/ }).first().click();
    await expect(page).toHaveURL(new RegExp(`/sheets/archive\\?type=${SEEDED_TYPE}$`));
    await expect(page.getByRole("article", { name: SEEDED_TYPE })).toBeVisible();

    // Type → the sheets page filtered by it, and from a row back to the type.
    await page.getByRole("link", { name: "Show in the sheets list" }).click();
    await expect(page).toHaveURL(new RegExp(`/sheets\\?type=${SEEDED_TYPE}$`));
    const row = page.getByRole("row").filter({ has: page.getByRole("link", { name: SEEDED_SHEET }) });
    await row.getByRole("link", { name: /\(v\d+\)/ }).click();
    await expect(page.getByRole("article", { name: SEEDED_TYPE })).toBeVisible();
  });
});
