// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "../../test-utils";
import { job, mockApi, PRINTER_A, PRINTER_B, status } from "../test-api";

afterEach(() => vi.unstubAllGlobals());

describe("Printers", () => {
  it("lists every printer with a state word, tape width and swatch name", async () => {
    mockApi({
      "GET /api/v1/printers": [PRINTER_A, PRINTER_B],
      [`GET /api/v1/printers/${PRINTER_A.id}/status`]: status(PRINTER_A.id),
      [`GET /api/v1/printers/${PRINTER_B.id}/status`]: status(PRINTER_B.id, { online: false }),
    });
    await renderApp({ path: "/printers" });
    expect(await screen.findByRole("heading", { level: 1, name: "Printers" })).toBeVisible();
    expect(await screen.findByText("Ready")).toBeVisible();
    // The paused flag wins over the probe result: the badge says what the operator can act on.
    expect(screen.getByText("Paused")).toBeVisible();
    expect(screen.getAllByText("Blue")[0]).toBeVisible();
  });

  it("the card agrees with the entry (#376)", async () => {
    // An open cover while a job waits: the card says Problem, as the notification does.
    mockApi({
      "GET /api/v1/printers": [PRINTER_A],
      [`GET /api/v1/printers/${PRINTER_A.id}/status`]: status(PRINTER_A.id, { error_flags: ["doorOpen"], error_state: "doorOpen" }),
      "GET /api/v1/jobs": [job("aaaaaaaa-0000-4000-8000-000000000001", PRINTER_A.id, "queued")],
    });
    await renderApp({ path: "/printers" });
    const card = await screen.findByRole("link", { name: /Workshop PT-P750W/ });
    expect(await within(card).findByText("Problem")).toBeVisible();
    expect(within(card).getByText("Cover open — close it to print")).toBeVisible();
    expect(card.textContent).not.toContain("doorOpen");
  });

  it("an open cover with nothing waiting is a warning on the card", async () => {
    mockApi({
      "GET /api/v1/printers": [PRINTER_A],
      [`GET /api/v1/printers/${PRINTER_A.id}/status`]: status(PRINTER_A.id, { error_flags: ["doorOpen"], error_state: "doorOpen" }),
      "GET /api/v1/jobs": [],
    });
    await renderApp({ path: "/printers" });
    const card = await screen.findByRole("link", { name: /Workshop PT-P750W/ });
    expect(await within(card).findByText("Warning")).toBeVisible();
  });

  it("not-wired printer shows its reason in words", async () => {
    const stuck = { ...PRINTER_A, wiring: "not_wired", wiring_reason: "discovery_failed", wiring_at: "2026-11-21T08:12:00Z" };
    mockApi({
      "GET /api/v1/printers": [stuck, PRINTER_B],
      [`GET /api/v1/printers/${PRINTER_B.id}/status`]: status(PRINTER_B.id),
    });
    await renderApp({ path: "/printers" });
    const card = await screen.findByRole("link", { name: /Workshop PT-P750W/ });
    expect(within(card).getByText("Not wired")).toBeVisible();
    expect(within(card).getByText("Not wired: the printer did not answer when the hub looked for it.")).toBeVisible();
    expect(within(card).getByText("Check the address, then save the printer again.")).toBeVisible();
    // The code itself is for the detail page; the card has words only.
    expect(card.textContent).not.toContain("discovery_failed");
    // One note above the cards names the printer.
    expect(await screen.findByText("1 printer is not wired")).toBeVisible();
    expect(screen.getByText(/Workshop PT-P750W: open a printer to see why/)).toBeVisible();
  });

  it("every reason has its own words, and an unknown code reads as the general sentence", async () => {
    const reasons = ["model_unknown", "invalid_config", "unexpected", "a_future_code", null];
    const printers = reasons.map((wiring_reason, i) => ({
      ...PRINTER_A,
      id: `1111111${i}-1111-4111-8111-111111111111`,
      name: `Printer ${i}`,
      wiring: "not_wired",
      wiring_reason,
    }));
    mockApi({ "GET /api/v1/printers": printers });
    await renderApp({ path: "/printers" });
    expect(await screen.findByText("Not wired: the hub does not know this printer model.")).toBeVisible();
    expect(screen.getByText("Not wired: the saved settings are not valid.")).toBeVisible();
    expect(screen.getByText("Not wired: something unexpected stopped the connection.")).toBeVisible();
    expect(screen.getAllByText("Not wired: the hub is not connected to this printer.")).toHaveLength(2);
    expect(screen.getByText("5 printers are not wired")).toBeVisible();
  });

  it("wired printer shows no badge", async () => {
    mockApi({
      "GET /api/v1/printers": [PRINTER_A],
      [`GET /api/v1/printers/${PRINTER_A.id}/status`]: status(PRINTER_A.id),
    });
    await renderApp({ path: "/printers" });
    const card = await screen.findByRole("link", { name: /Workshop PT-P750W/ });
    expect(await within(card).findByText("Ready")).toBeVisible();
    expect(screen.queryByText(/Not wired/)).toBeNull();
    expect(screen.queryByText(/not wired/)).toBeNull();
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("a disabled printer is neutral, not broken", async () => {
    mockApi({ "GET /api/v1/printers": [{ ...PRINTER_A, enabled: false, wiring: "disabled" }] });
    await renderApp({ path: "/printers" });
    const card = await screen.findByRole("link", { name: /Workshop PT-P750W/ });
    expect(within(card).getByText("Disabled")).toBeVisible();
    expect(screen.queryByText(/not wired/i)).toBeNull();
  });

  it("shows an empty state without printers", async () => {
    mockApi({ "GET /api/v1/printers": [] });
    await renderApp({ path: "/printers" });
    expect(await screen.findByText("No printers yet.")).toBeVisible();
  });
});
