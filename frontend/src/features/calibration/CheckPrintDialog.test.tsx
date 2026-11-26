// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { respond } from "../test-api";
import { BASE, body, button, JOB, LAYOUT, only, open, P, resetBetweenTests } from "./CalibrationPage.kit";

resetBetweenTests();
// A whole app render per test, as in the CalibrationPage files: the first one alone can pass 5 s when
// the folder runs side by side.
vi.setConfig({ testTimeout: 15_000 });

const SHEET = "eeeeeeee-0000-4000-8000-000000000001";
const RUN12 = "eeeeeeee-0000-4000-8000-000000000012";
const CHECK = "Print the check page (with correction)";
const post = { [`POST ${BASE}/main/page`]: respond(202, { job_id: JOB }) };
const openSheet = (slots = 2) => ({
  check_sheet_id: SHEET,
  passes_total: 3,
  passes_used: 1,
  next_pass: 2,
  run_line_slots: slots,
  passes: [{ pass_no: 1, run_no: 12, run_id: RUN12 }],
  passes_without_run_line: [2, 3].filter((p) => p - 2 >= slots),
});

async function dialog() {
  await userEvent.click(await button(CHECK));
  const d = await screen.findByRole("dialog", {
    name: "Print the check page",
  });
  return within(d);
}

