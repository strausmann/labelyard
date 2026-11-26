// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { I18nProvider } from "@lingui/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { INFO_KEY } from "../../app/session";
import { i18n, initI18n } from "../../i18n";
import { INFO } from "../../test-utils";
import { FEEDBACK_BASE } from "../archive/feedbackLink";
import { apiErrorBody, GEOMETRY_L4736, mockApi, respond, SHEET_TYPE } from "../test-api";
import type { FitResponse } from "./api";
import { SuggestionCard, type SuggestionRun } from "./SuggestionCard";

beforeAll(() => initI18n("en"));
afterEach(() => vi.unstubAllGlobals());

const VALUES = { offset_x_mm: 0, offset_y_mm: 0, scale_x: 1, scale_y: 1, rotation_deg: 0 };
const SUGGESTION: NonNullable<FitResponse["suggestion"]> = { pitch_x_hmm: 4820, pitch_y_hmm: null, margin_left_hmm: 870, margin_top_hmm: null, uneven: false };
const suggests = (over: Partial<typeof SUGGESTION> = {}): FitResponse => ({
  values: VALUES,
  residuals_mm: [0.1, 0.1, 0.1],
  flagged: [],
  out_of_range: [],
  suggestion: { ...SUGGESTION, ...over },
  suggestion_unavailable: null,
});
const unavailable = (reason: NonNullable<FitResponse["suggestion_unavailable"]>): FitResponse => ({
  values: VALUES,
  residuals_mm: [0.1, 0.1, 0.1],
  flagged: [],
  out_of_range: [],
  suggestion: null,
  suggestion_unavailable: reason,
});
const CHECK_RUN: SuggestionRun = { id: "run-14", run_no: 14, kind: "check_page" };
const DETAIL = { ...SHEET_TYPE, versions: [{ version: 1, change_note: null, created_at: null, geometry: GEOMETRY_L4736, geometry_hash: "a", sheets: 1 }] };
const APPLY = "POST /api/v1/sheet-types/L4736REV/versions/from-suggestion";

function show(response: FitResponse, { admin = true, run = CHECK_RUN as SuggestionRun | null, routes = {} as Record<string, unknown> } = {}) {
  const api = mockApi({ "GET /api/v1/sheet-types/L4736REV": DETAIL, ...routes });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  client.setQueryData(INFO_KEY, INFO);
  render(
    <I18nProvider i18n={i18n}>
      <QueryClientProvider client={client}>
        <SuggestionCard type={SHEET_TYPE} run={run} response={response} readings={8} measuredOn="Other OT-1 (OTHERTYPE)" admin={admin} />
      </QueryClientProvider>
    </I18nProvider>,
  );
  return api;
}

