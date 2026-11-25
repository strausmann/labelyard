// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SIGNED_IN } from "../../test-utils";
import type { components } from "../../api/schema.gen";
import { BASE, body, only, open, P, READ_SESSION, resetBetweenTests } from "./CalibrationPage.kit";

vi.setConfig({ testTimeout: 15_000 });
resetBetweenTests();

type Entry = components["schemas"]["CalibrationHistoryRead"];
type Run = components["schemas"]["CalibrationRunRead"];

const IDENTITY = { offset_x_mm: 0, offset_y_mm: 0, scale_x: 1, scale_y: 1, rotation_deg: 0 };
const V2 = { offset_x_mm: 0.41, offset_y_mm: -0.66, scale_x: 1.00012, scale_y: 0.99992, rotation_deg: 0.021 };
const V3 = { offset_x_mm: 0.38, offset_y_mm: -0.62, scale_x: 1.00012, scale_y: 0.99992, rotation_deg: 0.021 };

const entry = (over: Partial<Entry>): Entry => ({
  id: "e0000000-0000-4000-8000-000000000000",
  seq: 1,
  action: "save",
  method: "manual",
  values: IDENTITY,
  previous: null,
  diff: {},
  measured_on: { type_key: "L4736REV", version: 1 },
  measured_on_deleted: false,
  run_id: null,
  run_no: null,
  source: null,
  readings: null,
  residuals_mm: null,
  reverted_from_id: null,
  actor: "admin",
  created_at: "2026-11-15T10:40:00Z",
  note: null,
  jobs_count: 0,
  ...over,
});

const E1 = entry({ id: "e1", seq: 1, action: "imported", actor: null, measured_on: null, created_at: "2026-11-01T08:00:00Z" });
const E2 = entry({
  id: "e2",
  seq: 2,
  values: V2,
  previous: IDENTITY,
  source: "manual",
  jobs_count: 14,
  diff: {
    offset_x_mm: { old: 0, new: 0.41 },
    offset_y_mm: { old: 0, new: -0.66 },
    scale_x: { old: 1, new: 1.00012 },
    scale_y: { old: 1, new: 0.99992 },
    rotation_deg: { old: 0, new: 0.021 },
  },
});
const E3 = entry({
  id: "e3",
  seq: 3,
  values: V3,
  previous: V2,
  source: "fit",
  run_id: "r12",
  run_no: 12,
  readings: Array.from({ length: 12 }, (_, i) => ({ col_gap: 1, row_gap: i + 1, across_mm: 0.4, down_mm: -0.6 })),
  residuals_mm: [0.05, 0.18, 0.02],
  jobs_count: 5,
  created_at: "2026-11-20T18:22:00Z",
  diff: { offset_x_mm: { old: 0.41, new: 0.38 }, offset_y_mm: { old: -0.66, new: -0.62 } },
});
const E4 = entry({
  id: "e4",
  seq: 4,
  action: "revert",
  method: "manual",
  values: V2,
  previous: V3,
  reverted_from_id: "e2",
  note: "The new tray feed drifted back; entry 3 was a bad fit.",
  jobs_count: 3,
  created_at: "2026-11-21T14:05:00Z",
  diff: { offset_x_mm: { old: 0.38, new: 0.41 }, offset_y_mm: { old: -0.62, new: -0.66 } },
});
const HISTORY = [E4, E3, E2, E1];

const run = (over: Partial<Run>): Run => ({
  id: "r0",
  run_no: 1,
  kind: "calibration_page",
  created_at: "2026-11-21T14:20:00Z",
  job_id: "j0",
  retry: false,
  submitted: true,
  requested_tray: "main",
  actual_media_source: null,
  media_source_reported: true,
  check_sheet_id: null,
  pass_no: null,
  type_key: "L4736REV",
  version: 1,
  ...over,
});

const RUNS = [
  run({ id: "r14", run_no: 14, kind: "check_page", created_at: "2026-11-21T14:44:00Z" }),
  run({ id: "r13", run_no: 13, kind: "check_page", created_at: "2026-11-21T14:32:00Z", actual_media_source: "bypass" }),
  run({ id: "r12", run_no: 12, kind: "calibration_page", created_at: "2026-11-21T14:20:00Z", media_source_reported: false }),
  run({ id: "r7", run_no: 7, kind: "probe_page", created_at: "2026-11-20T18:02:00Z", retry: true }),
  run({ id: "r6", run_no: 6, kind: "calibration_page", created_at: "2026-11-20T17:00:00Z", submitted: false }),
];

const HISTORY_PATH = `GET ${BASE}/main/history`;
const RUNS_PATH = `GET ${BASE}/main/runs`;
const atHistory = `/calibration?printer=${P}&tray=main&view=history`;

