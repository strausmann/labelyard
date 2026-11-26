// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { components } from "../../api/schema.gen";
import { apiErrorBody, GEOMETRY_L4736, respond, SHEET_TYPE } from "../test-api";
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
  // Another type's page is the newest run of the tray: it never belongs to these readings.
  run({ id: "run-other", run_no: 16, kind: "calibration_page", type_key: "OTHERTYPE", version: 3, created_at: "2026-11-21T15:40:00Z" }),
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

  it("run picker defaults to the newest run of this type and version and lists the open check sheet's passes", async () => {
    // Operator ruling (2026-11-26): the default is the newest run of this type and version, a check pass included, not
    // only the newest calibration page (the spec's literal default): check readings are entered right after the check.
    const { calls } = await open(withRuns({ [`POST ${BASE}/main/fit`]: FIT }));
    const picker = await screen.findByRole("button", { name: /Readings from print/ });
    // Run #14 (pass 2) is the newest of L4736REV v1; the other type's #16 is newer still and is not offered.
    await waitFor(() => expect(picker).toHaveTextContent("Check sheet of run #13 — pass 2, run #14"));
    await userEvent.click(picker);
    const options = (await screen.findAllByRole("option")).map((o) => o.textContent);
    expect(options).toEqual([
      expect.stringMatching(/^#12 · calibration page · /),
      expect.stringMatching(/^#9 · calibration page · /),
      "Check sheet of run #13 — pass 1, run #13",
      "Check sheet of run #13 — pass 2, run #14",
    ]);
    await userEvent.click(screen.getByRole("option", { name: /^#12 · calibration page/ }));
    expect(screen.getByText(/Printed from Main tray, as requested\./)).toBeVisible();
    await fillThreePlain();
    await userEvent.click(await button("Fit"));
    await waitFor(() => expect(only(calls, "POST", "/fit").length).toBeGreaterThan(0));
    expect(body(only(calls, "POST", "/fit").at(-1))).toMatchObject({ type: "L4736REV", version: 1, run_id: "run-12" });
  });

  it("save sends run_id and readings", async () => {
    // The type has moved on to version 2; the run was printed for version 1, so the fit and the save name version 1.
    const typeV2 = { ...SHEET_TYPE, current_version: 2 };
    const { calls } = await open(
      withRuns({
        "GET /api/v1/sheet-types": [typeV2],
        "GET /api/v1/sheet-types/L4736REV": { ...typeV2, versions: [{ version: 1, change_note: null, created_at: null, geometry: GEOMETRY_L4736, geometry_hash: "a", sheets: 1 }] },
        [`POST ${BASE}/main/fit`]: FIT,
        [`PUT ${BASE}/main`]: stored,
      }),
    );
    await chooseRun(/^#12 · calibration page/);
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

  it("a suggestion is only offered for the readings and the print it was computed from", async () => {
    await open(
      withRuns({
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
    );
    await chooseRun(/pass 2, run #14/);
    await fillThreePlain();
    await userEvent.click(await button("Fit"));
    expect(await screen.findByRole("button", { name: "Apply as new version" })).toBeVisible();
    // A corrected reading: the old suggestion is gone at once, not applied under the new readings.
    await enter(across("S1 · Z1"), "0,3");
    expect(screen.queryByRole("button", { name: "Apply as new version" })).toBeNull();
    // Another print: the same.
    await waitFor(() => expect(screen.getByRole("button", { name: "Apply as new version" })).toBeVisible(), { timeout: 5_000 });
    await chooseRun(/pass 1, run #13/);
    expect(screen.queryByRole("button", { name: "Apply as new version" })).toBeNull();
  });

  it("the crossings follow the grid of the version the chosen print was made for", async () => {
    const sixRows = { ...GEOMETRY_L4736, rows: 6 };
    const typeV2 = { ...SHEET_TYPE, current_version: 2 };
    const { calls } = await open(
      withRuns({
        "GET /api/v1/sheet-types": [typeV2],
        "GET /api/v1/sheet-types/L4736REV": {
          ...typeV2,
          versions: [
            { version: 2, change_note: null, created_at: null, geometry: GEOMETRY_L4736, geometry_hash: "b", sheets: 0 },
            { version: 1, change_note: null, created_at: null, geometry: sixRows, geometry_hash: "a", sheets: 1 },
          ],
        },
        [`POST ${BASE}/main/fit`]: FIT,
      }),
    );
    // Run #12 was printed for version 1, a 4 x 6 sheet: Z goes to 5, and the suggested crossings sit on that grid.
    expect(await screen.findByLabelText(across("S1 · Z5"), {}, { timeout: 5_000 })).toBeVisible();
    await userEvent.click(within(card(1)).getByRole("button", { name: /Gap Z of reading 1/ }));
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["Z1", "Z2", "Z3", "Z4", "Z5"]);
    await userEvent.keyboard("{Escape}");
    await enterAll([
      [across("S1 · Z1"), "0,1"],
      [down("S1 · Z1"), "0,1"],
      [across("S3 · Z1"), "0,1"],
      [down("S3 · Z1"), "0,1"],
      [across("S1 · Z5"), "0,1"],
      [down("S1 · Z5"), "0,1"],
    ]);
    await userEvent.click(await button("Fit"));
    await waitFor(() => expect(only(calls, "POST", "/fit").length).toBeGreaterThan(0));
    const sent = body(only(calls, "POST", "/fit").at(-1));
    expect(sent.version).toBe(1);
    expect(Math.max(...sent.readings.map((r: { row_gap: number }) => r.row_gap))).toBe(5);
  });

  it("the passes of a check sheet stay choosable after its last pass closed it", async () => {
    const lastPass = run({ id: "run-15", run_no: 15, kind: "check_page", created_at: "2026-11-21T15:30:00Z", check_sheet_id: SHEET_ID, pass_no: 3 });
    const { calls } = await open({
      [`GET ${BASE}/main/runs`]: [lastPass, ...RUNS],
      // The sheet took its third pass and is closed: there is no open check sheet any more.
      [`GET ${BASE}/main/check-sheets/open`]: () => new Response("null", { status: 200, headers: { "Content-Type": "application/json" } }),
      [`POST ${BASE}/main/fit`]: FIT,
    });
    const picker = await screen.findByRole("button", { name: /Readings from print/ });
    // The last pass is the newest run of the type: it is the default.
    await waitFor(() => expect(picker).toHaveTextContent("Check sheet of run #13 — pass 3, run #15"));
    await userEvent.click(picker);
    const options = (await screen.findAllByRole("option")).map((o) => o.textContent);
    expect(options).toContain("Check sheet of run #13 — pass 3, run #15");
    expect(options).toContain("Check sheet of run #13 — pass 1, run #13");
    await userEvent.click(screen.getByRole("option", { name: "Check sheet of run #13 — pass 3, run #15" }));
    await fillThreePlain();
    await userEvent.click(await button("Fit"));
    await waitFor(() => expect(only(calls, "POST", "/fit").length).toBeGreaterThan(0));
    expect(body(only(calls, "POST", "/fit").at(-1)).run_id).toBe("run-15");
  });

  it("check readings are never filled in as a correction, and a save after them sends no run and no readings", async () => {
    const { calls } = await open(withRuns({ [`POST ${BASE}/main/fit`]: FIT, [`PUT ${BASE}/main`]: stored }));
    await chooseRun(/pass 2, run #14/);
    await fillThreePlain();
    await userEvent.click(await button("Fit"));
    expect(await screen.findByText(/These are check readings: they show what is left with the stored correction/)).toBeVisible();
    // The form keeps the stored values (0.4), not the fit's 0.35.
    expect(screen.getByRole("textbox", { name: "Offset X (mm)" })).toHaveValue("0.4");
    expect(screen.queryByText("The values are filled in below. Nothing is saved yet.")).toBeNull();
    expect(screen.queryByText(/From the readings of print/)).toBeNull();
    await userEvent.click(await button("Save for this printer and tray"));
    await screen.findByText("Saved. New prints use this correction.");
    const put = body(only(calls, "PUT")[0]);
    expect(put.run_id).toBeUndefined();
    expect(put.readings).toBeUndefined();
  });

  it("says in words when the hub refuses a check print's run as a correction", async () => {
    await open(
      withRuns({
        [`POST ${BASE}/main/fit`]: FIT,
        [`PUT ${BASE}/main`]: () => respond(422, apiErrorBody("run_is_a_check_print", "No.")),
      }),
    );
    await chooseRun(/^#12 · calibration page/);
    await fillThreePlain();
    await userEvent.click(await button("Fit"));
    await screen.findByText("From the readings of print #12.");
    await userEvent.click(await button("Save for this printer and tray"));
    expect(
      await screen.findByText("These readings come from a check print. They show what is left with the stored correction and are never saved as a correction."),
    ).toBeVisible();
  });

  it("the print is pinned once a reading is typed: a newer print appearing later does not take the readings", async () => {
    let newer = false;
    const view = await open(
      withRuns({ [`GET ${BASE}/main/runs`]: () => (newer ? [run({ id: "run-17", run_no: 17, created_at: "2026-11-21T16:00:00Z" }), ...RUNS] : RUNS) }),
    );
    const picker = await screen.findByRole("button", { name: /Readings from print/ });
    await waitFor(() => expect(picker).toHaveTextContent("pass 2, run #14"));
    await enter(across("S1 · Z1"), "0,2");
    newer = true;
    await act(() => view.queryClient.invalidateQueries());
    await waitFor(() => expect(view.calls.filter((c) => c.path.endsWith("/runs")).length).toBeGreaterThan(1));
    // Counter-check: the new print is in the list, it just is not chosen for readings that were already typed.
    await userEvent.click(picker);
    expect(await screen.findByRole("option", { name: /^#17 · calibration page/ })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(picker).toHaveTextContent("pass 2, run #14");
    expect(screen.getByLabelText(across("S1 · Z1"))).toHaveValue("0,2");
  });

  it("when the prints cannot be loaded it says so, and fits nothing without a print", async () => {
    const { calls } = await open({
      [`GET ${BASE}/main/runs`]: () => respond(500, apiErrorBody("internal", "Boom.")),
      [`POST ${BASE}/main/fit`]: FIT,
    });
    // A 5xx is asked again twice (queryClient.ts) before the page gives up.
    expect(
      await screen.findByText("The prints of this tray could not be loaded, so the readings cannot be tied to one. Reload the page to try again.", {}, { timeout: 10_000 }),
    ).toBeVisible();
    expect(screen.queryByText(/No calibration page of this type was printed/)).toBeNull();
    await fillThreePlain();
    expect(await button("Fit")).toBeDisabled();
    await new Promise((r) => setTimeout(r, 600));
    expect(only(calls, "POST", "/fit")).toHaveLength(0);
  });

  it("a print of an older version is not read on the current grid while that version loads or when it cannot", async () => {
    const typeV2 = { ...SHEET_TYPE, current_version: 2 };
    const { calls } = await open(
      withRuns({
        "GET /api/v1/sheet-types": [typeV2],
        "GET /api/v1/sheet-types/L4736REV": () => respond(500, apiErrorBody("internal", "Boom.")),
        [`POST ${BASE}/main/fit`]: FIT,
      }),
    );
    expect(await screen.findByText(/The sheet of print #14 \(version 1\) could not be loaded/, {}, { timeout: 10_000 })).toBeVisible();
    expect(screen.queryByRole("group", { name: "Reading 1" })).toBeNull();
    expect(await button("Fit")).toBeDisabled();
    expect(only(calls, "POST", "/fit")).toHaveLength(0);
  });

  // Two guards hold this: switching the print resets the fit mutation (its callbacks are dropped), and the answer is
  // compared with the request now in the form. The counter-test removes both; one alone keeps the test green.
  it("a manual fit whose print was switched while it ran fills nothing", async () => {
    let answer: (value: Response) => void = () => {};
    const slow = new Promise<Response>((resolve) => (answer = resolve));
    let first = true;
    await open(
      withRuns({
        [`POST ${BASE}/main/fit`]: () => {
          if (!first) return FIT;
          first = false;
          return slow;
        },
      }),
    );
    await chooseRun(/^#12 · calibration page/);
    await fillThreePlain();
    await userEvent.click(await button("Fit"));
    await chooseRun(/^#9 · calibration page/);
    await act(async () => answer(new Response(JSON.stringify(FIT), { status: 200, headers: { "Content-Type": "application/json" } })));
    await new Promise((r) => setTimeout(r, 100));
    expect(screen.queryByText("The values are filled in below. Nothing is saved yet.")).toBeNull();
    expect(screen.getByRole("textbox", { name: "Offset X (mm)" })).toHaveValue("0.4");
  });
});
