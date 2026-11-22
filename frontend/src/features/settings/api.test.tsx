// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useLookup } from "../lookup/api";
import { mockApi, settingsList } from "../test-api";
import { useSaveSettings } from "./api";

afterEach(() => vi.unstubAllGlobals());

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  // What an open item and the cart hold: the lookup, its preview and the cart's render check, for two apps.
  for (const app of ["snipeit", "grocy"]) {
    client.setQueryData(["lookup", app, "42"], { id: "42" });
    client.setQueryData(["preview", app, "42", "Drill", "qr_two_lines", 12, "null", "null"], "data:image/png;base64,AA==");
  }
  client.setQueryData(["render-check", 12, "x", []], {});
  client.setQueryData(["grocy-object", "battery", "4"], {});
  mockApi({ "PUT /api/v1/settings": () => settingsList() });
  const hook = renderHook(() => useSaveSettings(), { wrapper });
  const stale = (key: unknown[]) => client.getQueryState(key)?.isInvalidated;
  return { client, hook, stale };
}

describe("saving a public web address (#247)", () => {
  it("makes the lookup and the preview of that integration stale and bumps the cart's version", async () => {
    const { client, hook, stale } = setup();
    await act(async () => hook.result.current.mutate({ "integrations.snipeit.web_url": "https://assets.example.com" }));
    await waitFor(() => expect(hook.result.current.isSuccess).toBe(true));
    expect(stale(["lookup", "snipeit", "42"])).toBe(true);
    expect(stale(["preview", "snipeit", "42", "Drill", "qr_two_lines", 12, "null", "null"])).toBe(true);
    // The cart's verdicts follow a counter the save bumps (useCartCheck.test covers what that does to a cart).
    expect(client.getQueryData(["web-address-version"])).toBe(1);
    // Another integration's items are untouched.
    expect(stale(["lookup", "grocy", "42"])).toBe(false);
    expect(stale(["preview", "grocy", "42", "Drill", "qr_two_lines", 12, "null", "null"])).toBe(false);
  });

  it("also covers Grocy's own object pages when Grocy's web address is saved", async () => {
    const { hook, stale } = setup();
    await act(async () => hook.result.current.mutate({ "integrations.grocy.web_url": "" }));
    await waitFor(() => expect(hook.result.current.isSuccess).toBe(true));
    expect(stale(["grocy-object", "battery", "4"])).toBe(true);
    expect(stale(["lookup", "grocy", "42"])).toBe(true);
    expect(stale(["lookup", "snipeit", "42"])).toBe(false);
  });

  it("leaves them alone when another setting is saved", async () => {
    const { client, hook, stale } = setup();
    await act(async () => hook.result.current.mutate({ "integrations.snipeit.timeout": 9 }));
    await waitFor(() => expect(hook.result.current.isSuccess).toBe(true));
    expect(stale(["lookup", "snipeit", "42"])).toBe(false);
    expect(stale(["preview", "snipeit", "42", "Drill", "qr_two_lines", 12, "null", "null"])).toBe(false);
    expect(client.getQueryData(["web-address-version"])).toBeUndefined();
  });
});

describe("an open item after saving its web address (#247)", () => {
  it("asks the lookup again, so the link follows the new address", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    let url: string | null = null;
    const { calls } = mockApi({
      "GET /api/v1/lookup/snipeit/42": () => ({ app: "snipeit", id: "42", name: "Drill", url, extra: {} }),
      "PUT /api/v1/settings": () => settingsList(),
    });
    const { result } = renderHook(() => ({ lookup: useLookup("snipeit", "42"), save: useSaveSettings() }), { wrapper });
    await waitFor(() => expect(result.current.lookup.data?.url).toBeNull());
    url = "https://assets.example.com/42";
    await act(async () => result.current.save.mutate({ "integrations.snipeit.web_url": "https://assets.example.com" }));
    await waitFor(() => expect(result.current.lookup.data?.url).toBe("https://assets.example.com/42"));
    expect(calls.filter((c) => c.path === "/api/v1/lookup/snipeit/42")).toHaveLength(2);
  });
});
