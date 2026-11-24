// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { render as rtlRender, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { I18nProvider } from "@lingui/react";
import type { ReactElement } from "react";
import { i18n, initI18n } from "../../i18n";
import type { GeometryMm } from "./cellState";
import { GeometryDiagram, SheetFrontDiagram } from "./GeometryDiagram";

const G: GeometryMm = {
  page_size: "A4", page_w: 210, page_h: 297, label_w: 45.7, label_h: 21.2, cols: 4, rows: 12,
  pitch_x: 48.2, pitch_y: 21.2, margin_top: 21.3, margin_left: 9.85, corner_radius: null, shape: "rectangle",
};

const render = (ui: ReactElement) =>
  rtlRender(ui, { wrapper: ({ children }) => <I18nProvider i18n={i18n}>{children}</I18nProvider> });

beforeEach(async () => {
  await initI18n("en");
});

describe("GeometryDiagram", () => {
  it("shows an unknown corner radius as the word", () => {
    render(<GeometryDiagram geometry={G} />);
    expect(screen.getByText("Corner radius").nextElementSibling).toHaveTextContent("unknown");
  });

  it("shows a known radius, and gaps and computed margins", () => {
    render(<GeometryDiagram geometry={{ ...G, corner_radius: 1.5 }} />);
    expect(screen.getByText("Corner radius").nextElementSibling).toHaveTextContent("1.5 mm");
    expect(screen.getByText("Gap between columns").nextElementSibling).toHaveTextContent("2.5 mm");
    expect(screen.getByText("Gap between rows").nextElementSibling).toHaveTextContent("0 mm");
    // right = 210 - (9.85 + 3 * 48.2 + 45.7) = 9.85 ; bottom = 297 - (21.3 + 11 * 21.2 + 21.2) = 21.3
    expect(screen.getByText("Right margin").nextElementSibling).toHaveTextContent("9.85 mm");
    expect(screen.getByText("Bottom margin").nextElementSibling).toHaveTextContent("21.3 mm");
  });

  it("flags asymmetric margins on its own when no hints are given", () => {
    render(<GeometryDiagram geometry={{ ...G, margin_left: 4 }} />);
    expect(screen.getByRole("note")).toHaveTextContent(/left and right margins differ/i);
  });

  it("uses the server's hints when given, and stays quiet for none", () => {
    const { rerender } = render(<GeometryDiagram geometry={{ ...G, margin_left: 4 }} hints={[]} />);
    expect(screen.queryByRole("note")).toBeNull();
    rerender(<GeometryDiagram geometry={G} hints={["margins_asymmetric_y"]} />);
    expect(within(screen.getByRole("note")).getByText(/top and bottom margins differ/i)).toBeVisible();
  });

  it("says a round sheet has no corner radius to measure", () => {
    render(<GeometryDiagram geometry={{ ...G, shape: "round", label_h: 45.7, pitch_y: 48 }} />);
    expect(screen.getByText("Corner radius").nextElementSibling).toHaveTextContent("round");
  });

  it("pluralises the grid size, also for one column and one row", () => {
    const { unmount } = render(<GeometryDiagram geometry={G} />);
    expect(screen.getByText("Grid").nextElementSibling).toHaveTextContent("4 columns × 12 rows");
    unmount();
    render(<GeometryDiagram geometry={{ ...G, cols: 1, rows: 1, pitch_x: 190, label_w: 190 }} />);
    expect(screen.getByText("Grid").nextElementSibling).toHaveTextContent("1 column × 1 row");
  });

  it("survives a geometry without page dimensions", () => {
    render(<GeometryDiagram geometry={{ ...G, page_w: null, page_h: null }} />);
    expect(screen.getByText("Right margin").nextElementSibling).not.toHaveTextContent("NaN");
  });
});

describe("SheetFrontDiagram", () => {
  const logo = { x_mm: 10, y_mm: 6.5, w_mm: 34, h_mm: 9, note: "Logo" };
  const dust = { x_mm: 12, y_mm: 22, w_mm: 30, h_mm: 6, note: "Dust cover" };

  it("diagram draws zones hatched and the ID rectangle outlined", () => {
    const { container } = render(
      <SheetFrontDiagram geometry={G} zones={[logo, dust]} refused={[1]} idRect={{ x_mm: 104.2, y_mm: 5, w_mm: 42, h_mm: 12 }} label="Sheet front" />,
    );
    const svg = screen.getByRole("img", { name: "Sheet front" });
    const zones = svg.querySelectorAll("[data-zone]");
    expect(zones).toHaveLength(2);
    const [kept, refused] = [...zones] as SVGRectElement[];
    // Hatched: filled with the hatch pattern, drawn at the zone's place in page mm.
    expect(kept!.getAttribute("data-zone")).toBe("kept");
    expect(kept!.getAttribute("fill")).toMatch(/^url\(#.+-zone\)$/);
    expect([kept!.getAttribute("x"), kept!.getAttribute("y"), kept!.getAttribute("width"), kept!.getAttribute("height")]).toEqual(["10", "6.5", "34", "9"]);
    // A refused zone is dashed, not only coloured.
    expect(refused!.getAttribute("data-zone")).toBe("refused");
    expect(refused!.getAttribute("stroke-dasharray")).not.toBeNull();
    const id = svg.querySelector("[data-id-rect]")!;
    expect(id).not.toBeNull();
    expect([id.getAttribute("x"), id.getAttribute("y"), id.getAttribute("width"), id.getAttribute("height")]).toEqual(["104.2", "5", "42", "12"]);
    expect(id.getAttribute("fill")).toBeNull();
    expect(id.getAttribute("class")).toContain("stroke-link");
    expect(id.getAttribute("stroke-dasharray")).toBeNull();
    // Every label cell is drawn.
    expect(container.querySelectorAll("[data-cell-shape]")).toHaveLength(48);
  });

  it("without zones and without a rectangle it draws only the labels", () => {
    render(<SheetFrontDiagram geometry={G} zones={[]} label="Sheet front" />);
    const svg = screen.getByRole("img", { name: "Sheet front" });
    expect(svg.querySelector("[data-zone]")).toBeNull();
    expect(svg.querySelector("[data-id-rect]")).toBeNull();
  });
});