async function openHistory(extra: Record<string, unknown> = {}, session = SIGNED_IN) {
  const api = await open({ [HISTORY_PATH]: HISTORY, [RUNS_PATH]: RUNS, ...extra }, session, atHistory);
  const list = await screen.findByRole("list", { name: "Corrections for this tray" });
  return { ...api, list };
}

describe("the calibration page tabs (M7, M8)", () => {
  it("has the four tabs of the mockups and the address names the open one", async () => {
    const { router } = await open();
    const tabs = await screen.findAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Print page", "Readings", "Result", "History"]);
    // A stored correction opens on its result and the check print (#349).
    expect(screen.getByRole("tab", { name: "Result" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByRole("tab", { name: "History" }));
    await waitFor(() => expect(router.state.location.search).toMatchObject({ printer: P, tray: "main", view: "history" }));
  });

  it("a tray without a correction opens on Print page", async () => {
    await open({}, SIGNED_IN, `/calibration?printer=${P}&tray=bypass`);
    expect(await screen.findByRole("tab", { name: "Print page" })).toHaveAttribute("aria-selected", "true");
  });

  it("the tabs that are not open are inert, and keep their content", async () => {
    await open({}, SIGNED_IN, `/calibration?printer=${P}&tray=main&view=readings`);
    const readings = await screen.findByRole("tabpanel", { name: "Readings" });
    expect(readings).not.toHaveAttribute("inert");
    const result = document.querySelector('[data-inert="true"] [aria-labelledby="cal-current-h"]');
    expect(result).not.toBeNull();
  });

  it("opens the tab the address names", async () => {
    await openHistory();
    expect(screen.getByRole("tab", { name: "History" })).toHaveAttribute("aria-selected", "true");
  });
});

describe("history (#336, M7)", () => {
  it("history lists entries newest first with the diff", async () => {
    const { list } = await openHistory();
    const items = within(list).getAllByRole("listitem");
    expect(items.map((li) => within(li).getByTestId("seq").textContent)).toEqual(["4", "3", "2", "1"]);
    // The newest entry is the one in use, and it says what it came from and why.
    expect(within(items[0]!).getByText("In use now")).toBeVisible();
    expect(within(items[0]!).getByText("Revert")).toBeVisible();
    expect(within(items[0]!).getByText(/Restored from entry 2\./)).toBeVisible();
    expect(within(items[0]!).getByText("Note: The new tray feed drifted back; entry 3 was a bad fit.")).toBeVisible();
    // The values, with the change against the entry before, in the units the page uses.
    const across = within(items[0]!).getByRole("group", { name: "Across" });
    expect(across).toHaveTextContent("+0.41");
    expect(across).toHaveTextContent("+0.03 vs. before");
    expect(within(items[0]!).getByRole("group", { name: "Scale across" })).toHaveTextContent("+0.012");
    // An unchanged value has no "vs. before".
    expect(within(items[0]!).getByRole("group", { name: "Scale across" })).not.toHaveTextContent("vs. before");
    expect(within(items[1]!).getByRole("group", { name: "Down" })).toHaveTextContent("+0.04 vs. before");
    expect(within(items[1]!).getByText(/Fit from 12 readings of print #12 · largest residual 0\.18 mm\./)).toBeVisible();
    expect(within(items[2]!).getByText(/Typed by hand, no readings\./)).toBeVisible();
    expect(within(items[3]!).getByText("Imported")).toBeVisible();
    expect(within(items[3]!).getByText("The correction this tray had when 1.8.0 was installed. It had no readings.")).toBeVisible();
    expect(screen.getByText("Showing 4 of 4 entries. The hub keeps the newest 500 per printer and tray.")).toBeVisible();
  });

  it("jobs count links to the jobs list filtered by the entry", async () => {
    const { list } = await openHistory();
    const link = within(list).getByRole("link", { name: "14 jobs printed with it" });
    expect(link).toHaveAttribute("href", "/jobs?calibration=e2");
    expect(within(list).getByRole("link", { name: "0 jobs printed with it" })).toHaveAttribute("href", "/jobs?calibration=e1");
  });

  it("offers restore on every entry but the one in use, and only to an admin", async () => {
    const { list } = await openHistory();
    expect(within(list).getAllByRole("button", { name: /^Restore these values/ })).toHaveLength(3);
    expect(within(within(list).getAllByRole("listitem")[0]!).queryByRole("button", { name: /^Restore/ })).toBeNull();
  });

  it("a read key sees the history without restore", async () => {
    const { list } = await openHistory({}, READ_SESSION);
    expect(within(list).queryByRole("button", { name: /^Restore/ })).toBeNull();
  });

  it("restore dialog names the values", async () => {
    let posted: string | undefined;
    const { list, calls } = await openHistory({
      [`POST ${BASE}/main/history/e2/restore`]: (init: RequestInit) => {
        posted = init.body as string;
        return { tray: "main", ...V2, method: "manual", updated_at: "2026-11-25T08:00:00Z", measured_on: null, notes: [], ignored_fields: [] };
      },
    });
    const item = within(list).getAllByRole("listitem")[2]!;
    await userEvent.click(within(item).getByRole("button", { name: /^Restore these values/ }));
    const dialog = await screen.findByRole("dialog", { name: "Use these values again?" });
    expect(within(dialog).getByText("Office laser · Main tray. This adds a new entry; nothing is deleted.")).toBeVisible();
    expect(within(dialog).getByRole("group", { name: "Across" })).toHaveTextContent("+0.41 mm");
    expect(within(dialog).getByRole("group", { name: "Down" })).toHaveTextContent("−0.66 mm");
    expect(within(dialog).getByRole("group", { name: "Scale" })).toHaveTextContent("+0.012 % / −0.008 %");
    expect(within(dialog).getByRole("group", { name: "Rotation" })).toHaveTextContent("+0.021°");
    await userEvent.type(within(dialog).getByRole("textbox", { name: "Note (optional)" }), "Back to the values of 11-15");
    await userEvent.click(within(dialog).getByRole("button", { name: "Use these values" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(only(calls, "POST", "/restore")).toHaveLength(1);
    expect(JSON.parse(posted!)).toEqual({ note: "Back to the values of 11-15" });
    expect(body(only(calls, "POST", "/restore")[0])).toEqual({ note: "Back to the values of 11-15" });
  });

  it("a failed restore keeps the dialog open and says so", async () => {
    const { list } = await openHistory({
      [`POST ${BASE}/main/history/e2/restore`]: () => new Response(JSON.stringify({ detail: { error_code: "tray_unknown" } }), { status: 422, headers: { "Content-Type": "application/json" } }),
    });
    await userEvent.click(within(within(list).getAllByRole("listitem")[2]!).getByRole("button", { name: /^Restore these values/ }));
    const dialog = await screen.findByRole("dialog", { name: "Use these values again?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Use these values" }));
    expect(await within(dialog).findByRole("alert")).toBeVisible();
  });
});

describe("pages printed from this tray (#336, #240, M7)", () => {
  const rowOf = async (run: string) => {
    const table = await screen.findByRole("table", { name: "Pages printed from this tray" });
    return within(table).getAllByRole("row").find((row) => within(row).queryByText(run) !== null)!;
  };

  it("actual tray warning shows printed from and requested", async () => {
    await openHistory();
    const warning = await screen.findByRole("note", { name: "Print #13 came from Bypass" });
    expect(warning).toHaveTextContent(
      "You asked for Main tray but the printer reported that the page was fed from Bypass. The correction you save from it belongs to Bypass.",
    );
    const row = await rowOf("#13");
    expect(within(row).getByText("Bypass")).toBeVisible();
    expect(within(row).getByText("Main tray")).toBeVisible();
  });

  it("not reported shown when the printer gives no media source", async () => {
    await openHistory();
    expect(within(await rowOf("#12")).getByText("not reported by the printer")).toBeVisible();
    // A reported tray that matches the request shows that tray.
    expect(within(await rowOf("#14")).getAllByText("Main tray")).toHaveLength(2);
  });

  it("retry run shows #7 (retry)", async () => {
    await openHistory();
    const row = await rowOf("#7");
    expect(row).toHaveTextContent("#7");
    expect(row).toHaveTextContent("Probe page (retry)");
  });

  it("not submitted run is marked", async () => {
    await openHistory();
    expect(within(await rowOf("#6")).getByText("not submitted")).toBeVisible();
    expect(within(await rowOf("#14")).queryByText("not submitted")).toBeNull();
  });

  it("without a mismatch there is no tray warning", async () => {
    await openHistory({ [RUNS_PATH]: RUNS.filter((r) => r.actual_media_source === null) });
    await rowOf("#14");
    expect(screen.queryByRole("note", { name: /came from/ })).toBeNull();
  });

  it("an empty history and run list say so", async () => {
    await open({ [HISTORY_PATH]: [], [RUNS_PATH]: [] }, SIGNED_IN, atHistory);
    expect(await screen.findByText("Nothing saved for this tray yet.")).toBeVisible();
    expect(await screen.findByText("No calibration, check or probe page printed from this tray yet.")).toBeVisible();
  });
});