describe("the geometry suggestion", () => {
  it("names the values, old to new, and the type the correction came from", async () => {
    show(suggests());
    const region = await screen.findByRole("region", { name: "Geometry suggestion" });
    expect(region).toHaveTextContent("From 8 check readings");
    expect(region).toHaveTextContent("Other OT-1 (OTHERTYPE)");
    expect(screen.getByText("48.50 → 48.20 mm")).toBeVisible();
    expect(screen.getByText("8.40 → 8.70 mm")).toBeVisible();
    // Counter-test: a value the suggestion leaves alone is not listed.
    expect(screen.queryByText("Pitch down")).toBeNull();
  });

  it("apply also opens the prefilled feedback issue", async () => {
    const opened = vi.fn<(url?: string | URL, target?: string) => Window | null>(() => ({ opener: {} }) as unknown as Window);
    vi.stubGlobal("open", opened);
    const { calls } = show(suggests(), { routes: { [APPLY]: { version: 2, change_note: "From the check print of run #14", created_at: null, geometry: GEOMETRY_L4736, geometry_hash: "b", sheets: 0 } } });
    const link = await screen.findByRole("link", { name: /Fill the feedback issue/ });
    await userEvent.click(screen.getByRole("button", { name: "Apply as new version" }));
    expect(await screen.findByText("Version 2 of L4736REV is added. The feedback issue opened in a new tab.")).toBeVisible();
    const sent = calls.find((c) => c.method === "POST");
    expect(JSON.parse(sent!.body!)).toEqual({ run_id: "run-14", pitch_x_mm: 48.2, margin_left_mm: 8.7 });
    expect(opened).toHaveBeenCalledTimes(1);
    const [url, target] = opened.mock.calls[0]!;
    expect(String(url)).toBe(link.getAttribute("href"));
    expect(String(url).startsWith(FEEDBACK_BASE)).toBe(true);
    expect(new URL(String(url)).searchParams.get("observed")).toContain("check print run #14");
    expect(target).toBe("_blank");
  });

  it("says where the issue is when the browser blocked the new tab", async () => {
    vi.stubGlobal("open", vi.fn(() => null));
    show(suggests(), { routes: { [APPLY]: { version: 2, change_note: null, created_at: null, geometry: GEOMETRY_L4736, geometry_hash: "b", sheets: 0 } } });
    await userEvent.click(await screen.findByRole("button", { name: "Apply as new version" }));
    expect(await screen.findByText("Version 2 of L4736REV is added. Open the feedback issue with the link above.")).toBeVisible();
  });

  it("shows each refusal of apply in its own words, and opens no issue for it", async () => {
    const opened = vi.fn();
    vi.stubGlobal("open", opened);
    const cases: [number, string, Record<string, unknown> | undefined, string][] = [
      [409, "run_version_not_current", undefined, "This sheet type has a newer version than the one the check print was made for. Print a new check page and read it again."],
      [422, "run_not_a_check_print", undefined, "These readings are not from a check print, so they cannot change the sheet type."],
      [422, "suggestion_unavailable", { reason: "same_type" }, "The saved correction was measured on this same type, so a pitch error would be absorbed into it. Print a check page on another sheet type first."],
      [422, "invalid_geometry", { field: "pitch_x", reason: "overlap" }, "The suggested values do not make a valid sheet: the labels would overlap or leave the page. Nothing was changed."],
    ];
    for (const [status, code, detail, words] of cases) {
      const view = show(suggests(), { routes: { [APPLY]: () => respond(status, apiErrorBody(code, "No.", detail)) } });
      await userEvent.click(await screen.findByRole("button", { name: "Apply as new version" }));
      expect(await screen.findByRole("alert")).toHaveTextContent(words);
      expect(view.calls.filter((c) => c.method === "POST")).toHaveLength(1);
      cleanup();
    }
    expect(opened).not.toHaveBeenCalled();
  });

  it("an uneven pattern points at the sheet, not the printer", async () => {
    show(suggests({ pitch_x_hmm: null, margin_left_hmm: null, uneven: true }));
    expect(await screen.findByText("Uneven: the readings follow no straight line. Check the sheet, not the printer.")).toBeVisible();
    // Nothing to apply: no value is proposed.
    expect(screen.queryByRole("button", { name: "Apply as new version" })).toBeNull();
  });
});

describe("no suggestion", () => {
  it("shows each reason in its own sentence", async () => {
    const reasons: [NonNullable<FitResponse["suggestion_unavailable"]>, string][] = [
      ["same_type", "The saved correction was measured on this same type, so a pitch error would be absorbed into it. Print a check page on another sheet type first."],
      ["no_check_run", "This check print was made without a correction, so what it shows is the printer, not the sheet. Print the check page with the correction."],
      ["unknown_correction", "It is not known which sheet type the saved correction was measured on (it was typed in, or its history is gone), so the sheet cannot be told apart from the printer."],
    ];
    for (const [reason, words] of reasons) {
      show(unavailable(reason));
      const note = await screen.findByRole("note");
      expect(note).toHaveTextContent("No suggestion for this one");
      expect(note).toHaveTextContent(words);
      cleanup();
    }
  });

  it("stays quiet for the readings of a calibration page", async () => {
    show(unavailable("no_check_run"), { run: { id: "run-12", run_no: 12, kind: "calibration_page" } });
    await waitFor(() => expect(screen.queryByRole("note")).toBeNull());
  });
});
