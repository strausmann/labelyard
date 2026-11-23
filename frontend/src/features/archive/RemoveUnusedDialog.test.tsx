// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderApp, widenAppWaits } from "../../test-utils";
import { apiErrorBody, mockApi, respond, SHEET_TYPE } from "../test-api";

vi.setConfig({ testTimeout: 15_000 });
widenAppWaits();
afterEach(() => vi.unstubAllGlobals());

const unused = (key: string, vendor: string | null, code: string | null) => ({
  ...SHEET_TYPE,
  key,
  vendor,
  product_codes: code ? [code] : [],
  description: null,
  sheets_in_use: 0,
  sheets_total: 0,
  sheets_with_free_cells: 0,
  free_labels: 0,
  usage_state: "unused" as const,
});
const A = unused("avery-zweckform-l4736rev", "Avery Zweckform", "L4736REV");
const B = { ...unused("herma-4200", "Herma", "4200"), origin: "imported" as const };
const C = { ...unused("shelf-labels", null, null), description: "Shelf labels", origin: "custom" as const };
const USED = { ...SHEET_TYPE, key: "in-use" };

describe("remove unused types", () => {
  it("remove unused lists exactly the unused types, none preselected", async () => {
    const { calls } = mockApi({
      "GET /api/v1/sheet-types": (_init: RequestInit, url: URL) => (url.searchParams.get("usage_state") === "unused" ? [A, B] : [USED, A, B]),
      "GET /api/v1/sheet-types/facets": { vendors: [], page_sizes: [] },
      "POST /api/v1/sheet-types/delete-unused": { deleted: [B.key] },
    });
    await renderApp({ path: "/sheets/archive" });
    await userEvent.click(await screen.findByRole("button", { name: "Remove unused types" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove unused types" });
    const boxes = await within(dialog).findAllByRole("checkbox");
    expect(boxes).toHaveLength(2);
    expect(boxes.every((b) => !(b as HTMLInputElement).checked)).toBe(true);
    expect(within(dialog).queryByText(/in-use/)).toBeNull();
    // The list was asked for by state, archived types included.
    const asked = calls.find((c) => c.path === "/api/v1/sheet-types" && c.search.includes("usage_state=unused"))!;
    expect(new URLSearchParams(asked.search).get("include_archived")).toBe("true");

    const remove = within(dialog).getByRole("button", { name: /Remove 0 types/ });
    expect(remove).toBeDisabled();

    await userEvent.type(within(dialog).getByRole("searchbox", { name: "Filter" }), "herma");
    expect(within(dialog).getAllByRole("checkbox")).toHaveLength(1);
    await userEvent.click(within(dialog).getByRole("checkbox", { name: /Herma 4200/ }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove 1 type" }));
    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.path === "/api/v1/sheet-types/delete-unused");
      expect(JSON.parse(post?.body ?? "null")).toEqual({ keys: [B.key] });
    });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Remove unused types" })).toBeNull());
  });

  it("names imported and custom types as such, and keeps a chosen type that the filter hides chosen", async () => {
    const { calls } = mockApi({
      "GET /api/v1/sheet-types": [A, B, C],
      "GET /api/v1/sheet-types/facets": { vendors: [], page_sizes: [] },
      "POST /api/v1/sheet-types/delete-unused": { deleted: [B.key] },
    });
    await renderApp({ path: "/sheets/archive" });
    await userEvent.click(await screen.findByRole("button", { name: "Remove unused types" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove unused types" });
    expect(await within(dialog).findByText("Herma 4200 (imported)")).toBeVisible();
    expect(within(dialog).getByText("Shelf labels (custom)")).toBeVisible();
    expect(within(dialog).getByText("Avery Zweckform L4736REV")).toBeVisible();

    await userEvent.click(within(dialog).getByRole("checkbox", { name: /Herma 4200/ }));
    await userEvent.type(within(dialog).getByRole("searchbox", { name: "Filter" }), "avery");
    expect(within(dialog).getAllByRole("checkbox")).toHaveLength(1);
    expect(within(dialog).getByRole("status")).toHaveTextContent("1 chosen, 1 of them hidden by the filter.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove 1 type" }));
    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.path === "/api/v1/sheet-types/delete-unused");
      expect(JSON.parse(post?.body ?? "null")).toEqual({ keys: [B.key] });
    });
  });

  it("names the types that got sheets meanwhile, and removes nothing", async () => {
    mockApi({
      "GET /api/v1/sheet-types": [A],
      "GET /api/v1/sheet-types/facets": { vendors: [], page_sizes: [] },
      "POST /api/v1/sheet-types/delete-unused": respond(409, apiErrorBody("not_unused", "x", { keys: [A.key] })),
    });
    await renderApp({ path: "/sheets/archive" });
    await userEvent.click(await screen.findByRole("button", { name: "Remove unused types" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove unused types" });
    await userEvent.click(await within(dialog).findByRole("checkbox", { name: /Avery Zweckform L4736REV/ }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove 1 type" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(`These types have sheets now: ${A.key}. Nothing was removed.`);
  });
});
