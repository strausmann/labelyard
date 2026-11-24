// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { I18nProvider } from "@lingui/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { i18n, initI18n } from "../../i18n";
import { apiErrorBody, mockApi, respond, SHEET_TYPE } from "../test-api";
import type { SheetTypeDetail } from "./api";
import { TypeForm } from "./TypeForm";

const BOTTOM = { x_mm: 10, y_mm: 288.4, w_mm: 190, h_mm: 5, note: "Bottom line" };
const TYPE: SheetTypeDetail = { ...SHEET_TYPE, versions: [], preprint_zones: [BOTTOM] };
const PATH = `/api/v1/sheet-types/${TYPE.key}`;

async function show(type: SheetTypeDetail = TYPE) {
  await initI18n("en");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const done = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <I18nProvider i18n={i18n}>
        <TypeForm mode={{ kind: "details", type }} onDone={done} />
      </I18nProvider>
    </QueryClientProvider>,
  );
  return done;
}

async function addZone(values: { note: string; x: string; y: string; w: string; h: string }) {
  await userEvent.click(screen.getByRole("button", { name: "Add area" }));
  const box = within(await screen.findByRole("dialog"));
  await userEvent.type(box.getByRole("textbox", { name: "Note" }), values.note);
  await userEvent.type(box.getByRole("textbox", { name: "x" }), values.x);
  await userEvent.type(box.getByRole("textbox", { name: "y" }), values.y);
  await userEvent.type(box.getByRole("textbox", { name: "Width" }), values.w);
  await userEvent.type(box.getByRole("textbox", { name: "Height" }), values.h);
  await userEvent.click(box.getByRole("button", { name: "Save area" }));
}

afterEach(() => vi.unstubAllGlobals());

describe("edit details: preprinted areas and the sheet code position", () => {
  it("zone over a cell shows the refusal from the server", async () => {
    const { calls } = mockApi({
      "GET /api/v1/preprint-presets": [],
      [`PATCH ${PATH}`]: respond(422, apiErrorBody("preprint_zone_overlaps_cell", "A preprinted area covers a label cell.", { field: "preprint_zones.1" })),
    });
    const done = await show();
    await addZone({ note: "Dust cover", x: "12", y: "22", w: "30", h: "6" });
    await userEvent.click(screen.getByRole("button", { name: "Save details" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(JSON.parse(calls.find((c) => c.method === "PATCH")!.body!)).toEqual({
      preprint_zones: [BOTTOM, { x_mm: 12, y_mm: 22, w_mm: 30, h_mm: 6, note: "Dust cover" }],
    });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("1 area was not saved");
    expect(alert).toHaveTextContent("Area 2 covers label 1 (row 1, column 1).");
    const refused = within(screen.getByRole("list", { name: "Preprinted areas" })).getAllByRole("listitem")[1]!;
    expect(within(refused).getByText("preprint_zone_overlaps_cell")).toBeVisible();
    // The diagram draws the refused zone dashed.
    expect(screen.getByRole("img", { name: /Sheet front/ }).querySelector('[data-zone="refused"]')).not.toBeNull();
    expect(done).not.toHaveBeenCalled();
  });

  it("saving an area does not submit the details form around it (Codex on #441)", async () => {
    const { calls } = mockApi({ "GET /api/v1/preprint-presets": [], [`PATCH ${PATH}`]: TYPE });
    const done = await show();
    // Another change first: a submit of the outer form would save it and close the editor.
    await userEvent.clear(screen.getByRole("textbox", { name: "Vendor" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Vendor" }), "Other vendor");
    await addZone({ note: "Logo", x: "10", y: "6", w: "30", h: "8" });
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);
    expect(done).not.toHaveBeenCalled();
    expect(screen.queryByText("Nothing changed.")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Save details" }));
    await waitFor(() => expect(done).toHaveBeenCalled());
    expect(JSON.parse(calls.find((c) => c.method === "PATCH")!.body!)).toEqual({
      vendor: "Other vendor",
      preprint_zones: [BOTTOM, { x_mm: 10, y_mm: 6, w_mm: 30, h_mm: 8, note: "Logo" }],
    });
  });

  it("saves the code position with the details", async () => {
    const { calls } = mockApi({ "GET /api/v1/preprint-presets": [], [`PATCH ${PATH}`]: TYPE });
    const done = await show();
    await userEvent.click(screen.getByRole("radio", { name: "Top" }));
    await userEvent.click(screen.getByRole("radio", { name: "Right or bottom" }));
    await userEvent.click(screen.getByRole("button", { name: "Increase the offset by 0.5 mm" }));
    await userEvent.click(screen.getByRole("button", { name: "Save details" }));
    await waitFor(() => expect(done).toHaveBeenCalled());
    expect(JSON.parse(calls.find((c) => c.method === "PATCH")!.body!)).toEqual({
      id_margin_band: "top",
      id_margin_align: "end",
      id_margin_offset_mm: 0.5,
    });
  });

  it("a position over a label is refused in words", async () => {
    mockApi({
      "GET /api/v1/preprint-presets": [],
      [`PATCH ${PATH}`]: respond(422, apiErrorBody("id_position_overlaps_cell", "The sheet code position covers a label cell.")),
    });
    await show();
    await userEvent.click(screen.getByRole("radio", { name: "Left" }));
    await userEvent.click(screen.getByRole("button", { name: "Save details" }));
    expect(await screen.findByText("At this position the sheet code would cover a label. Choose another band or offset.")).toBeVisible();
  });
});
