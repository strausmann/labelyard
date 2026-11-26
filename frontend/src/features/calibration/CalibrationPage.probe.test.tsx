// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { respond } from "../test-api";
import { BASE, body, button, JOB, only, open, P, resetBetweenTests } from "./CalibrationPage.kit";

resetBetweenTests();
vi.setConfig({ testTimeout: 15_000 });

describe("the probe print next to the calibration page (#241)", () => {
  it("opens with the page's printer, tray and sheet type and prints there", async () => {
    const { calls } = await open({ [`POST ${BASE}/bypass/probe`]: respond(202, { job_id: JOB, run_no: 3 }) }, undefined, `/calibration?printer=${P}&tray=bypass&view=print`);
    await userEvent.click(await button("Probe print on plain paper"));
    const d = within(await screen.findByRole("dialog", { name: "Probe print on plain paper" }));
    // The bypass tray of this page, not the printer's default tray.
    expect(await d.findByText("This tray has no correction stored: the page is printed as it comes.")).toBeVisible();
    await userEvent.click(d.getByRole("button", { name: "Print probe page" }));
    await waitFor(() => expect(only(calls, "POST", "/probe")).toHaveLength(1));
    expect(only(calls, "POST", "/probe")[0]!.path).toBe(`${BASE}/bypass/probe`);
    expect(body(only(calls, "POST", "/probe")[0])).toEqual({ type: "L4736REV", version: 1, grid: false });
  });
});
