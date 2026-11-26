// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { components } from "../../api/schema.gen";
import { GEOMETRY_L4736, SHEET_TYPE } from "../test-api";
import { BASE, body, button, enter, enterAll, FIT, none, only, open, PRINT_SESSION, resetBetweenTests, stored, typeB } from "./CalibrationPage.kit";

vi.setConfig({ testTimeout: 20_000 });
resetBetweenTests();

type Run = components["schemas"]["CalibrationRunRead"];

const run = (over: Partial<Run>): Run => ({
  id: "run-x",
  run_no: 1,
  kind: "calibration_page",
  type_key: "L4736REV",
  version: 1,
  created_at: "2026-11-21T14:20:00Z",
  requested_tray: "main",
  actual_media_source: null,
  media_source_reported: true,
  retry: false,
  submitted: true,
  job_id: "dddddddd-0000-4000-8000-000000000012",
  check_sheet_id: null,
  pass_no: null,
  ...over,
});

const SHEET_ID = "eeeeeeee-0000-4000-8000-000000000001";
// Newest first, as the server lists them.
const RUNS: Run[] = [
  run({ id: "run-14", run_no: 14, kind: "check_page", created_at: "2026-11-21T15:10:00Z", check_sheet_id: SHEET_ID, pass_no: 2 }),
  run({ id: "run-13", run_no: 13, kind: "check_page", created_at: "2026-11-21T14:50:00Z", check_sheet_id: SHEET_ID, pass_no: 1 }),
  run({ id: "run-other", run_no: 13, kind: "calibration_page", type_key: "OTHERTYPE", version: 3, created_at: "2026-11-21T14:40:00Z" }),
  run({ id: "run-12", run_no: 12, kind: "calibration_page", created_at: "2026-11-21T14:20:00Z" }),
  run({ id: "run-9", run_no: 9, kind: "calibration_page", created_at: "2026-11-20T09:00:00Z" }),
];
const OPEN_SHEET = {
  check_sheet_id: SHEET_ID,
  next_pass: 3,
  passes: [
    { pass_no: 1, run_id: "run-13", run_no: 13 },
    { pass_no: 2, run_id: "run-14", run_no: 14 },
  ],
  passes_total: 3,
  passes_used: 2,
  run_line_slots: 2,
  passes_without_run_line: [],
};
const withRuns = (extra: Record<string, unknown> = {}) => ({
  [`GET ${BASE}/main/runs`]: RUNS,
  [`GET ${BASE}/main/check-sheets/open`]: OPEN_SHEET,
  ...extra,
});

const across = (crossing: string) => `Across at ${crossing} (mm)`;
const down = (crossing: string) => `Down at ${crossing} (mm)`;
const card = (n: number) => screen.getByRole("group", { name: `Reading ${n}` });

/** The first three suggested crossings of L4736REV, with plain values. */
async function fillThreePlain() {
  await enterAll([
    [across("S1 · Z1"), "0,2"],
    [down("S1 · Z1"), "0,1"],
    [across("S3 · Z1"), "0,4"],
    [down("S3 · Z1"), "0"],
    [across("S1 · Z11"), "0,6"],
    [down("S1 · Z11"), "0,3"],
  ]);
}

async function chooseRun(name: RegExp) {
  await userEvent.click(await screen.findByRole("button", { name: /Readings from print/ }));
  await userEvent.click(await screen.findByRole("option", { name }));
}

