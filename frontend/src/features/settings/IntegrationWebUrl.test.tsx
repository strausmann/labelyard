// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderApp, widenAppWaits } from "../../test-utils";
import { KEY_ADMIN, mockApi, settingsList } from "../test-api";

afterEach(() => vi.unstubAllGlobals());
vi.setConfig({ testTimeout: 15_000 });
widenAppWaits();

const KEY = "integrations.snipeit.web_url";
const CONFIGURED = [
  { name: "snipeit" as const, configured: true },
  { name: "grocy" as const, configured: true },
  { name: "spoolman" as const, configured: false },
];

const TEST_SNIPEIT = "POST /api/v1/settings/integrations/snipeit/test";

async function open(over: Parameters<typeof settingsList>[0] = {}, language: "en" | "de" = "en", app = "Snipe-IT") {
  const saved = settingsList(over);
  const { calls } = mockApi({
    [TEST_SNIPEIT]: { ok: true, status: 200, detail: "ok" },
    "GET /api/v1/settings": saved,
    "PUT /api/v1/settings": (init: RequestInit) => {
      const values = JSON.parse(String(init.body)).values as Record<string, string>;
      return saved.map((v) => (v.key in values ? { ...v, value: String(values[v.key]).replace(/\/+$/, ""), is_set: values[v.key] !== "" } : v));
    },
    "GET /api/v1/admin/api-keys": [KEY_ADMIN],
  });
  await renderApp({ path: "/settings/integrations", info: { integrations: CONFIGURED }, language });
  return { card: await screen.findByRole("group", { name: app }), calls };
}

const field = (card: HTMLElement, de = false) => within(card).getByRole("textbox", { name: de ? "Öffentliche Webadresse" : "Public web address" });
const putBodies = (calls: { method?: string; path: string; body?: string }[]) =>
  calls.filter((c) => c.path === "/api/v1/settings" && c.method === "PUT").map((c) => JSON.parse(c.body!).values);

