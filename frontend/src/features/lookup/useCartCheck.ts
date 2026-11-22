// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useState, useSyncExternalStore } from "react";
import { apiFetch } from "../../api/client";
import type { components } from "../../api/schema.gen";
import { useDebounced, type LayoutOptions } from "../layout/ql";
import { PREVIEW_DEBOUNCE_MS } from "./api";
import type { CartItem } from "./cart";
import { checkChunks, itemKey } from "./cartPreviews";

export type RenderCheckResult = components["schemas"]["RenderCheckResult"];
type RenderCheckResponse = components["schemas"]["RenderCheckResponse"];

/**
 * Asks the server which items it can draw for this tape and layout. The requests go one after the other: the route
 * charges the key's rate limit per distinct lookup, so parallel chunks would only race for the same budget. Every
 * answer is handed to `keep` as it arrives, so an interrupted run loses only the chunk in flight.
 */
async function checkAll(
  items: CartItem[],
  tapeMm: number,
  layout: LayoutOptions | undefined,
  keep: (item: CartItem, result: RenderCheckResult) => void,
  signal: AbortSignal,
): Promise<void> {
  for (const chunk of checkChunks(items)) {
    // The inputs changed while an earlier chunk was on its way: the next request would be for a cart that is gone.
    if (signal.aborted) throw new DOMException("The cart changed.", "AbortError");
    const response = await apiFetch<RenderCheckResponse>("/api/v1/render/check", {
      method: "POST",
      signal,
      json: {
        tape_mm: tapeMm,
        ...layout,
        items: chunk.map((i) => ({ content_type: i.content_type, lookup: { app: i.app, identifier: i.id } })),
      },
    });
    for (const result of response.results) {
      const item = chunk[result.index];
      if (item) keep(item, result);
    }
  }
}

const NONE: ReadonlyMap<string, RenderCheckResult> = new Map();
export const CHECK_KEY = "render-check";

/**
 * Counts the saves of a public web address (#247). The code a print carries follows that address, so a verdict
 * ("fits the label") given under the old one says nothing about the new one: the count is part of every verdict's
 * key and of the check's query key, and a save bumps it (`useSaveSettings`).
 *
 * The count lives beside the query cache, not in it: a query would be refetched by any global invalidation (reconnect,
 * session check) and fall back to its start value, and a cart coming back would then find its old verdicts again
 * (Codex review of #425). Here it only ever goes up. One count per QueryClient, so a fresh client starts at 0.
 */
type Counter = { value: number; listeners: Set<() => void> };
const counters = new WeakMap<QueryClient, Counter>();
function counterOf(client: QueryClient): Counter {
  let counter = counters.get(client);
  if (!counter) {
    counter = { value: 0, listeners: new Set() };
    counters.set(client, counter);
  }
  return counter;
}
export const webAddressVersion = (client: QueryClient) => counterOf(client).value;
export function bumpWebAddressVersion(client: QueryClient) {
  const counter = counterOf(client);
  counter.value += 1;
  for (const listener of counter.listeners) listener();
}
function useWebAddressVersion(client: QueryClient): number {
  return useSyncExternalStore(
    (listener) => {
      const { listeners } = counterOf(client);
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => counterOf(client).value,
  );
}

/** The verdicts one check run fetched, by verdict key: what the query caches. */
type Verdicts = Record<string, RenderCheckResult>;

/**
 * The verdict per cart item (key `app:id`). No check without a known tape.
 *
 * A verdict is kept per item, tape and layout. Only the items without one are sent: removing an item costs no
 * request, adding one costs one lookup, so a cart edited a few times does not burn the key's rate limit. A verdict
 * never goes stale by itself (no refetch on focus or reconnect): the cart is local, and the print checks every item
 * again. A failed check, a refused one (429) included, is not tried again by itself: it answers `isError` and no flags.
 */
export function useCartCheck(
  items: CartItem[],
  tapeMm: number | null,
  layout: LayoutOptions | undefined,
): { flags: ReadonlyMap<string, RenderCheckResult>; isPending: boolean; isError: boolean } {
  const client = useQueryClient();
  // Mutated as answers arrive, never replaced: a render reads what the finished requests left.
  const [kept] = useState(() => new Map<string, RenderCheckResult>());
  const layoutKey = JSON.stringify(layout ?? null);
  // Subscribed, so a cart that is open while the address is saved checks again.
  const version = useWebAddressVersion(client);
  const verdictKey = (item: CartItem) => `${tapeMm}|${layoutKey}|v${version}|${item.app}:${item.id}:${item.content_type}`;
  // The query cache outlives this component: verdicts fetched before (on an earlier visit of the cart, also for a
  // smaller cart) are taken from it, so coming back neither loses them nor asks for them again (Codex review, #399).
  for (const [, data] of client.getQueriesData<Verdicts>({ queryKey: [CHECK_KEY, tapeMm, layoutKey] })) {
    if (data && typeof data === "object") for (const [key, verdict] of Object.entries(data)) if (!kept.has(key)) kept.set(key, verdict);
  }
  // A run of clicks (layout, copies, removing items) asks once.
  const wanted = JSON.stringify([tapeMm, layoutKey, items.map((i) => [i.app, i.id, i.content_type])]);
  const settled = useDebounced(wanted, PREVIEW_DEBOUNCE_MS);
  const active = tapeMm !== null && items.length > 0;
  const missing = active ? items.filter((i) => !kept.has(verdictKey(i))) : [];
  const query = useQuery({
    queryKey: [CHECK_KEY, tapeMm, layoutKey, version, missing.map((i) => [i.app, i.id, i.content_type])],
    // Only runs once the debounce has caught up, so `missing`, `tapeMm` and `layout` are what the key says. Returns
    // the verdicts it fetched, so the cache holds them for the next visit of the cart.
    queryFn: async ({ signal }) => {
      const fetched: Verdicts = {};
      await checkAll(
        missing,
        tapeMm as number,
        layout,
        (item, result) => {
          kept.set(verdictKey(item), result);
          fetched[verdictKey(item)] = result;
        },
        signal,
      );
      return fetched;
    },
    enabled: missing.length > 0 && settled === wanted,
    retry: false,
    // Nothing makes a verdict stale: the key holds every input that matters.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  // Reading `status` is what re-renders on success: the verdicts are read from `kept`, filled as answers arrive.
  const failed = query.status === "error";
  const flags = new Map<string, RenderCheckResult>();
  if (active && !(failed && missing.length > 0)) {
    for (const item of items) {
      const verdict = kept.get(verdictKey(item));
      if (verdict) flags.set(itemKey(item), verdict);
    }
  }
  return {
    flags: flags.size > 0 ? flags : NONE,
    isPending: missing.length > 0 && !failed,
    isError: missing.length > 0 && failed,
  };
}
