// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../../api/schema.gen";
import { renderApp, SIGNED_IN, widenAppWaits } from "../../test-utils";
import { apiErrorBody, GEOMETRY_L4736, mockApi, respond, SHEET_TYPE } from "../test-api";

vi.setConfig({ testTimeout: 15_000 });
widenAppWaits();
afterEach(() => vi.unstubAllGlobals());

type VersionHistory = components["schemas"]["VersionHistoryRead"];

const PRINT_SESSION = { ...SIGNED_IN, scope: "print" as const };
const version = (over: Partial<VersionHistory>): VersionHistory => ({
  version: 1,
  geometry: GEOMETRY_L4736,
  geometry_hash: "h1",
  change_note: null,
  created_at: "2026-11-01T08:00:00Z",
  sheets: 0,
  created_by: "system:seed",
  changed_fields: {},
  restorable: false,
  ...over,
});

// Newest first, as the list route answers.
const V3 = version({
  version: 3,
  geometry_hash: "h3",
  change_note: "Pitch measured on the probe page",
  created_by: "admin",
  created_at: "2026-11-20T18:55:00Z",
  changed_fields: { pitch_x: { old: 48.2, new: 47.55 }, margin_left: { old: 9.85, new: 9.9 } },
});
const V2 = version({ version: 2, geometry_hash: "h2", change_note: "Measured again", created_by: "admin", changed_fields: { margin_top: { old: 21.3, new: 21.5 } }, restorable: true });
const V1 = version({ version: 1, restorable: true, sheets: 2 });

const DETAIL = {
  ...SHEET_TYPE,
  current_version: 3,
  sheets_in_use: 2,
  versions: [V3, V2, V1].map(({ version: n, geometry, geometry_hash, change_note, created_at, sheets }) => ({ version: n, geometry, geometry_hash, change_note, created_at, sheets })),
};
const routes = (versions: VersionHistory[], extra: Record<string, unknown> = {}) => ({
  "GET /api/v1/sheet-types": [SHEET_TYPE],
  "GET /api/v1/sheet-types/facets": { vendors: [], page_sizes: [] },
  "GET /api/v1/sheet-types/L4736REV": DETAIL,
  "GET /api/v1/sheet-types/L4736REV/versions": versions,
  ...extra,
});

async function openVersions(versions: VersionHistory[], extra: Record<string, unknown> = {}, session = SIGNED_IN) {
  const api = mockApi(routes(versions, extra));
  await renderApp({ path: "/sheets/archive?type=L4736REV", session });
  await userEvent.click(await screen.findByRole("tab", { name: "Versions" }));
  const table = await screen.findByRole("table", { name: "Versions" });
  await within(table).findByText("Pitch measured on the probe page");
  const rowOf = (n: number) => within(table).getAllByRole("row").find((row) => within(row).queryByRole("rowheader")?.textContent?.startsWith(String(n)))!;
  return { ...api, table, rowOf };
}

describe("type versions (#336, spec 7.5)", () => {
  it("type versions show changed fields old → new", async () => {
    const { rowOf } = await openVersions([V3, V2, V1]);
    const newest = rowOf(3);
    expect(within(newest).getByText("Horizontal pitch: 48.2 → 47.55 mm")).toBeVisible();
    expect(within(newest).getByText("Left margin: 9.85 → 9.9 mm")).toBeVisible();
    expect(within(newest).getByText(/admin/)).toBeVisible();
    expect(within(rowOf(2)).getByText("Top margin: 21.3 → 21.5 mm")).toBeVisible();
    // Version 1 has nothing to compare with.
    expect(within(rowOf(1)).queryByText(/→/)).toBeNull();
  });

  it("restore as new version hidden when a newer version is in use", async () => {
    // A sheet is pinned to version 2: version 1 cannot be restored, the server says so with `restorable`.
    const { rowOf } = await openVersions([V3, { ...V2, sheets: 1, restorable: true }, { ...V1, restorable: false }]);
    expect(within(rowOf(1)).queryByRole("button", { name: /Restore as new version/ })).toBeNull();
    expect(within(rowOf(1)).getByText("A sheet uses a newer version, so this one can only be shown.")).toBeVisible();
    expect(within(rowOf(2)).getByRole("button", { name: "Restore as new version (version 2)" })).toBeVisible();
    // The newest version is never offered.
    expect(within(rowOf(3)).queryByRole("button", { name: /Restore as new version/ })).toBeNull();
  });

  it("restoring posts and the new version shows up", async () => {
    let list = [V3, V2, V1];
    const { calls, rowOf } = await openVersions(list, {
      "GET /api/v1/sheet-types/L4736REV/versions": () => list,
      "POST /api/v1/sheet-types/L4736REV/versions/2/restore": () => {
        list = [version({ version: 4, geometry_hash: "h2", change_note: "Restored from version 2", created_by: "admin" }), ...list];
        return respond(201, list[0]);
      },
    });
    await userEvent.click(within(rowOf(2)).getByRole("button", { name: "Restore as new version (version 2)" }));
    expect(calls.filter((c) => c.method === "POST" && c.path.endsWith("/versions/2/restore"))).toHaveLength(1);
    expect(await screen.findByText("Restored from version 2")).toBeVisible();
  });

  it("a refused restore says why", async () => {
    const { rowOf } = await openVersions([V3, V2, V1], {
      "POST /api/v1/sheet-types/L4736REV/versions/2/restore": () => respond(409, apiErrorBody("newer_version_in_use", "in use")),
    });
    await userEvent.click(within(rowOf(2)).getByRole("button", { name: "Restore as new version (version 2)" }));
    await waitFor(() => expect(within(rowOf(2)).getByRole("alert")).toBeVisible());
  });

  it("a key without the admin scope sees the versions without restore", async () => {
    const { table } = await openVersions([V3, V2, V1], {}, PRINT_SESSION);
    expect(within(table).queryByRole("button", { name: /Restore as new version/ })).toBeNull();
  });
});