describe("public web address of an integration (#247)", () => {
  it("web url field saves and shows the host", async () => {
    const { card, calls } = await open();
    expect(within(card).getByText("No links shown")).toBeVisible();
    await userEvent.type(field(card), "https://assets.example.com/");
    await userEvent.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(putBodies(calls)).toEqual([{ [KEY]: "https://assets.example.com/" }]));
    expect(await within(card).findByText("Links shown")).toBeVisible();
    expect(field(card)).toHaveValue("https://assets.example.com");
    expect(within(card).getByText("assets.example.com")).toBeVisible();
  });

  it("web url with user info shows the refusal and sends nothing", async () => {
    const { card, calls } = await open({}, "en", "Grocy");
    await userEvent.type(within(card).getByRole("textbox", { name: "Public web address" }), "https://user:secret@grocy.example.invalid");
    expect(within(card).getByText("Use an http or https address without a user name or password.")).toBeVisible();
    expect(field(card)).toBeInvalid();
    expect(within(card).getByRole("button", { name: "Save" })).toBeDisabled();
    expect(putBodies(calls)).toEqual([]);
  });

  it("env-locked web url is read-only", async () => {
    const { card } = await open({ [KEY]: { value: "https://assets.example.com", is_set: true, locked: true } });
    expect(field(card)).toHaveAttribute("readonly");
    expect(within(card).getByText("Set by LABELYARD_SNIPEIT_WEB_URL")).toBeVisible();
    expect(within(card).queryByRole("button", { name: "Clear" })).toBeNull();
  });

  it("clear empties the saved address", async () => {
    const { card, calls } = await open({ [KEY]: { value: "https://assets.example.com", is_set: true } });
    expect(within(card).getByText("Links shown")).toBeVisible();
    await userEvent.click(within(card).getByRole("button", { name: "Clear" }));
    expect(field(card)).toHaveValue("");
    await userEvent.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(putBodies(calls)).toEqual([{ [KEY]: "" }]));
  });

  it("editing the web address leaves the connection test and its values alone", async () => {
    const { card, calls } = await open();
    await userEvent.type(field(card), "https://assets.example.com");
    await userEvent.click(within(card).getByRole("button", { name: "Test connection" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/integrations/snipeit/test"))).toBe(true));
    const sent = JSON.parse(calls.find((c) => c.path.endsWith("/integrations/snipeit/test"))!.body!).values;
    expect(sent).not.toHaveProperty(KEY);
  });

  // M11 (the approved mockup): the page, the URL block, the badge and the refusal line.
  it("the page opens with the Public web address callout", async () => {
    await open();
    const note = await screen.findByRole("note", { name: "" }).catch(() => screen.getByRole("note"));
    expect(within(note).getByText("Public web address")).toBeVisible();
    expect(within(note).getByText(/Without it the hub shows no "Open in" link\. It never changes the address the hub itself uses to reach the integration\./)).toBeVisible();
  });

  it("the URL block comes first, labelled Address the hub uses, with the internal-only hint", async () => {
    const { card } = await open({ "integrations.snipeit.url": { value: "https://example.invalid", is_set: true, locked: true } });
    const boxes = within(card).getAllByRole("textbox");
    expect(boxes[0]).toBe(within(card).getByRole("textbox", { name: "Address the hub uses" }));
    expect(boxes[0]).toHaveAttribute("readonly");
    expect(within(card).getByText("Set by LABELYARD_SNIPEIT_URL")).toBeVisible();
    expect(within(card).getByText("Internal. Only admins see this address.")).toBeVisible();
  });

  it("an unlocked internal address is still editable and carries the same hint", async () => {
    const { card } = await open({}, "en", "Grocy");
    const url = within(card).getByRole("textbox", { name: "Address the hub uses" });
    expect(url).not.toHaveAttribute("readonly");
    expect(within(card).getByText("Internal. Only admins see this address.")).toBeVisible();
  });

  it("a locked web address keeps its explanation next to the lock note", async () => {
    const { card } = await open({ [KEY]: { value: "https://assets.example.com", is_set: true, locked: true } });
    expect(within(card).getByText("Set by LABELYARD_SNIPEIT_WEB_URL")).toBeVisible();
    expect(within(card).getByText(/The address people reach Snipe-IT on\./)).toBeVisible();
  });

  it("one status badge per card: Links shown, No links shown, or Not saved for an invalid entry", async () => {
    const { card } = await open({ [KEY]: { value: "https://assets.example.com", is_set: true } });
    const badges = () => within(card).queryAllByText(/^(Links shown|No links shown|Not saved)$/);
    expect(badges().map((b) => b.textContent)).toEqual(["Links shown"]);
    await userEvent.clear(field(card));
    await userEvent.type(field(card), "https://user:x@assets.example.com");
    expect(badges().map((b) => b.textContent)).toEqual(["Not saved"]);
  });

  it("the refusal line carries the error icon", async () => {
    const { card } = await open();
    await userEvent.type(field(card), "https://user:secret@assets.example.com");
    const line = within(card).getByText("Use an http or https address without a user name or password.");
    expect(line.parentElement!.querySelector("svg[aria-hidden='true']")).not.toBeNull();
  });

  it("a connection test result survives editing or clearing the web address", async () => {
    const { card } = await open({ [KEY]: { value: "https://assets.example.com", is_set: true } });
    await userEvent.click(within(card).getByRole("button", { name: "Test connection" }));
    expect(await within(card).findByText("Works")).toBeVisible();
    await userEvent.type(field(card), "/snipe");
    expect(within(card).getByText("Works")).toBeVisible();
    await userEvent.click(within(card).getByRole("button", { name: "Clear" }));
    expect(within(card).getByText("Works")).toBeVisible();
    expect(within(card).getByText("Connected")).toBeVisible();
  });

  it("Paperless has no web address field (it prints no items)", async () => {
    await open();
    const paperless = await screen.findByRole("group", { name: "Paperless" });
    expect(within(paperless).queryByRole("textbox", { name: "Public web address" })).toBeNull();
  });

  it("reads in German with du", async () => {
    const { card } = await open({}, "de");
    expect(field(card, true)).toBeVisible();
    expect(within(card).getByText("Keine Links")).toBeVisible();
  });
});