describe("guided readings", () => {
  it("typed value snaps to 0.1 with a hint", async () => {
    const { calls } = await open({ [`POST ${BASE}/main/fit`]: FIT });
    await enter(across("S1 · Z1"), "0,43");
    expect(await within(card(1)).findByText("You typed 0,43. Readings snap to 0.1 mm, so it is saved as 0.4 mm.")).toBeVisible();
    // Counter-test: a value on the grid says nothing.
    await enter(across("S3 · Z1"), "0,4");
    expect(within(card(2)).queryByText(/Readings snap to 0.1 mm/)).toBeNull();
    await enterAll([
      [down("S1 · Z1"), "0"],
      [down("S3 · Z1"), "0"],
      [across("S1 · Z11"), "0,1"],
      [down("S1 · Z11"), "0"],
    ]);
    await userEvent.click(await button("Fit"));
    await waitFor(() => expect(only(calls, "POST", "/fit").length).toBeGreaterThan(0));
    expect(body(only(calls, "POST", "/fit").at(-1)).readings[0].across_mm).toBe(0.4);
  });

  it('"-0,4" and "0.4" with left give the same value', async () => {
    const { calls } = await open({ [`POST ${BASE}/main/fit`]: FIT });
    await enter(across("S1 · Z1"), "-0,4");
    // The minus moved into the direction picker; the field keeps the size.
    expect(within(card(1)).getByRole("radio", { name: "Left of the gap" })).toBeChecked();
    expect(screen.getByLabelText(across("S1 · Z1"))).toHaveValue("0,4");
    await userEvent.click(within(card(2)).getByRole("radio", { name: "Left of the gap" }));
    await enter(across("S3 · Z1"), "0.4");
    // Counter-test: the same size to the right stays positive.
    await enterAll([
      [down("S1 · Z1"), "0"],
      [down("S3 · Z1"), "0"],
      [across("S1 · Z11"), "0.4"],
      [down("S1 · Z11"), "-0,2"],
    ]);
    expect(within(card(3)).getByRole("radio", { name: "Above the cut" })).toBeChecked();
    await userEvent.click(await button("Fit"));
    await waitFor(() => expect(only(calls, "POST", "/fit").length).toBeGreaterThan(0));
    const readings = body(only(calls, "POST", "/fit").at(-1)).readings;
    expect(readings[0].across_mm).toBe(-0.4);
    expect(readings[1].across_mm).toBe(-0.4);
    expect(readings[2].across_mm).toBe(0.4);
    expect(readings[2].down_mm).toBe(-0.2);
  });

  it("outside ±2.0 shows the warning text", async () => {
    await open();
    const warning = "The print is off by more than the scale shows (±2.0 mm). Check the tray, the sheet type or the printer.";
    await enter(across("S1 · Z1"), "2,6");
    expect(await within(card(1)).findByText(warning)).toBeVisible();
    // Counter-test: the end of the scale itself is still on it.
    await enter(across("S1 · Z1"), "2,0");
    await waitFor(() => expect(within(card(1)).queryByText(warning)).toBeNull());
    await enter(down("S3 · Z1"), "-2,1");
    expect(await within(card(2)).findByText(warning)).toBeVisible();
  });

  it("crossing select limited to the grid and accepts every S and Z line", async () => {
    const { calls } = await open({ [`POST ${BASE}/main/fit`]: FIT });
    await screen.findByRole("group", { name: "Reading 1" });
    await userEvent.click(within(card(1)).getByRole("button", { name: /Gap S of reading 1/ }));
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["S1", "S2", "S3"]);
    await userEvent.click(screen.getByRole("option", { name: "S2" }));
    await userEvent.click(within(card(1)).getByRole("button", { name: /Gap Z of reading 1/ }));
    const zs = (await screen.findAllByRole("option")).map((o) => o.textContent);
    // A 4 x 12 sheet has gaps S1-S3 and Z1-Z11, and nothing past them.
    expect(zs).toEqual(Array.from({ length: 11 }, (_, i) => `Z${i + 1}`));
    await userEvent.click(screen.getByRole("option", { name: "Z6" }));
    await enterAll([
      [across("S2 · Z6"), "0,1"],
      [down("S2 · Z6"), "0,1"],
      [across("S3 · Z1"), "0,1"],
      [down("S3 · Z1"), "0,1"],
      [across("S3 · Z11"), "0,1"],
      [down("S3 · Z11"), "0,1"],
    ]);
    await userEvent.click(await button("Fit"));
    await waitFor(() => expect(only(calls, "POST", "/fit").length).toBeGreaterThan(0));
    const crossings = body(only(calls, "POST", "/fit").at(-1)).readings.map((r: { col_gap: number; row_gap: number }) => [r.col_gap, r.row_gap]);
    expect(crossings).toEqual([
      [2, 6],
      [3, 1],
      [3, 11],
    ]);
  });

  it("residual over 0.3 mm marked as probable misread", async () => {
    await open({ [`POST ${BASE}/main/fit`]: { ...FIT, residuals_mm: [0.05, 0.42, 0.1], flagged: [1] } });
    await fillThreePlain();
    await userEvent.click(await button("Fit"));
    const table = await screen.findByRole("table", { name: "Result of the fit" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(rows[1]).toHaveTextContent("S3 · Z1");
    expect(rows[1]).toHaveTextContent("0.42 mm");
    expect(rows[1]).toHaveTextContent("probable misread");
    // Counter-test: the readings that fit say so, and are not marked.
    expect(rows[0]).toHaveTextContent("0.05 mm");
    expect(rows[0]).toHaveTextContent("fits");
    expect(rows[0]).not.toHaveTextContent("probable misread");
    expect(rows[2]).not.toHaveTextContent("probable misread");
  });

  it("run picker defaults to the newest calibration run and lists the open check sheet's passes", async () => {
    const { calls } = await open(withRuns({ [`POST ${BASE}/main/fit`]: FIT }));
    const picker = await screen.findByRole("button", { name: /Readings from print/ });
    // The newest calibration page of this type: #12, not the other type's #13 and not a check print.
    await waitFor(() => expect(picker).toHaveTextContent(/^#12 · calibration page · /));
    expect(screen.getByText(/Printed from Main tray, as requested\./)).toBeVisible();
    await userEvent.click(picker);
    const options = (await screen.findAllByRole("option")).map((o) => o.textContent);
    expect(options).toEqual([
      expect.stringMatching(/^#12 · calibration page · /),
      expect.stringMatching(/^#9 · calibration page · /),
      "Check sheet of run #13 — pass 1, run #13",
      "Check sheet of run #13 — pass 2, run #14",
    ]);
    await userEvent.click(screen.getByRole("option", { name: "Check sheet of run #13 — pass 2, run #14" }));
    await fillThreePlain();
    await userEvent.click(await button("Fit"));
    await waitFor(() => expect(only(calls, "POST", "/fit").length).toBeGreaterThan(0));
    expect(body(only(calls, "POST", "/fit").at(-1))).toMatchObject({ type: "L4736REV", version: 1, run_id: "run-14" });
  });

  it("save sends run_id and readings", async () => {
    // The type has moved on to version 2; the run was printed for version 1, so the fit and the save name version 1.
    const typeV2 = { ...SHEET_TYPE, current_version: 2 };
    const { calls } = await open(
      withRuns({
        "GET /api/v1/sheet-types": [typeV2],
        [`POST ${BASE}/main/fit`]: FIT,
        [`PUT ${BASE}/main`]: stored,
      }),
    );
    await waitFor(() => expect(screen.getByRole("button", { name: /Readings from print/ })).toHaveTextContent(/^#12/));
    await fillThreePlain();
    await userEvent.click(await button("Fit"));
    expect(await screen.findByText("From the readings of print #12.")).toBeVisible();
    await userEvent.click(await button("Save for this printer and tray"));
    await screen.findByText("Saved. New prints use this correction.");
    const put = body(only(calls, "PUT")[0]);
    expect(put.run_id).toBe("run-12");
    expect(put.measured_on).toEqual({ type_key: "L4736REV", version: 1 });
    expect(put.readings).toEqual([
      { col_gap: 1, row_gap: 1, across_mm: 0.2, down_mm: 0.1 },
      { col_gap: 3, row_gap: 1, across_mm: 0.4, down_mm: 0 },
      { col_gap: 1, row_gap: 11, across_mm: 0.6, down_mm: 0.3 },
    ]);
    expect(body(only(calls, "POST", "/fit").at(-1))).toMatchObject({ version: 1, run_id: "run-12" });
  });

  it("direct entry keeps six decimals for scale and rotation and is never snapped", async () => {
    const { calls } = await open(withRuns({ [`PUT ${BASE}/main`]: stored }));
    await enterAll([
      ["Scale X", "0,987533"],
      ["Rotation (°)", "0,065216"],
    ]);
    await userEvent.click(await button("Save for this printer and tray"));
    await screen.findByText("Saved. New prints use this correction.");
    const put = body(only(calls, "PUT")[0]);
    expect(put.scale_x).toBe(0.987533);
    expect(put.rotation_deg).toBe(0.065216);
    // Typed values are no fit: no run and no readings travel with them.
    expect(put.run_id).toBeUndefined();
    expect(put.readings).toBeUndefined();
  });

  it("suggestion card offers apply as new version to admins only", async () => {
    const suggesting = {
      ...withRuns({
        "GET /api/v1/sheet-types": [SHEET_TYPE, typeB],
        "GET /api/v1/sheet-types/L4736REV": { ...SHEET_TYPE, versions: [{ version: 1, change_note: null, created_at: null, geometry: GEOMETRY_L4736, geometry_hash: "a", sheets: 1 }] },
        [`GET ${BASE}`]: [{ ...stored, measured_on: { type_key: "OTHERTYPE", version: 3 } }, none],
        [`POST ${BASE}/main/fit`]: {
          ...FIT,
          residuals_mm: [0.05, 0.1, 0.1],
          flagged: [],
          suggestion: { pitch_x_hmm: 4820, pitch_y_hmm: null, margin_left_hmm: null, margin_top_hmm: null, uneven: false },
          suggestion_unavailable: null,
        },
      }),
    };
    for (const [session, admin] of [
      [undefined, true],
      [PRINT_SESSION, false],
    ] as const) {
      const view = await open(suggesting, session);
      await chooseRun(/pass 2, run #14/);
      await fillThreePlain();
      await userEvent.click(await button("Fit"));
      const suggestion = await screen.findByRole("region", { name: "Geometry suggestion" });
      expect(within(suggestion).getByText("48.50 → 48.20 mm")).toBeVisible();
      expect(within(suggestion).getByRole("link", { name: /Fill the feedback issue/ })).toBeVisible();
      if (admin) expect(within(suggestion).getByRole("button", { name: "Apply as new version" })).toBeVisible();
      else expect(within(suggestion).queryByRole("button", { name: "Apply as new version" })).toBeNull();
      view.unmount();
    }
  });
});
