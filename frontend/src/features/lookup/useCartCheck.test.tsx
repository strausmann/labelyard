// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, respond } from "../test-api";
import { PREVIEW_DEBOUNCE_MS } from "./api";
import type { CartItem } from "./cart";
import { useSaveSettings } from "../settings/api";
import { settingsList } from "../test-api";
import { useCartCheck } from "./useCartCheck";

const item = (n: number): CartItem => ({ app: "snipeit", id: String(n), name: `Item ${n}`, content_type: "qr_two_lines", copies: 1 });
const cart = (count: number, from = 1) => Array.from({ length: count }, (_, n) => item(from + n));

type Body = { items: { lookup: { identifier: string } }[] };
/** The check route as the server answers it: every item fine, in request order. */
const checkRoute = (extra: Record<string, unknown> = {}) => ({
  "POST /api/v1/render/check": (init: RequestInit) => ({
    results: (JSON.parse(String(init.body)) as Body).items.map((_, index) => ({ index, ok: true })),
  }),
  ...extra,
});
const sent = (calls: { path: string; body?: string }[]) =>
  calls.filter((c) => c.path === "/api/v1/render/check").map((c) => (JSON.parse(c.body!) as Body).items.map((i) => i.lookup.identifier));

function setup(initial: CartItem[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return renderHook(({ items }) => useCartCheck(items, 62, { orientation: "across", layout_preset: "compact" }), { wrapper, initialProps: { items: initial } });
}
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, PREVIEW_DEBOUNCE_MS + 80)));

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useCartCheck", () => {
  // Codex review of #399: the verdicts lived only in the component, the query cache kept just "done".
  it("coming back to the same cart shows its verdicts again without a new request", async () => {
    const { calls } = mockApi(checkRoute());
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const layout = { orientation: "across", layout_preset: "compact" } as const;
    const first = renderHook(() => useCartCheck(cart(3), 62, layout), { wrapper });
    await waitFor(() => expect(first.result.current.flags.size).toBe(3));
    first.unmount();

    const again = renderHook(() => useCartCheck(cart(3), 62, layout), { wrapper });
    expect(again.result.current.flags.size).toBe(3);
    expect(again.result.current.isPending).toBe(false);
    await settle();
    expect(again.result.current.flags.size).toBe(3);
    expect(sent(calls)).toEqual([["1", "2", "3"]]);
  });

  it("coming back after the cart grew asks only for the item that has no verdict yet", async () => {
    const { calls } = mockApi(checkRoute());
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const layout = { orientation: "across", layout_preset: "compact" } as const;
    const first = renderHook(({ items }) => useCartCheck(items, 62, layout), { wrapper, initialProps: { items: cart(2) } });
    await waitFor(() => expect(first.result.current.flags.size).toBe(2));
    first.rerender({ items: cart(3) });
    await waitFor(() => expect(first.result.current.flags.size).toBe(3));
    first.unmount();

    const again = renderHook(() => useCartCheck(cart(3), 62, layout), { wrapper });
    expect(again.result.current.flags.size).toBe(3);
    await settle();
    expect(sent(calls)).toEqual([["1", "2"], ["3"]]);
  });

  it("removing an item sends nothing, adding one sends only that one", async () => {
    const { calls } = mockApi(checkRoute());
    const view = setup(cart(3));
    await waitFor(() => expect(view.result.current.flags.size).toBe(3));
    expect(sent(calls)).toEqual([["1", "2", "3"]]);

    view.rerender({ items: cart(2) });
    await settle();
    expect(sent(calls)).toHaveLength(1);
    expect(view.result.current.flags.size).toBe(2);

    view.rerender({ items: [...cart(2), item(9)] });
    await waitFor(() => expect(view.result.current.flags.size).toBe(3));
    expect(sent(calls)).toEqual([["1", "2", "3"], ["9"]]);
  });

  it("asks again for every item when the layout changes", async () => {
    const { calls } = mockApi(checkRoute());
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const view = renderHook(({ preset }: { preset: "compact" | "standard" }) => useCartCheck(cart(2), 62, { orientation: "across", layout_preset: preset }), {
      wrapper,
      initialProps: { preset: "compact" },
    });
    await waitFor(() => expect(view.result.current.flags.size).toBe(2));
    view.rerender({ preset: "standard" });
    await waitFor(() => expect(sent(calls)).toHaveLength(2));
    expect(sent(calls)[1]).toEqual(["1", "2"]);
  });

  it("does not ask again when the window gets the focus after the verdicts went stale", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    const { calls } = mockApi(checkRoute());
    const view = setup(cart(2));
    await waitFor(() => expect(view.result.current.flags.size).toBe(2));
    vi.setSystemTime(Date.now() + 3_600_000);
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await settle();
    expect(sent(calls)).toHaveLength(1);
  });

  it("does not ask again on focus after a refused check", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    const { calls } = mockApi({ "POST /api/v1/render/check": respond(429, { detail: { error_code: "rate_limit_exceeded", error_message: "Slow down." } }) });
    const view = setup(cart(2));
    await waitFor(() => expect(view.result.current.isError).toBe(true));
    expect(view.result.current.flags.size).toBe(0);
    vi.setSystemTime(Date.now() + 3_600_000);
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await settle();
    expect(sent(calls)).toHaveLength(1);
  });

  it("stops sending chunks once the cart changed", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    let first = true;
    const { calls } = mockApi({
      "POST /api/v1/render/check": async (init: RequestInit) => {
        if (first) {
          first = false;
          await gate;
        }
        return { results: (JSON.parse(String(init.body)) as Body).items.map((_, index) => ({ index, ok: true })) };
      },
    });
    const view = setup(cart(120));
    await waitFor(() => expect(sent(calls)).toHaveLength(1));
    view.rerender({ items: cart(50) });
    await settle();
    release();
    await settle();
    // Chunk two and three of the old cart (items 51 to 120) were never sent.
    expect(sent(calls).flat().map(Number).filter((n) => n > 50)).toEqual([]);
  });

  // Codex review of #425: a longer public web address can push a code over the label's capacity, but the verdicts
  // kept for the cart were still "fine" and nothing asked again.
  describe("after a public web address is saved (#247)", () => {
    const longer = { "integrations.snipeit.web_url": "https://assets.example.com/a/rather/long/path" };

    function withSave() {
      let tooLong = false;
      const route = {
        "POST /api/v1/render/check": (init: RequestInit) => ({
          results: (JSON.parse(String(init.body)) as Body).items.map((_, index) => (tooLong ? { index, ok: false, error_code: "qr_too_long" } : { index, ok: true })),
        }),
        "PUT /api/v1/settings": () => settingsList(),
      };
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
      return { route, client, wrapper, flip: () => (tooLong = true) };
    }

    it("a mounted cart checks again and flags the item the longer address pushed over", async () => {
      const { route, wrapper, flip } = withSave();
      const { calls } = mockApi(route);
      const view = renderHook(() => ({ check: useCartCheck(cart(2), 62, { orientation: "across", layout_preset: "compact" }), save: useSaveSettings() }), { wrapper });
      await waitFor(() => expect(view.result.current.check.flags.size).toBe(2));
      expect([...view.result.current.check.flags.values()].every((v) => v.ok)).toBe(true);

      flip();
      await act(async () => view.result.current.save.mutate(longer));
      await waitFor(() => expect([...view.result.current.check.flags.values()].every((v) => !v.ok)).toBe(true));
      expect(sent(calls)).toEqual([["1", "2"], ["1", "2"]]);
    });

    it("coming back to the cart after the save checks every item again", async () => {
      const { route, wrapper, flip } = withSave();
      const { calls } = mockApi(route);
      const layout = { orientation: "across", layout_preset: "compact" } as const;
      const first = renderHook(() => useCartCheck(cart(2), 62, layout), { wrapper });
      await waitFor(() => expect(first.result.current.flags.size).toBe(2));
      first.unmount();

      flip();
      const saver = renderHook(() => useSaveSettings(), { wrapper });
      await act(async () => saver.result.current.mutate(longer));
      await waitFor(() => expect(saver.result.current.isSuccess).toBe(true));

      const again = renderHook(() => useCartCheck(cart(2), 62, layout), { wrapper });
      await waitFor(() => expect([...again.result.current.flags.values()].every((v) => !v.ok)).toBe(true));
      expect(sent(calls)).toEqual([["1", "2"], ["1", "2"]]);
    });

    // Codex review of #425 (04eba7cf): the count was a query, and any global invalidation (reconnect, session check)
    // refetched it back to 0, so a cart coming back found its verdicts from before the save again.
    it("coming back after the save and a global invalidation still checks every item again", async () => {
      const { route, client, wrapper, flip } = withSave();
      const { calls } = mockApi(route);
      const layout = { orientation: "across", layout_preset: "compact" } as const;
      const first = renderHook(() => useCartCheck(cart(2), 62, layout), { wrapper });
      await waitFor(() => expect(first.result.current.flags.size).toBe(2));
      first.unmount();

      flip();
      const saver = renderHook(() => useSaveSettings(), { wrapper });
      await act(async () => saver.result.current.mutate(longer));
      await waitFor(() => expect(saver.result.current.isSuccess).toBe(true));
      await act(async () => client.invalidateQueries());

      const again = renderHook(() => useCartCheck(cart(2), 62, layout), { wrapper });
      await waitFor(() => expect([...again.result.current.flags.values()].every((v) => !v.ok)).toBe(true));
      // Whatever the invalidation refetches once the cart is back must not bring the old verdicts with it.
      await settle();
      expect(again.result.current.flags.size).toBe(2);
      expect([...again.result.current.flags.values()].every((v) => !v.ok)).toBe(true);
      expect(sent(calls)).toEqual([["1", "2"], ["1", "2"]]);
    });

    it("a mounted cart keeps the new verdicts through a global invalidation", async () => {
      const { route, client, wrapper, flip } = withSave();
      const { calls } = mockApi(route);
      const view = renderHook(() => ({ check: useCartCheck(cart(2), 62, { orientation: "across", layout_preset: "compact" }), save: useSaveSettings() }), { wrapper });
      await waitFor(() => expect(view.result.current.check.flags.size).toBe(2));

      flip();
      await act(async () => view.result.current.save.mutate(longer));
      await waitFor(() => expect([...view.result.current.check.flags.values()].every((v) => !v.ok)).toBe(true));
      await act(async () => client.invalidateQueries());
      await settle();
      expect([...view.result.current.check.flags.values()].every((v) => !v.ok)).toBe(true);
      expect(sent(calls)).toEqual([["1", "2"], ["1", "2"]]);
    });

    it("saving another setting keeps the verdicts", async () => {
      const { route, wrapper } = withSave();
      const { calls } = mockApi(route);
      const view = renderHook(() => ({ check: useCartCheck(cart(2), 62, { orientation: "across", layout_preset: "compact" }), save: useSaveSettings() }), { wrapper });
      await waitFor(() => expect(view.result.current.check.flags.size).toBe(2));
      await act(async () => view.result.current.save.mutate({ "integrations.snipeit.timeout": 9 }));
      await settle();
      expect(sent(calls)).toHaveLength(1);
    });
  });
});
