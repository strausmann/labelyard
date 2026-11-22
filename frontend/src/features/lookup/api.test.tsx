// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, pngResponse } from "../test-api";
import { usePreviewImage } from "./api";

afterEach(() => vi.unstubAllGlobals());

const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{children}</QueryClientProvider>
);

describe("usePreviewImage (#247)", () => {
  it("preview of an integration item sends item and no qr_payload", async () => {
    const { calls } = mockApi({ "POST /api/v1/render/preview": () => pngResponse() });
    const looked = { app: "snipeit", id: "42", name: "Cordless drill", url: null, extra: {} };
    renderHook(() => usePreviewImage(looked as never, "qr_two_lines", 12), { wrapper });
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/render/preview")).toBe(true));
    const body = JSON.parse(calls.find((c) => c.path === "/api/v1/render/preview")!.body!);
    expect(body.item).toEqual({ app: "snipeit", id: "42" });
    expect(body.data).not.toHaveProperty("qr_payload");
    expect(JSON.stringify(body)).not.toContain("qr_payload");
  });
});
