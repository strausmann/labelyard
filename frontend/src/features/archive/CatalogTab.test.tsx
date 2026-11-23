// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../../api/schema.gen";
import { renderApp, widenAppWaits } from "../../test-utils";
import { apiErrorBody, GEOMETRY_L4736, mockApi, respond, SHEET_TYPE } from "../test-api";

vi.setConfig({ testTimeout: 15_000 });
widenAppWaits();
afterEach(() => vi.unstubAllGlobals());

type Entry = components["schemas"]["CatalogEntryRead"];

export const ENTRY: Entry = {
  key: "avery-zweckform-l4736rev",
  vendor: "Avery Zweckform",
  product_codes: ["L4736REV"],
  page_size: "A4",
  cols: 4,
  rows: 12,
  label_w_mm: 45.7,
  label_h_mm: 21.2,
  verified: false,
  verified_on: null,
  evidence: "Values from one third-party source.",
  source_name: "Labelplanet",
  source_url: null,
  revision: 1,
  geometry_hash: "h1",
  installed: false,
  update_available: false,
  catalog_newer: false,
};
const INSTALLED: Entry = {
  ...ENTRY,
  key: "avery-zweckform-6121",
  product_codes: ["6121"],
  cols: 5,
  rows: 13,
  verified: true,
  verified_on: "2026-11-15",
  evidence: "Checked on real sheets.",
  installed: true,
};
const UPDATABLE: Entry = { ...INSTALLED, key: "avery-zweckform-l4731rev", product_codes: ["L4731REV"], revision: 3, update_available: true };
const FACETS = { vendors: [{ value: "Avery Zweckform", count: 3 }], page_sizes: [{ value: "A4", count: 3 }] };
const page = (items: Entry[]) => ({ items, total: 39, facets: FACETS });

const catalogCalls = (calls: { method: string; path: string; search: string }[]) =>
  calls.filter((c) => c.method === "GET" && c.path === "/api/v1/sheet-catalog");

