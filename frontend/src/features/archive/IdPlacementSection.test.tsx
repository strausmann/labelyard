// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { I18nProvider } from "@lingui/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { i18n, initI18n } from "../../i18n";
import { mockApi, SHEET_TYPE } from "../test-api";
import type { SheetTypeDetail } from "./api";
import { IdPlacementSection } from "./IdPlacementSection";

const ULTRAGRIP = {
  key: "avery-zweckform-ultragrip",
  name: "Avery Zweckform ultragrip",
  page_size: "A4" as const,
  strips: [
    { x_mm: 33.5, y_mm: 22, w_mm: 22, h_mm: 253 },
    { x_mm: 154.5, y_mm: 22, w_mm: 22, h_mm: 253 },
  ],
  origin: "seed" as const,
  types_in_use: 2,
};
const T3490: SheetTypeDetail = {
  ...SHEET_TYPE,
  key: "avery-zweckform-3490",
  geometry: { page_size: "A4", page_w: 210, page_h: 297, label_w: 70, label_h: 36, cols: 3, rows: 8, pitch_x: 70, pitch_y: 36, margin_top: 4.5, margin_left: 0, corner_radius: null, shape: "rectangle" },
  id_placement: "auto",
  id_placement_resolved: "back_side",
  id_placement_auto: "back_side",
  id_cell: null,
  backing_design: ULTRAGRIP,
  versions: [],
};
const T_MARGIN: SheetTypeDetail = { ...SHEET_TYPE, id_placement: "auto", id_placement_resolved: "margin", versions: [] };

async function show(type: SheetTypeDetail, admin = true, language: "en" | "de" = "en") {
  await initI18n(language);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <I18nProvider i18n={i18n}>
        <IdPlacementSection type={type} admin={admin} />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("where the sheet code goes", () => {
  it("auto shows the resolved option selected and the automatic note", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    await show(T3490);
    expect(screen.getByRole("radio", { name: /Back side/ })).toBeChecked();
    expect(screen.getByText("Default for this sheet")).toBeVisible();
    expect(screen.getByText("Chosen automatically from the usable margin. You can change it.")).toBeVisible();
  });

  it("margin is disabled with the reason when the type has no usable band", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    await show(T3490);
    expect(screen.getByRole("radio", { name: /Margin/ })).toBeDisabled();
    expect(screen.getByText(/Not available for this sheet: no margin is wide enough/)).toBeVisible();
  });

  it('choosing reserved cell shows the cell select with "1 (top left)"', async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    await show(T3490);
    expect(screen.queryByRole("button", { name: /Cell/ })).toBeNull();
    await userEvent.click(screen.getByRole("radio", { name: /Reserved cell/ }));
    expect(screen.getByRole("button", { name: /Cell/ })).toHaveTextContent("1 (top left)");
  });

  it("save sends id_placement and id_cell", async () => {
    const { calls } = mockApi({
      "GET /api/v1/backing-designs": [ULTRAGRIP],
      [`PATCH /api/v1/sheet-types/${T3490.key}`]: { ...T3490, id_placement: "cell", id_cell: 2 },
    });
    await show(T3490);
    await userEvent.click(screen.getByRole("radio", { name: /Reserved cell/ }));
    await userEvent.click(screen.getByRole("button", { name: /Cell/ }));
    await userEvent.click(await screen.findByRole("option", { name: "2" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    const body = JSON.parse(calls.find((c) => c.method === "PATCH")!.body!);
    expect(body).toEqual({ id_placement: "cell", id_cell: 2, backing_design: ULTRAGRIP.key });
  });

  it("keeps auto when the automatic option is saved, and offers the margin type its margin", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    await show(T_MARGIN);
    expect(screen.getByRole("radio", { name: /Margin/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Margin/ })).toBeEnabled();
  });

  it("follows the server's automatic choice, not its own reading of the geometry (M7)", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    // The geometry alone has a 4.5 mm band; the server says the code still goes on the back.
    await show({ ...T3490, geometry: { ...T3490.geometry, margin_top: 20 } });
    expect(screen.getByRole("radio", { name: /Back side/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Margin/ })).toBeDisabled();
  });

  it("an explicit choice can go back to automatic, which saves auto (M7)", async () => {
    const explicit: SheetTypeDetail = { ...T3490, id_placement: "cell", id_placement_resolved: "cell", id_cell: 2 };
    const { calls } = mockApi({
      "GET /api/v1/backing-designs": [ULTRAGRIP],
      [`PATCH /api/v1/sheet-types/${T3490.key}`]: { ...T3490 },
    });
    await show(explicit);
    expect(screen.getByRole("radio", { name: /Reserved cell/ })).toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Choose automatically" }));
    expect(screen.getByRole("radio", { name: /Back side/ })).toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(JSON.parse(calls.find((c) => c.method === "PATCH")!.body!)).toEqual({ id_placement: "auto", backing_design: ULTRAGRIP.key });
  });

  it("automatic is not offered while the setting is already automatic", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    await show(T3490);
    expect(screen.queryByRole("button", { name: "Choose automatically" })).toBeNull();
  });

  it("a read-only user sees the setting but no Save", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    await show(T3490, false);
    expect(screen.getByRole("radio", { name: /Back side/ })).toBeChecked();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("de: options are German", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    await show(T3490, true, "de");
    expect(screen.getByRole("heading", { name: "Wo der Bogencode hinkommt" })).toBeVisible();
    expect(screen.getByRole("radio", { name: /Rand/ })).toBeDisabled();
    expect(screen.getByRole("radio", { name: /Rückseite/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Reservierte Zelle/ })).toBeVisible();
    expect(screen.getByText("Standard für diesen Bogen")).toBeVisible();
    await initI18n("en");
  });
});

