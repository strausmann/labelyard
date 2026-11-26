// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { I18nProvider } from "@lingui/react";
import { act, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FitRequest } from "./api";
import { i18n, initI18n } from "../../i18n";
import { LiveFitPanel, useLiveFit } from "./LiveFit";

// The pause of design 7.4, written out: a test against the constant would follow a changed constant.
const LIVE_FIT_DELAY_MS = 400;

const reading = (col_gap: number, row_gap: number) => ({ col_gap, row_gap, across_mm: 0.1, down_mm: 0.2 });
const request = (n: number): FitRequest => ({
  type: "L4736REV",
  version: 1,
  run_id: "run-1",
  readings: [reading(1, 1), reading(3, 1), reading(1, 11), reading(3, 11)].slice(0, n),
});
const FIT = { values: { offset_x_mm: 0, offset_y_mm: 0, scale_x: 1, scale_y: 1, rotation_deg: 0 }, residuals_mm: [], flagged: [], out_of_range: [] };

interface Sent {
  body: unknown;
  signal: AbortSignal | undefined;
  answer: (body: unknown) => void;
}

let sent: Sent[];
beforeEach(() => {
  vi.useFakeTimers();
  sent = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((_input: RequestInfo | URL, init: RequestInit = {}) => {
      return new Promise<Response>((resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        sent.push({
          body: JSON.parse(String(init.body)),
          signal: init.signal ?? undefined,
          answer: (body) => resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })),
        });
      });
    }),
  );
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useLiveFit", () => {
  it("live fit runs after three readings, debounced 400 ms, and cancels the previous request", async () => {
    const { result, rerender } = renderHook(({ r }) => useLiveFit("p", "main", r), { initialProps: { r: request(2) } });
    await act(() => vi.advanceTimersByTimeAsync(2_000));
    // Counter-test: two readings never ask the server.
    expect(sent).toHaveLength(0);

    rerender({ r: request(3) });
    await act(() => vi.advanceTimersByTimeAsync(LIVE_FIT_DELAY_MS - 1));
    expect(sent).toHaveLength(0);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(sent).toHaveLength(1);
    expect(sent[0]!.body).toEqual(request(3));

    // A change while the first answer is out: the first request is cancelled, the second runs after its own pause.
    rerender({ r: request(4) });
    expect(sent[0]!.signal?.aborted).toBe(true);
    await act(() => vi.advanceTimersByTimeAsync(LIVE_FIT_DELAY_MS));
    expect(sent).toHaveLength(2);
    expect(sent[1]!.signal?.aborted).toBe(false);
    await act(async () => sent[1]!.answer({ ...FIT, residuals_mm: [0.1, 0.1, 0.1, 0.1] }));
    expect(result.current.result?.response.residuals_mm).toEqual([0.1, 0.1, 0.1, 0.1]);
    expect(result.current.result?.request).toEqual(request(4));
  });

  it("typing in quick succession sends one request, for the last state", async () => {
    const { rerender } = renderHook(({ r }) => useLiveFit("p", "main", r), { initialProps: { r: request(3) } });
    await act(() => vi.advanceTimersByTimeAsync(200));
    rerender({ r: { ...request(3), readings: [reading(1, 1), reading(3, 1), reading(2, 11)] } });
    await act(() => vi.advanceTimersByTimeAsync(200));
    rerender({ r: request(4) });
    await act(() => vi.advanceTimersByTimeAsync(LIVE_FIT_DELAY_MS));
    expect(sent).toHaveLength(1);
    expect(sent[0]!.body).toEqual(request(4));
  });

  it("an unchanged request is not sent again", async () => {
    const { rerender } = renderHook(({ r }) => useLiveFit("p", "main", r), { initialProps: { r: request(3) } });
    await act(() => vi.advanceTimersByTimeAsync(LIVE_FIT_DELAY_MS));
    await act(async () => sent[0]!.answer(FIT));
    rerender({ r: request(3) });
    await act(() => vi.advanceTimersByTimeAsync(2_000));
    expect(sent).toHaveLength(1);
  });

  it("dropping below three readings clears the preview and cancels what is out", async () => {
    const { result, rerender } = renderHook(({ r }) => useLiveFit("p", "main", r), { initialProps: { r: request(3) } });
    await act(() => vi.advanceTimersByTimeAsync(LIVE_FIT_DELAY_MS));
    await act(async () => sent[0]!.answer(FIT));
    expect(result.current.result).not.toBeNull();
    rerender({ r: request(4) });
    await act(() => vi.advanceTimersByTimeAsync(LIVE_FIT_DELAY_MS));
    rerender({ r: request(2) });
    expect(sent[1]!.signal?.aborted).toBe(true);
    expect(result.current.result).toBeNull();
  });
});

describe("useLiveFit, late answers", () => {
  it("an answer that arrives after its request was cancelled is dropped", async () => {
    // A fetch that ignores the abort and answers anyway, as a slow server or a proxy can.
    const late: ((body: unknown) => void)[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            late.push((body) => resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })));
          }),
      ),
    );
    const { result, rerender } = renderHook(({ r }) => useLiveFit("p", "main", r), { initialProps: { r: request(3) } });
    await act(() => vi.advanceTimersByTimeAsync(LIVE_FIT_DELAY_MS));
    rerender({ r: request(4) });
    await act(() => vi.advanceTimersByTimeAsync(LIVE_FIT_DELAY_MS));
    expect(late).toHaveLength(2);
    await act(async () => late[1]!({ ...FIT, residuals_mm: [4, 4, 4, 4] }));
    // The first request's answer comes last: it must not replace the answer of the readings now in the form.
    await act(async () => late[0]!({ ...FIT, residuals_mm: [3, 3, 3] }));
    expect(result.current.result?.request).toEqual(request(4));
    expect(result.current.result?.response.residuals_mm).toEqual([4, 4, 4, 4]);
  });
});

describe("LiveFitPanel for assistive technology", () => {
  const result = (residuals: number[], outOfRange: string[] = []) => ({
    request: request(3),
    response: { ...FIT, residuals_mm: residuals, flagged: residuals.flatMap((r, i) => (r > 0.3 ? [i] : [])), out_of_range: outOfRange },
  });
  const panel = (r: ReturnType<typeof result>) => (
    <I18nProvider i18n={i18n}>
      <LiveFitPanel result={r} error={null} updating={false} complete={3} readingRows={[1, 2, 3]} />
    </I18nProvider>
  );

  it("announces the recalculated fit politely, and the out-of-range values only when they change", async () => {
    vi.useRealTimers();
    await initI18n("en");
    const { rerender } = render(panel(result([0.1, 0.4, 0.1], ["offset_x_mm"])));
    const status = screen.getByRole("status", { name: "Live fit result" });
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveTextContent("Across 0.00 mm, down 0.00 mm. 1 probable misread.");
    // The range warning is no alert: an alert is read out again on every recalculation.
    expect(screen.queryByRole("alert")).toBeNull();
    const range = screen.getByText(/outside the allowed range: offset X/);
    expect(range.closest("[aria-live='polite']")).not.toBeNull();
    const node = range;
    rerender(panel(result([0.1, 0.2, 0.1], ["offset_x_mm"])));
    // Same list: the same node, untouched, so nothing is read out again.
    expect(screen.getByText(/outside the allowed range: offset X/)).toBe(node);
    expect(status).toHaveTextContent("Across 0.00 mm, down 0.00 mm. No probable misreads.");
  });
});
