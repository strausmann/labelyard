// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { I18nProvider } from "@lingui/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { i18n, initI18n } from "../../i18n";
import { apiErrorBody, mockApi, respond, SHEET_TYPE, status } from "../test-api";
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
  return showWith(type, admin, language);
}

async function showWith(type: SheetTypeDetail, admin = true, language: "en" | "de" = "en") {
  await initI18n(language);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider i18n={i18n}>
        <IdPlacementSection type={type} admin={admin} />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

/** The card of the chosen placement (mockup M2): a group named after the place. */
const chosen = (name: RegExp | string) => screen.getByRole("group", { name });
const other = (name: string) => screen.getByRole("button", { name });

describe("where the sheet code goes", () => {
  it("auto shows the resolved place as the chosen card and the automatic note", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    await show(T3490);
    expect(within(chosen("On the back of the liner")).getByText("Chosen automatically")).toBeVisible();
    expect(within(chosen("On the back of the liner")).getByText("Default for this sheet")).toBeVisible();
    expect(screen.getByText("The hub picked this place from the usable margin. You can change it.")).toBeVisible();
  });

  it("the margin is offered as another choice but disabled, with the reason, when the type has no usable band", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    await show(T3490);
    expect(other("In the margin")).toBeDisabled();
    expect(screen.getByText(/In the margin is not available for this sheet: no margin is wide enough/)).toBeVisible();
  });

  it('the other choices line switches to a label cell, with the cell select at "1 (top left)"', async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    await show(T3490);
    expect(screen.queryByRole("button", { name: / Cell$/ })).toBeNull();
    expect(screen.getByText("Other choices:")).toBeVisible();
    await userEvent.click(other("A label cell"));
    expect(chosen("In a label cell")).toHaveTextContent("Chosen");
    // The chosen place leaves the line, the one left behind joins it.
    expect(screen.queryByRole("button", { name: "A label cell" })).toBeNull();
    expect(other("Back of the liner")).toBeEnabled();
    expect(screen.getByRole("button", { name: / Cell$/ })).toHaveTextContent("1 (top left)");
  });

  it("save sends id_placement and id_cell", async () => {
    const { calls } = mockApi({
      "GET /api/v1/backing-designs": [ULTRAGRIP],
      [`PATCH /api/v1/sheet-types/${T3490.key}`]: { ...T3490, id_placement: "cell", id_cell: 2 },
    });
    await show(T3490);
    await userEvent.click(other("A label cell"));
    await userEvent.click(screen.getByRole("button", { name: / Cell$/ }));
    await userEvent.click(await screen.findByRole("option", { name: "2" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    const body = JSON.parse(calls.find((c) => c.method === "PATCH")!.body!);
    expect(body).toEqual({ id_placement: "cell", id_cell: 2, backing_design: ULTRAGRIP.key });
  });

  it("a margin type shows the margin as chosen and offers the back and a cell", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP], [`GET /api/v1/sheet-types/${T_MARGIN.key}/id-placement`]: [] });
    await show(T_MARGIN);
    expect(chosen("In the margin")).toHaveTextContent("Prints the code in the free band around the labels.");
    expect(other("Back of the liner")).toBeEnabled();
    expect(other("A label cell")).toBeEnabled();
  });

  it("follows the server's automatic choice, not its own reading of the geometry (M7)", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    // The geometry alone has a 4.5 mm band; the server says the code still goes on the back.
    await show({ ...T3490, geometry: { ...T3490.geometry, margin_top: 20 } });
    expect(chosen("On the back of the liner")).toBeVisible();
    expect(other("In the margin")).toBeDisabled();
  });

  it("an explicit choice can go back to automatic, which saves auto (M7)", async () => {
    const explicit: SheetTypeDetail = { ...T3490, id_placement: "cell", id_placement_resolved: "cell", id_cell: 2 };
    const { calls } = mockApi({
      "GET /api/v1/backing-designs": [ULTRAGRIP],
      [`PATCH /api/v1/sheet-types/${T3490.key}`]: { ...T3490 },
    });
    await show(explicit);
    expect(chosen("In a label cell")).toBeVisible();
    await userEvent.click(other("Automatic"));
    expect(chosen("On the back of the liner")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(JSON.parse(calls.find((c) => c.method === "PATCH")!.body!)).toEqual({ id_placement: "auto", backing_design: ULTRAGRIP.key });
  });

  it("with a stored automatic setting, another place is plainly chosen and Automatic is offered to return (re-review 1)", async () => {
    mockApi({ "GET /api/v1/backing-designs": [], [`GET /api/v1/sheet-types/${T_MARGIN.key}/id-placement`]: [] });
    await show(T_MARGIN);
    expect(within(chosen("In the margin")).getByText("Chosen automatically")).toBeVisible();
    await userEvent.click(other("Back of the liner"));
    const card = chosen("On the back of the liner");
    expect(within(card).getByText("Chosen")).toBeVisible();
    expect(within(card).queryByText("Chosen automatically")).toBeNull();
    expect(screen.queryByText(/picked this place from the usable margin/)).toBeNull();
    await userEvent.click(other("Automatic"));
    expect(within(chosen("In the margin")).getByText("Chosen automatically")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Automatic" })).toBeNull();
  });

  it("automatic is not offered while the setting is already automatic", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    await show(T3490);
    expect(screen.queryByRole("button", { name: "Automatic" })).toBeNull();
  });

  it("a read-only user sees the chosen place but no other choices and no Save", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    await show(T3490, false);
    expect(chosen("On the back of the liner")).toBeVisible();
    expect(screen.queryByText("Other choices:")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("de: the card and the other choices are German", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    await show(T3490, true, "de");
    expect(screen.getByRole("heading", { name: "Wo der Bogencode hinkommt" })).toBeVisible();
    expect(chosen("Auf der Rückseite des Trägerpapiers")).toHaveTextContent("Automatisch gewählt");
    expect(other("Im Rand")).toBeDisabled();
    expect(other("Eine Etikettenzelle")).toBeVisible();
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

  it("save position shows the server's refusal and keeps the draft", async () => {
    mockApi(
      routes({
        [`PATCH /api/v1/sheet-types/${POSITIONED.key}`]: respond(422, apiErrorBody("id_position_overlaps_cell", "The sheet code position covers a label cell.")),
      }),
    );
    await show(POSITIONED);
    await userEvent.click(screen.getByRole("radio", { name: "Left" }));
    await userEvent.click(screen.getByRole("button", { name: "Save position" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("At this position the sheet code would cover a label. Choose another band or offset.");
    expect(screen.getByRole("radio", { name: "Left" })).toBeChecked();
    expect(screen.queryByText("Saved.")).toBeNull();
  });

  it("save position on a lost connection says so and keeps the draft", async () => {
    mockApi(
      routes({
        [`PATCH /api/v1/sheet-types/${POSITIONED.key}`]: () => {
          throw new TypeError("offline");
        },
      }),
    );
    await show(POSITIONED);
    const field = screen.getByRole("textbox", { name: "Offset from that end" });
    await userEvent.clear(field);
    await userEvent.type(field, "12,5");
    await userEvent.click(screen.getByRole("button", { name: "Save position" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The server cannot be reached.");
    expect(field).toHaveValue("12,5");
    expect(screen.getByRole("button", { name: "Save position" })).toBeEnabled();
  });

  it.each([
    ["-1", "a negative offset"],
    ["1000,5", "an offset over 1000 mm"],
    ["", "an empty offset"],
  ])("save position refuses %s (%s) without a request", async (text) => {
    const { calls } = mockApi(routes({ [`PATCH /api/v1/sheet-types/${POSITIONED.key}`]: POSITIONED }));
    await show(POSITIONED);
    const field = screen.getByRole("textbox", { name: "Offset from that end" });
    await userEvent.clear(field);
    if (text) await userEvent.type(field, text);
    expect(screen.getByRole("button", { name: "Save position" })).toBeDisabled();
    expect(screen.getByText("The offset is a number of mm from 0 to 1000, for example 12.5.")).toBeVisible();
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);
  });

  it("the printer list says while it is checking, when it failed and when there is no page printer", async () => {
    let answer: unknown = new Promise(() => {});
    mockApi({ "GET /api/v1/backing-designs": [], [`GET /api/v1/sheet-types/${POSITIONED.key}/id-placement`]: () => answer });
    const { unmount } = await showWith(POSITIONED);
    expect(screen.getByText("Checking the printers…")).toBeVisible();
    unmount();
    answer = respond(500, apiErrorBody("internal", "boom"));
    const second = await showWith(POSITIONED);
    expect(await screen.findByText("The printers could not be checked.")).toBeVisible();
    expect(screen.queryByRole("list", { name: "Printers" })).toBeNull();
    second.unmount();
    answer = [];
    await showWith(POSITIONED);
    expect(await screen.findByText("No enabled page printer.")).toBeVisible();
    expect(screen.queryByText(/printers do not print the code/)).toBeNull();
  });

  it("the chosen place follows the printer picked there", async () => {
    mockApi(routes());
    await show(POSITIONED);
    expect(await screen.findByText("Top band, 103.6 mm from the left")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: / Printer$/ }));
    await userEvent.click(await screen.findByRole("option", { name: "Desk inkjet" }));
    expect(screen.getByText("This printer does not print the code in the margin.")).toBeVisible();
    expect(screen.getByRole("img", { name: /^Sheet front/ }).querySelector("[data-id-rect]")).toBeNull();
  });

  it("a tray picked on a row re-checks that printer on that tray, and the chosen place follows", async () => {
    const onTray2 = [
      { ...ROWS[0]!, tray: "tray-2", offset_mm: 50, x_mm: 58.4, w_mm: 30, content: "qr_code" },
      { ...ROWS[1]! },
    ];
    const { calls } = mockApi(
      routes({
        [`GET /api/v1/sheet-types/${POSITIONED.key}/id-placement`]: (_init: RequestInit, url: URL) =>
          url.searchParams.get("tray") === "tray-2" ? onTray2 : ROWS,
        [`GET /api/v1/printers/${OFFICE}/status`]: status(OFFICE, {
          trays: [
            { key: "tray-1", label: "Tray 1 (manual feed)", enabled: true, is_default: true, load_hint: null },
            { key: "tray-2", label: "Tray 2", enabled: true, is_default: false, load_hint: null },
            { key: "tray-3", label: "Tray 3", enabled: false, is_default: false, load_hint: null },
          ],
        }),
      }),
    );
    await show(POSITIONED);
    const office = within(await screen.findByRole("list", { name: "Printers" })).getAllByRole("listitem")[0]!;
    await waitFor(() => expect(office).toHaveTextContent("Office laser · Tray 1 (manual feed)"));
    await userEvent.click(within(office).getByRole("button", { name: / Check on tray$/ }));
    // Only enabled trays are offered.
    expect(screen.queryByRole("option", { name: "Tray 3" })).toBeNull();
    await userEvent.click(await screen.findByRole("option", { name: "Tray 2" }));
    expect(calls.some((c) => c.path.endsWith("/id-placement") && c.search === "?tray=tray-2")).toBe(true);
    await waitFor(() => expect(within(office).getByText("QR + code")).toBeVisible());
    expect(within(office).getByText("The type text does not fit beside the QR here.")).toBeVisible();
    // The other printer keeps its own (default) row.
    const inkjet = within(screen.getByRole("list", { name: "Printers" })).getAllByRole("listitem")[1]!;
    expect(within(inkjet).getByText("Does not fit")).toBeVisible();
    expect(screen.getByText("Top band, 58.4 mm from the left")).toBeVisible();
  });

  it("an unsaved offset survives switching to another choice and back (re-review 1)", async () => {
    mockApi(routes());
    await show(POSITIONED);
    const field = await screen.findByRole("textbox", { name: "Offset from that end" });
    await userEvent.clear(field);
    await userEvent.type(field, "12");
    await userEvent.click(other("A label cell"));
    expect(screen.queryByRole("textbox", { name: "Offset from that end" })).toBeNull();
    await userEvent.click(other("In the margin"));
    expect(await screen.findByRole("textbox", { name: "Offset from that end" })).toHaveValue("12");
    expect(screen.getByRole("button", { name: "Save position" })).toBeEnabled();
  });

  it("a late answer for an older tray never overwrites the newer tray (re-review 1)", async () => {
    const t2 = [{ ...ROWS[0]!, tray: "tray-2", offset_mm: 50, x_mm: 58.4, w_mm: 30, content: "qr_code" }, ROWS[1]!];
    const t3 = [{ ...ROWS[0]!, tray: "tray-3", offset_mm: 70, x_mm: 78.4, w_mm: 31, content: "qr_code" }, ROWS[1]!];
    let release2: () => void = () => {};
    const gate2 = new Promise<void>((r) => (release2 = r));
    mockApi(
      routes({
        [`GET /api/v1/sheet-types/${POSITIONED.key}/id-placement`]: async (_i: RequestInit, url: URL) => {
          const tray = url.searchParams.get("tray");
          if (tray === "tray-2") {
            await gate2;
            return t2;
          }
          return tray === "tray-3" ? t3 : ROWS;
        },
        [`GET /api/v1/printers/${OFFICE}/status`]: status(OFFICE, {
          trays: ["tray-1", "tray-2", "tray-3"].map((k, i) => ({ key: k, label: `Tray ${i + 1}`, enabled: true, is_default: i === 0, load_hint: null })),
        }),
      }),
    );
    await show(POSITIONED);
    const office = within(await screen.findByRole("list", { name: "Printers" })).getAllByRole("listitem")[0]!;
    await userEvent.click(await within(office).findByRole("button", { name: / Check on tray$/ }));
    await userEvent.click(await screen.findByRole("option", { name: "Tray 2" }));
    await userEvent.click(await within(office).findByRole("button", { name: / Check on tray$/ }));
    await userEvent.click(await screen.findByRole("option", { name: "Tray 3" }));
    await waitFor(() => expect(office).toHaveTextContent("Office laser · Tray 3"));
    expect(screen.getByText("Top band, 78.4 mm from the left")).toBeVisible();
    release2();
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.getByText("Top band, 78.4 mm from the left")).toBeVisible();
    expect(office).toHaveTextContent("Office laser · Tray 3");
  });

  it("a printer whose status cannot be read keeps its row and says why it has no tray check (re-review 1)", async () => {
    mockApi(
      routes({
        [`GET /api/v1/printers/${OFFICE}/status`]: () => {
          throw new TypeError("offline");
        },
      }),
    );
    await show(POSITIONED);
    const office = within(await screen.findByRole("list", { name: "Printers" })).getAllByRole("listitem")[0]!;
    expect(within(office).getByText("Fits")).toBeVisible();
    expect(await within(office).findByText("Tray check needs the printer status, which could not be read.")).toBeVisible();
    expect(within(office).queryByRole("button", { name: / Check on tray$/ })).toBeNull();
  });

  it("a tray the printer cannot use now is said, and its default tray is shown", async () => {
    mockApi(
      routes({
        [`GET /api/v1/sheet-types/${POSITIONED.key}/id-placement`]: () => ROWS,
        [`GET /api/v1/printers/${OFFICE}/status`]: status(OFFICE, {
          trays: [
            { key: "tray-1", label: "Tray 1", enabled: true, is_default: true, load_hint: null },
            { key: "tray-2", label: "Tray 2", enabled: true, is_default: false, load_hint: null },
          ],
        }),
      }),
    );
    await show(POSITIONED);
    const office = within(await screen.findByRole("list", { name: "Printers" })).getAllByRole("listitem")[0]!;
    await userEvent.click(await within(office).findByRole("button", { name: / Check on tray$/ }));
    await userEvent.click(await screen.findByRole("option", { name: "Tray 2" }));
    expect(await within(office).findByText("This printer cannot use that tray now: shown on its default tray.")).toBeVisible();
  });

  it("a tray check on a lost connection says so and keeps the row", async () => {
    mockApi(
      routes({
        [`GET /api/v1/sheet-types/${POSITIONED.key}/id-placement`]: (_init: RequestInit, url: URL) => {
          if (url.searchParams.get("tray")) throw new TypeError("offline");
          return ROWS;
        },
        [`GET /api/v1/printers/${OFFICE}/status`]: status(OFFICE, {
          trays: [
            { key: "tray-1", label: "Tray 1", enabled: true, is_default: true, load_hint: null },
            { key: "tray-2", label: "Tray 2", enabled: true, is_default: false, load_hint: null },
          ],
        }),
      }),
    );
    await show(POSITIONED);
    const office = within(await screen.findByRole("list", { name: "Printers" })).getAllByRole("listitem")[0]!;
    await userEvent.click(await within(office).findByRole("button", { name: / Check on tray$/ }));
    await userEvent.click(await screen.findByRole("option", { name: "Tray 2" }));
    expect(await within(office).findByText("This tray could not be checked.")).toBeVisible();
    expect(within(office).getByText("Fits")).toBeVisible();
  });

  it("the position is not offered while the code goes on the back", async () => {
    mockApi({ "GET /api/v1/backing-designs": [ULTRAGRIP] });
    await show(T3490);
    expect(screen.queryByRole("heading", { name: "Position in the margin" })).toBeNull();
  });
});

