// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { I18nProvider } from "@lingui/react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { i18n, initI18n } from "../../i18n";
import { GEOMETRY_L4736 } from "../test-api";
import type { PreprintPresetRead } from "./api";
import { PreprintZonesEditor, type ZonesDraft } from "./PreprintZonesEditor";
import type { ZoneRefusal } from "./preprint";

const FRONT: PreprintPresetRead = {
  key: "example-front",
  name: "Example front print",
  page_size: "A4",
  origin: "custom",
  types_in_use: 0,
  zones: [
    { x_mm: 10, y_mm: 6.5, w_mm: 34, h_mm: 9, note: "Logo" },
    { x_mm: 49, y_mm: 7.5, w_mm: 51, h_mm: 7, note: "Product description" },
  ],
};
const LETTER: PreprintPresetRead = { ...FRONT, key: "letter-front", name: "Letter front", page_size: "Letter" };
const BOTTOM = { x_mm: 10, y_mm: 288.4, w_mm: 190, h_mm: 5, note: "Bottom line" };

function Harness({ start, refusal, seen }: { start: ZonesDraft; refusal?: ZoneRefusal | null; seen: ZonesDraft[] }) {
  const [value, setValue] = useState(start);
  return (
    <PreprintZonesEditor
      geometry={GEOMETRY_L4736}
      presets={[FRONT, LETTER]}
      linked={null}
      value={value}
      refusal={refusal ?? null}
      onChange={(v) => {
        setValue(v);
        seen.push(v);
      }}
    />
  );
}

async function show(start: ZonesDraft, refusal: ZoneRefusal | null = null) {
  await initI18n("en");
  const seen: ZonesDraft[] = [];
  render(
    <I18nProvider i18n={i18n}>
      <Harness start={start} refusal={refusal} seen={seen} />
    </I18nProvider>,
  );
  return seen;
}

const rows = () => within(screen.getByRole("list", { name: "Preprinted areas" })).getAllByRole("listitem");

describe("preprinted areas editor", () => {
  it("zone editor accepts a decimal comma", async () => {
    const seen = await show({ preset: null, zones: [] });
    await userEvent.click(screen.getByRole("button", { name: "Add area" }));
    const dialog = await screen.findByRole("dialog", { name: "New area 1" });
    const box = within(dialog);
    await userEvent.type(box.getByRole("textbox", { name: "Note" }), "Logo");
    await userEvent.type(box.getByRole("textbox", { name: "x" }), "12,5");
    await userEvent.type(box.getByRole("textbox", { name: "y" }), "6");
    await userEvent.type(box.getByRole("textbox", { name: "Width" }), "30,25");
    await userEvent.type(box.getByRole("textbox", { name: "Height" }), "9");
    await userEvent.click(box.getByRole("button", { name: "Save area" }));
    expect(seen.at(-1)).toEqual({ preset: null, zones: [{ x_mm: 12.5, y_mm: 6, w_mm: 30.25, h_mm: 9, note: "Logo" }] });
    expect(rows()).toHaveLength(1);
    expect(rows()[0]).toHaveTextContent("x 12.5 · y 6.0 · width 30.25 · height 9.0 mm");
  });

  it("refuses a value that is no number, and warns before saving a zone over a label", async () => {
    const seen = await show({ preset: null, zones: [] });
    await userEvent.click(screen.getByRole("button", { name: "Add area" }));
    const box = within(await screen.findByRole("dialog"));
    await userEvent.type(box.getByRole("textbox", { name: "x" }), "12");
    await userEvent.type(box.getByRole("textbox", { name: "y" }), "1.234,5");
    await userEvent.type(box.getByRole("textbox", { name: "Width" }), "30");
    await userEvent.type(box.getByRole("textbox", { name: "Height" }), "6");
    await userEvent.click(box.getByRole("button", { name: "Save area" }));
    expect(box.getByText("Enter each value as a number in mm, for example 12.5.")).toBeVisible();
    expect(seen).toHaveLength(0);
    await userEvent.clear(box.getByRole("textbox", { name: "y" }));
    await userEvent.type(box.getByRole("textbox", { name: "y" }), "22");
    // The fixture's first row starts at 21.5 mm.
    expect(box.getByText("Covers label 1 (row 1, column 1). Move it above 21.5 mm or shrink it so it stays out of the labels.")).toBeVisible();
  });

  it("preset picker adds its zones above the own list", async () => {
    const seen = await show({ preset: null, zones: [BOTTOM] });
    expect(rows()).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: /Vendor preset/ }));
    // Only presets of the type's page size are offered.
    expect(screen.queryByRole("option", { name: "Letter front" })).toBeNull();
    await userEvent.click(await screen.findByRole("option", { name: "Example front print" }));
    expect(seen.at(-1)).toEqual({ preset: "example-front", zones: [BOTTOM] });
    const list = rows();
    expect(list).toHaveLength(3);
    expect(list[0]).toHaveTextContent(/^1Logo/);
    expect(within(list[0]!).getByText("Preset")).toBeVisible();
    expect(list[1]).toHaveTextContent(/^2Product description/);
    expect(list[2]).toHaveTextContent(/^3Bottom line/);
    expect(within(list[2]!).queryByText("Preset")).toBeNull();
    // A preset zone is changed by unlinking, not in place.
    expect(within(list[0]!).queryByRole("button", { name: /Edit/ })).toBeNull();
    expect(within(list[2]!).getByRole("button", { name: "Edit Bottom line" })).toBeVisible();
  });

  it("unlinking keeps a copy of the preset's zones as own zones", async () => {
    const seen = await show({ preset: "example-front", zones: [BOTTOM] });
    await userEvent.click(screen.getByRole("button", { name: "Unlink preset and keep a copy" }));
    expect(seen.at(-1)).toEqual({ preset: null, zones: [...FRONT.zones, BOTTOM] });
    expect(screen.queryAllByText("Preset")).toHaveLength(0);
  });

  it("removes an own zone", async () => {
    const seen = await show({ preset: null, zones: [BOTTOM, { ...BOTTOM, note: "Second" }] });
    await userEvent.click(screen.getByRole("button", { name: "Remove Bottom line" }));
    expect(seen.at(-1)).toEqual({ preset: null, zones: [{ ...BOTTOM, note: "Second" }] });
  });

  it("marks the zone the server refused, numbered after the preset's", async () => {
    const dust = { x_mm: 12, y_mm: 22, w_mm: 30, h_mm: 6, note: "Dust cover" };
    await show({ preset: "example-front", zones: [BOTTOM, dust] }, { list: "own", index: 1, code: "preprint_zone_overlaps_cell" });
    const refused = rows()[3]!;
    expect(refused).toHaveTextContent(/^4Dust cover/);
    expect(within(refused).getByText(/Covers label 1 \(R1 C1\)\./)).toBeVisible();
    expect(within(refused).getByText("preprint_zone_overlaps_cell")).toBeVisible();
  });
});