describe("catalog tab", () => {
  it("catalog tab searches by vendor, code and size", async () => {
    const { calls } = mockApi({ "GET /api/v1/sheet-catalog": page([ENTRY]), "GET /api/v1/sheet-types": [] });
    await renderApp({ path: "/sheets/archive?tab=catalog" });
    await screen.findByRole("heading", { name: /Avery Zweckform L4736REV/ });
    const box = screen.getByRole("textbox", { name: "Search" });

    await userEvent.type(box, "zweckform");
    await waitFor(() => expect(catalogCalls(calls).some((c) => new URLSearchParams(c.search).get("q") === "zweckform")).toBe(true));

    await userEvent.clear(box);
    await userEvent.type(box, "L4736REV");
    await waitFor(() => expect(catalogCalls(calls).some((c) => new URLSearchParams(c.search).get("q") === "L4736REV")).toBe(true));

    // A size is asked for as width and height, not as text.
    await userEvent.clear(box);
    await userEvent.type(box, "45,7 x 21.2");
    await waitFor(() => {
      const sized = catalogCalls(calls).find((c) => new URLSearchParams(c.search).get("w") === "45.7");
      expect(sized).toBeDefined();
      const params = new URLSearchParams(sized!.search);
      expect(params.get("h")).toBe("21.2");
      expect(params.has("q")).toBe(false);
    });
  });

  it("verified only filter", async () => {
    const { calls } = mockApi({ "GET /api/v1/sheet-catalog": page([ENTRY, INSTALLED]), "GET /api/v1/sheet-types": [] });
    await renderApp({ path: "/sheets/archive?tab=catalog" });
    await screen.findByRole("heading", { name: /Avery Zweckform L4736REV/ });
    expect(catalogCalls(calls).every((c) => !new URLSearchParams(c.search).has("verified"))).toBe(true);
    await userEvent.click(screen.getByRole("checkbox", { name: "Verified only" }));
    await waitFor(() => expect(catalogCalls(calls).some((c) => new URLSearchParams(c.search).get("verified") === "true")).toBe(true));
    // The state badges say it in words.
    expect(within(screen.getByRole("article", { name: INSTALLED.key })).getByText(/Verified/)).toBeVisible();
    expect(within(screen.getByRole("article", { name: ENTRY.key })).getByText("Not yet validated")).toBeVisible();
  });

  it("installed entry shows Installed and Update", async () => {
    mockApi({ "GET /api/v1/sheet-catalog": page([INSTALLED, UPDATABLE, ENTRY]), "GET /api/v1/sheet-types": [] });
    const { router } = await renderApp({ path: "/sheets/archive?tab=catalog" });
    const installed = await screen.findByRole("article", { name: INSTALLED.key });
    expect(within(installed).getByText("Installed")).toBeVisible();
    expect(within(installed).queryByRole("button", { name: /Add/ })).toBeNull();
    const updatable = screen.getByRole("article", { name: UPDATABLE.key });
    expect(within(updatable).getByText("Update: catalog version 3")).toBeVisible();
    // Update opens the installed type, where the change is shown before it is applied.
    await userEvent.click(within(updatable).getByRole("link", { name: "Update" }));
    await waitFor(() => expect(router.state.location.search).toMatchObject({ type: UPDATABLE.key }));
    expect((router.state.location.search as { tab?: string }).tab).toBeUndefined();
  });

  it("add installs and the type appears in Installed", async () => {
    let installed = false;
    const added = { ...SHEET_TYPE, key: ENTRY.key, vendor: "Avery Zweckform", product_codes: ["L4736REV"], description: null };
    const { calls } = mockApi({
      "GET /api/v1/sheet-catalog": () => page([{ ...ENTRY, installed }]),
      "POST /api/v1/sheet-catalog/avery-zweckform-l4736rev/install": () => {
        installed = true;
        return respond(201, { ...added, also_updated: [] });
      },
      "GET /api/v1/sheet-types": () => (installed ? [added] : []),
      "GET /api/v1/sheet-types/facets": FACETS,
    });
    await renderApp({ path: "/sheets/archive?tab=catalog" });
    const card = await screen.findByRole("article", { name: ENTRY.key });
    await userEvent.click(within(card).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path.endsWith("/install"))).toBe(true));
    const post = calls.find((c) => c.method === "POST" && c.path.endsWith("/install"))!;
    expect(JSON.parse(post.body ?? "{}")).toEqual({});
    expect(await within(card).findByText("Installed")).toBeVisible();

    await userEvent.click(screen.getByRole("tab", { name: /Installed/ }));
    expect(await screen.findByRole("link", { name: /Avery Zweckform L4736REV/ })).toBeVisible();
  });

  it("prefix taken offers a new prefix", async () => {
    const bodies: unknown[] = [];
    mockApi({
      "GET /api/v1/sheet-catalog": page([ENTRY]),
      "GET /api/v1/sheet-types": [],
      "POST /api/v1/sheet-catalog/avery-zweckform-l4736rev/install": (init: RequestInit) => {
        const body = JSON.parse(String(init.body ?? "{}"));
        bodies.push(body);
        if (!body.prefix) return respond(409, apiErrorBody("prefix_taken", "taken", { suggested_prefix: "L47362" }));
        return respond(201, { ...SHEET_TYPE, key: ENTRY.key, prefix: body.prefix, also_updated: [] });
      },
    });
    await renderApp({ path: "/sheets/archive?tab=catalog" });
    const card = await screen.findByRole("article", { name: ENTRY.key });
    await userEvent.click(within(card).getByRole("button", { name: "Add" }));
    expect(await within(card).findByText(/Another type already uses the code prefix/)).toBeVisible();
    const prefix = within(card).getByRole("textbox", { name: "New code prefix" });
    expect(prefix).toHaveValue("L47362");
    await userEvent.click(within(card).getByRole("button", { name: "Add with this prefix" }));
    await waitFor(() => expect(bodies).toEqual([{}, { prefix: "L47362" }]));
  });

  it("opening an entry shows its grid and where the values come from", async () => {
    mockApi({
      "GET /api/v1/sheet-catalog": page([ENTRY]),
      "GET /api/v1/sheet-types": [],
      "GET /api/v1/sheet-catalog/avery-zweckform-l4736rev": { ...ENTRY, geometry: GEOMETRY_L4736, description: null, notes: null, prefix: "L4736", allow_qr: true },
    });
    await renderApp({ path: "/sheets/archive?tab=catalog" });
    const card = await screen.findByRole("article", { name: ENTRY.key });
    await userEvent.click(within(card).getByRole("button", { name: /Show details/ }));
    expect(await within(card).findByText("Labelplanet")).toBeVisible();
    expect(within(card).getByText("Values from one third-party source.", { selector: "dd" })).toBeVisible();
  });
});