describe("the check print dialog (design 7.2)", () => {
  it("dialog offers pass 2 of 3 on the same sheet", async () => {
    const { calls } = await open({
      ...post,
      [`GET ${BASE}/main/page/layout`]: {
        ...LAYOUT,
        passes_total: 3,
        run_line_slots: 2,
      },
      [`GET ${BASE}/main/check-sheets/open`]: openSheet(),
    });
    const d = await dialog();
    const same = await d.findByRole("radio", {
      name: /Print on the same sheet \(pass 2 of 3\)/,
    });
    expect(same).toBeChecked();
    expect(d.getByText("Put the sheet of run #12 back into the tray. The new crosses land beside the earlier ones.")).toBeVisible();
    expect(d.queryByRole("note")).toBeNull(); // two slots: nothing to warn about
    await userEvent.click(d.getByRole("button", { name: "Print pass 2 of 3" }));
    expect(body(only(calls, "POST", "/page")[0])).toEqual({
      type: "L4736REV",
      version: 1,
      corrected: true,
      check_sheet: "next",
    });
  });

  it("a new sheet can be chosen instead of the open one", async () => {
    const { calls } = await open({
      ...post,
      [`GET ${BASE}/main/page/layout`]: {
        ...LAYOUT,
        passes_total: 3,
        run_line_slots: 2,
      },
      [`GET ${BASE}/main/check-sheets/open`]: openSheet(),
    });
    const d = await dialog();
    await userEvent.click(await d.findByRole("radio", { name: /New sheet/ }));
    await userEvent.click(d.getByRole("button", { name: "Print pass 1 of 3" }));
    expect(body(only(calls, "POST", "/page")[0])).toMatchObject({
      check_sheet: "new",
    });
  });

  it("dialog warns later passes print without their run line", async () => {
    const { calls } = await open({
      ...post,
      [`GET ${BASE}/main/page/layout`]: {
        ...LAYOUT,
        passes_total: 3,
        run_line_slots: 0,
      },
    });
    const d = await dialog();
    expect(await d.findByText("This sheet takes 3 check passes. This is pass 1, on a fresh sheet: keep the sheet for the next check.")).toBeVisible();
    expect(
      within(d.getByRole("note", { name: "Before you print" })).getByText("Later check passes on this sheet will print without their run line."),
    ).toBeVisible();
    await userEvent.click(d.getByRole("button", { name: "Print pass 1 of 3" }));
    expect(body(only(calls, "POST", "/page")[0])).toMatchObject({
      check_sheet: "new",
    });
  });

  it("the later pass itself is warned that it prints without its run line", async () => {
    await open({
      [`GET ${BASE}/main/page/layout`]: {
        ...LAYOUT,
        page_codes: 1,
        passes_total: 3,
        run_line_slots: 0,
      },
      [`GET ${BASE}/main/check-sheets/open`]: openSheet(0),
    });
    const d = await dialog();
    const note = await d.findByRole("note", { name: "Before you print" });
    // No slot at all: pass 2 itself prints without its run line, and so will pass 3.
    expect(within(note).getByText("This pass prints without its run line.")).toBeVisible();
    expect(within(note).getByText("Pass 3 on this sheet will print without its run line.")).toBeVisible();
    // Pass 2 prints no page codes: their count is no news for it.
    expect(within(note).queryByText(/page codes/)).toBeNull();
  });

  it("dialog says the sheet type leaves room for 2 of the 4 page codes", async () => {
    await open({
      [`GET ${BASE}/main/page/layout`]: { ...LAYOUT, page_codes: 2 },
    });
    const d = await dialog();
    expect(await d.findByText("This sheet type leaves room for 2 of the 4 page codes on this printer.")).toBeVisible();
    expect(d.getByText("This sheet takes one check pass: the check page uses up the sheet.")).toBeVisible();
  });

  it("dialog says the page prints without margin text", async () => {
    await open({
      [`GET ${BASE}/main/page/layout`]: {
        ...LAYOUT,
        margin_text: { top: "omitted", bottom: "omitted" },
      },
    });
    const d = await dialog();
    expect(await d.findByText("This page prints without margin text; the page codes carry the run and correction.")).toBeVisible();
  });

  it("dialog warns that a page without page codes and margin text names its run nowhere", async () => {
    await open({
      [`GET ${BASE}/main/page/layout`]: {
        ...LAYOUT,
        page_codes: 0,
        margin_text: { top: "omitted", bottom: "omitted" },
      },
    });
    const d = await dialog();
    const note = await d.findByRole("note", { name: "Before you print" });
    expect(within(note).getByText(/This page names its run nowhere/)).toBeVisible();
    // The weaker sentences would contradict it: the page codes carry nothing here.
    expect(within(note).queryByText(/the page codes carry/)).toBeNull();
  });

  it("with one slot on a three-pass sheet, pass 2 is told only that pass 3 lacks its run line", async () => {
    await open({
      [`GET ${BASE}/main/page/layout`]: { ...LAYOUT, passes_total: 3, run_line_slots: 1 },
      [`GET ${BASE}/main/check-sheets/open`]: openSheet(1),
    });
    const d = await dialog();
    const note = await d.findByRole("note", { name: "Before you print" });
    // Pass 2 gets the one slot: only pass 3 prints without its line.
    expect(within(note).getByText("Pass 3 on this sheet will print without its run line.")).toBeVisible();
    expect(within(note).queryByText(/Later check passes/)).toBeNull();
    expect(within(note).queryByText("This pass prints without its run line.")).toBeNull();
  });

  it("with one slot on a three-pass sheet, pass 3 is told that it prints without its run line", async () => {
    await open({
      [`GET ${BASE}/main/page/layout`]: { ...LAYOUT, passes_total: 3, run_line_slots: 1 },
      [`GET ${BASE}/main/check-sheets/open`]: { ...openSheet(1), passes_used: 2, next_pass: 3 },
    });
    const d = await dialog();
    const note = await d.findByRole("note", { name: "Before you print" });
    expect(within(note).getByText("This pass prints without its run line.")).toBeVisible();
    expect(within(note).queryByText(/Pass 3 on this sheet/)).toBeNull();
  });

  it("a new three-pass sheet with one slot says that pass 3 will lack its run line", async () => {
    await open({ [`GET ${BASE}/main/page/layout`]: { ...LAYOUT, passes_total: 3, run_line_slots: 1 } });
    const d = await dialog();
    const note = await d.findByRole("note", { name: "Before you print" });
    expect(within(note).getByText("Pass 3 on this sheet will print without its run line.")).toBeVisible();
    expect(within(note).queryByText(/Later check passes/)).toBeNull();
  });

  it("pass 2 is warned when the current correction moves its reserved slot off the page", async () => {
    // Pass 1 reserved both slots; the tray's correction changed since and pass 2's slot no longer fits.
    await open({
      [`GET ${BASE}/main/page/layout`]: { ...LAYOUT, passes_total: 3, run_line_slots: 2 },
      [`GET ${BASE}/main/check-sheets/open`]: { ...openSheet(2), run_line_slots: 1, passes_without_run_line: [2] },
    });
    const d = await dialog();
    const note = await d.findByRole("note", { name: "Before you print" });
    expect(within(note).getByText("This pass prints without its run line.")).toBeVisible();
    expect(within(note).queryByText(/Pass 3 on this sheet/)).toBeNull();
  });

  it("the calibration page says the same before it is printed, without a dialog", async () => {
    await open(
      {
        [`GET ${BASE}/bypass/page/layout`]: {
          ...LAYOUT,
          page_codes: 2,
          margin_text: { top: "reduced", bottom: "omitted" },
        },
      },
      undefined,
      `/calibration?printer=${P}&tray=bypass`,
    );
    const note = await screen.findByRole("note", {
      name: "Before you print",
    });
    expect(within(note).getByText("This sheet type leaves room for 2 of the 4 page codes on this printer.")).toBeVisible();
    expect(within(note).getByText("This page prints without its bottom margin text (the correction); the page codes carry it.")).toBeVisible();
    // Operator rule (25.11.2026): only a block that is left out is warned about. The top block here is
    // `reduced` (printed smaller, still complete), which is no note at all.
    expect(within(note).getAllByRole("listitem")).toHaveLength(2);
    expect(within(note).queryByText(/top margin/)).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("a full sheet is said in words", async () => {
    await open({
      [`GET ${BASE}/main/page/layout`]: { ...LAYOUT, passes_total: 3 },
      [`GET ${BASE}/main/check-sheets/open`]: openSheet(),
      [`POST ${BASE}/main/page`]: respond(409, {
        detail: {
          error_code: "check_sheet_full",
          error_message: "full",
          error_detail: null,
        },
      }),
    });
    const d = await dialog();
    await userEvent.click(await d.findByRole("button", { name: "Print pass 2 of 3" }));
    expect(await screen.findByText("That check sheet has no free pass left. Print on a new sheet.")).toBeVisible();
  });
});
