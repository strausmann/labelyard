// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderApp, widenAppWaits } from "../../test-utils";
import { apiErrorBody, GEOMETRY_L4736, mockApi, respond, SHEET_TYPE, sheetSummary } from "../test-api";

vi.setConfig({ testTimeout: 15_000 });
widenAppWaits();
afterEach(() => vi.unstubAllGlobals());

const VERSION = { version: 1, change_note: null, created_at: "2026-11-01T08:00:00Z", geometry: GEOMETRY_L4736, geometry_hash: "abc", sheets: 3 };
const DETAIL = { ...SHEET_TYPE, sheets_total: 4, sheets_in_use: 4, versions: [VERSION] };
const USED_UP = sheetSummary("L4736-0003", { used: 48, free: 0, last_used_at: "2026-11-10T09:00:00Z" });
const IN_USE_OLD = sheetSummary("L4736-0001", { used: 10, free: 38, last_used_at: "2026-11-01T09:00:00Z" });
const IN_USE_NEW = sheetSummary("L4736-0002", { used: 20, free: 28, last_used_at: "2026-11-20T09:00:00Z" });
const DISCARDED = sheetSummary("L4736-0004", { state: "discarded", used: 5, free: 43 });

function routes(extra: Record<string, unknown> = {}) {
  return {
    "GET /api/v1/sheet-types": [SHEET_TYPE],
    "GET /api/v1/sheet-types/facets": { vendors: [], page_sizes: [] },
    "GET /api/v1/sheet-types/L4736REV": DETAIL,
    "GET /api/v1/sheets": [USED_UP, DISCARDED, IN_USE_OLD, IN_USE_NEW],
    ...extra,
  };
}

describe("the sheets of a type", () => {
  it("type detail lists its sheets in use first and hides discarded behind a toggle", async () => {
    const { calls } = mockApi(routes());
    await renderApp({ path: "/sheets/archive?type=L4736REV" });
    const list = await screen.findByRole("list", { name: "Its sheets" });
    const codes = () => within(list).getAllByRole("link").map((a) => a.textContent);
    await waitFor(() => expect(codes()).toEqual(["L4736-0002", "L4736-0001", "L4736-0003"]));
    expect(within(list).getByRole("link", { name: "L4736-0002" })).toHaveAttribute("href", "/sheets/L4736-0002");
    const asked = calls.find((c) => c.path === "/api/v1/sheets")!;
    expect(new URLSearchParams(asked.search).get("type")).toBe("L4736REV");
    expect(new URLSearchParams(asked.search).get("state")).toBe("all");

    await userEvent.click(screen.getByRole("checkbox", { name: "Show discarded (1)" }));
    await waitFor(() => expect(codes()).toEqual(["L4736-0002", "L4736-0001", "L4736-0003", "L4736-0004"]));
  });

  it("type detail links to /sheets?type=<key>", async () => {
    mockApi(routes());
    await renderApp({ path: "/sheets/archive?type=L4736REV" });
    const link = await screen.findByRole("link", { name: "Show in the sheets list" });
    expect(link).toHaveAttribute("href", "/sheets?type=L4736REV");
  });

  it("shows the usage numbers and keeps delete disabled while the type has sheets", async () => {
    mockApi(routes({ "GET /api/v1/sheet-types/L4736REV": { ...DETAIL, sheets_with_free_cells: 2, free_labels: 66, sheets_total: 4 } }));
    await renderApp({ path: "/sheets/archive?type=L4736REV" });
    expect(await screen.findByText("Sheets with free cells")).toBeVisible();
    expect(screen.getByText("66")).toBeVisible();
    expect(screen.getByRole("button", { name: "Delete type" })).toBeDisabled();
    expect(screen.getByText(/This type has 4 sheets: archive it instead/)).toBeVisible();
  });
});

describe("catalog update of an installed type", () => {
  const ENTRY = {
    key: "L4736REV",
    vendor: "Example Labels",
    product_codes: ["L4736REV-25"],
    page_size: "A4",
    cols: 4,
    rows: 12,
    label_w_mm: 45.7,
    label_h_mm: 21.2,
    verified: true,
    verified_on: "2026-11-15",
    evidence: null,
    source_name: null,
    source_url: null,
    revision: 3,
    geometry_hash: "h3",
    installed: true,
    update_available: true,
    catalog_newer: false,
    geometry: { ...GEOMETRY_L4736, margin_top: 21.3, corner_radius: 1.5 },
    description: null,
    notes: null,
    prefix: "L4736",
    allow_qr: true,
  };
  const UPDATABLE = { ...DETAIL, catalog_key: "L4736REV", catalog_revision: 2, update_available: true };

  it("catalog update shows the geometry diff old → new", async () => {
    const { calls } = mockApi(
      routes({
        "GET /api/v1/sheet-types/L4736REV": UPDATABLE,
        "GET /api/v1/sheet-catalog/L4736REV": ENTRY,
        "POST /api/v1/sheet-types/L4736REV/catalog-update": { ...SHEET_TYPE, also_updated: ["avery-zweckform-3490"] },
      }),
    );
    await renderApp({ path: "/sheets/archive?type=L4736REV" });
    const diff = await screen.findByRole("table", { name: "What the update changes" });
    const rows = within(diff).getAllByRole("row").slice(1);
    expect(rows.map((r) => r.textContent)).toEqual(["Top margin21.5 mm21.3 mm", "Corner radius2 mm1.5 mm"]);
    expect(within(diff).getByRole("columnheader", { name: "Installed (v2)" })).toBeVisible();
    expect(within(diff).getByRole("columnheader", { name: "Catalog (v3)" })).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "Update to catalog version 3" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path.endsWith("/catalog-update"))).toBe(true));
    expect(await screen.findByText("Also updated: avery-zweckform-3490")).toBeVisible();
  });

  it("an edited type is told the catalog is newer, without the button", async () => {
    mockApi(routes({ "GET /api/v1/sheet-types/L4736REV": { ...DETAIL, catalog_key: "L4736REV", catalog_revision: 2, catalog_newer: true } }));
    await renderApp({ path: "/sheets/archive?type=L4736REV" });
    expect(await screen.findByText(/The catalog has a newer version/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /Update to catalog version/ })).toBeNull();
  });

  it("a refused update says why", async () => {
    mockApi(
      routes({
        "GET /api/v1/sheet-types/L4736REV": UPDATABLE,
        "GET /api/v1/sheet-catalog/L4736REV": ENTRY,
        "POST /api/v1/sheet-types/L4736REV/catalog-update": respond(409, apiErrorBody("backing_design_shared", "x", { design: "ultragrip", types: ["my-type"] })),
      }),
    );
    await renderApp({ path: "/sheets/archive?type=L4736REV" });
    await userEvent.click(await screen.findByRole("button", { name: "Update to catalog version 3" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("my-type");
  });
});
