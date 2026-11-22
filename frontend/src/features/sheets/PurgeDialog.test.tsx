// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderApp, SIGNED_IN } from "../../test-utils";
import { apiErrorBody, mockApi, respond, SHEET_TYPE, sheetSummary, type Call } from "../test-api";

vi.setConfig({ testTimeout: 10_000 });
afterEach(() => vi.unstubAllGlobals());

const ADMIN = { ...SIGNED_IN, scope: "admin" as const };
const PRINT = { ...SIGNED_IN, scope: "print" as const };

const purgeCalls = (calls: Call[]) => calls.filter((c) => c.path === "/api/v1/sheets/purge").map((c) => JSON.parse(c.body ?? "{}"));

function setup(purge: (body: { older_than_days: number; dry_run: boolean; limit: number }) => unknown) {
  return mockApi({
    "GET /api/v1/sheets": [sheetSummary("L4736-0001")],
    "GET /api/v1/sheet-types": [SHEET_TYPE],
    "POST /api/v1/sheets/purge": (init: RequestInit) => purge(JSON.parse(String(init.body))),
  });
}

async function open() {
  await renderApp({ path: "/sheets", session: ADMIN });
  await userEvent.click(await screen.findByRole("button", { name: "Purge discarded sheets" }));
  return screen.findByRole("dialog", { name: "Purge discarded sheets" });
}

describe("purge discarded sheets (#204)", () => {
  it("purge dialog shows the dry run before deleting", async () => {
    const { calls } = setup((b) => ({ codes: b.dry_run ? ["L4736-0007", "L4736-0003"] : ["L4736-0007", "L4736-0003"], dry_run: b.dry_run, more: false }));
    const dialog = await open();
    const days = within(dialog).getByLabelText("Discarded more than (days) ago");
    await userEvent.clear(days);
    await userEvent.type(days, "90");
    await userEvent.click(within(dialog).getByRole("button", { name: "Check what would go" }));

    expect(await within(dialog).findByText("2 sheets would be purged:")).toBeVisible();
    expect(within(dialog).getByText("L4736-0007, L4736-0003")).toBeVisible();
    expect(purgeCalls(calls)).toEqual([{ older_than_days: 90, dry_run: true, limit: 1000 }]);

    await userEvent.click(within(dialog).getByRole("button", { name: "Purge 2 sheets" }));
    expect(await within(dialog).findByText("2 sheets purged.")).toBeVisible();
    expect(within(dialog).getByText("No further discarded sheet matches.")).toBeVisible();
    expect(purgeCalls(calls).at(-1)).toEqual({ older_than_days: 90, dry_run: false, limit: 2 });
  });

  it("repeats the dry run while the server says more match", async () => {
    let left = 3;
    const { calls } = setup((b) => {
      const codes = Array.from({ length: Math.min(2, left) }, (_, i) => `L4736-000${4 - left + i}`);
      if (!b.dry_run) left -= codes.length;
      return { codes, dry_run: b.dry_run, more: b.dry_run ? left > 2 : left > 0 };
    });
    const dialog = await open();
    await userEvent.click(within(dialog).getByRole("button", { name: "Check what would go" }));
    expect(await within(dialog).findByText("The oldest 2 sheets go in this step; more follow.")).toBeVisible();

    await userEvent.click(within(dialog).getByRole("button", { name: "Purge 2 sheets" }));
    expect(await within(dialog).findByText("2 sheets purged.")).toBeVisible();
    expect(await within(dialog).findByText("1 sheet would be purged:")).toBeVisible();
    await userEvent.click(within(dialog).getByRole("button", { name: "Purge 1 sheet" }));
    expect(await within(dialog).findByText("3 sheets purged.")).toBeVisible();
    expect(purgeCalls(calls).map((b) => b.dry_run)).toEqual([true, false, true, false]);
  });

  it("refuses a days value out of range without asking the server", async () => {
    const { calls } = setup(() => ({ codes: [], dry_run: true, more: false }));
    const dialog = await open();
    const days = within(dialog).getByLabelText("Discarded more than (days) ago");
    await userEvent.clear(days);
    await userEvent.type(days, "0");
    expect(within(dialog).getByRole("button", { name: "Check what would go" })).toBeDisabled();
    expect(purgeCalls(calls)).toEqual([]);
  });

  it("says so when nothing matches, and shows a refusal of the server", async () => {
    setup(() => respond(403, apiErrorBody("forbidden", "Not an admin.")));
    const dialog = await open();
    await userEvent.click(within(dialog).getByRole("button", { name: "Check what would go" }));
    expect(await within(dialog).findByRole("alert")).toBeVisible();

    vi.unstubAllGlobals();
    setup(() => ({ codes: [], dry_run: true, more: false }));
    const days = within(dialog).getByLabelText("Discarded more than (days) ago");
    await userEvent.type(days, "0"); // 3650: a change clears the old answer
    await userEvent.click(within(dialog).getByRole("button", { name: "Check what would go" }));
    expect(await within(dialog).findByText("No discarded sheet matches.")).toBeVisible();
  });

  it("is offered to admins only", async () => {
    setup(() => ({ codes: [], dry_run: true, more: false }));
    await renderApp({ path: "/sheets", session: PRINT });
    await screen.findByRole("link", { name: "L4736-0001" });
    expect(screen.queryByRole("button", { name: "Purge discarded sheets" })).toBeNull();
    await waitFor(() => expect(screen.getByRole("button", { name: "New sheet" })).toBeVisible());
  });
});

describe("purge dialog after a purged batch", () => {
  it("does not offer the purged codes again when the next dry run fails", async () => {
    let round = 0;
    setup((b) => {
      if (b.dry_run && round > 0) return respond(500, { detail: "boom" });
      if (!b.dry_run) round += 1;
      return { codes: ["L4736-0001", "L4736-0002"], dry_run: b.dry_run, more: !b.dry_run };
    });
    const dialog = await open();
    await userEvent.click(within(dialog).getByRole("button", { name: "Check what would go" }));
    await userEvent.click(await within(dialog).findByRole("button", { name: "Purge 2 sheets" }));

    expect(await within(dialog).findByRole("alert")).toBeVisible();
    expect(within(dialog).getByText("2 sheets purged.")).toBeVisible();
    expect(within(dialog).queryByRole("button", { name: "Purge 2 sheets" })).toBeNull();
    expect(within(dialog).queryByText("L4736-0001, L4736-0002")).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Check what would go" })).toBeEnabled();
  });
});