describe("the sheet code in the margin", () => {
  const POSITIONED: SheetTypeDetail = {
    ...T_MARGIN,
    id_margin_band: "top",
    id_margin_align: "end",
    id_margin_offset_mm: 54,
    preprint_zones: [{ x_mm: 150, y_mm: 7.5, w_mm: 48, h_mm: 7, note: "Size text" }],
  };
  const OFFICE = "11111111-1111-4111-8111-111111111111";
  const INKJET = "33333333-3333-4333-8333-333333333333";
  const ROWS = [
    { printer_id: OFFICE, printer_name: "Office laser", tray: "tray-1", fits: true, band: "top", offset_mm: 95.2, x_mm: 103.6, y_mm: 5.2, w_mm: 42, h_mm: 12, content: "qr_code_type", reason: null },
    { printer_id: INKJET, printer_name: "Desk inkjet", tray: "tray-1", fits: false, band: null, offset_mm: null, x_mm: null, y_mm: null, w_mm: null, h_mm: null, content: null, reason: "too_small" },
  ];
  const routes = (over: Record<string, unknown> = {}) => ({
    "GET /api/v1/backing-designs": [],
    [`GET /api/v1/sheet-types/${POSITIONED.key}/id-placement`]: ROWS,
    ...over,
  });

  it("per-printer table shows fits, reason and band", async () => {
    mockApi(routes());
    await show(POSITIONED);
    const list = await screen.findByRole("list", { name: "Printers" });
    const [office, inkjet] = within(list).getAllByRole("listitem");
    expect(office).toHaveTextContent("Office laser");
    expect(within(office!).getByText("Fits")).toBeVisible();
    expect(within(office!).getByText("QR + code + type text")).toBeVisible();
    expect(within(office!).getByText("Top band, 54.0 mm from the right")).toBeVisible();
    expect(inkjet).toHaveTextContent("Desk inkjet");
    expect(within(inkjet!).getByText("Does not fit")).toBeVisible();
    expect(within(inkjet!).getByText("Not printed in the margin")).toBeVisible();
    expect(within(inkjet!).getByText(/The band left on this printer is too shallow or too short for the code\./)).toBeVisible();
    expect(within(inkjet!).getByText("too_small")).toBeVisible();
    expect(screen.getByText("1 of 2 printers do not print the code in the margin")).toBeVisible();
    // The chosen place of the first row, drawn and in words.
    expect(screen.getByRole("img", { name: /Sheet front/ }).querySelector("[data-id-rect]")).not.toBeNull();
    expect(screen.getByText("Top band, 103.6 mm from the left")).toBeVisible();
    expect(screen.getByText("42.0 × 12.0 mm")).toBeVisible();
  });

  it("save position sends the band, the start and the offset", async () => {
    const { calls } = mockApi(routes({ [`PATCH /api/v1/sheet-types/${POSITIONED.key}`]: POSITIONED }));
    await show(POSITIONED);
    await userEvent.click(screen.getByRole("radio", { name: "Bottom" }));
    const field = screen.getByRole("textbox", { name: "Offset from that end" });
    await userEvent.clear(field);
    await userEvent.type(field, "12,5");
    await userEvent.click(screen.getByRole("button", { name: "Save position" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(JSON.parse(calls.find((c) => c.method === "PATCH")!.body!)).toEqual({
      id_margin_band: "bottom",
      id_margin_offset_mm: 12.5,
    });
  });

  it("the position is not offered while the code goes on the back", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    await show(T3490);
    expect(screen.queryByRole("heading", { name: "Position in the margin" })).toBeNull();
  });
});

