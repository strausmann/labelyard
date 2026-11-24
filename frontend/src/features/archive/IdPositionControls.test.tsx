// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { I18nProvider } from "@lingui/react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { i18n, initI18n } from "../../i18n";
import { IdPositionControls, type PositionDraft } from "./IdPositionControls";

function Harness({ start, onValue }: { start: PositionDraft; onValue?: (v: PositionDraft) => void }) {
  const [value, setValue] = useState(start);
  return (
    <IdPositionControls
      value={value}
      onChange={(v) => {
        setValue(v);
        onValue?.(v);
      }}
    />
  );
}

async function show(start: PositionDraft, language: "en" | "de" = "en", onValue?: (v: PositionDraft) => void) {
  await initI18n(language);
  render(
    <I18nProvider i18n={i18n}>
      <Harness start={start} onValue={onValue} />
    </I18nProvider>,
  );
}

describe("sheet code position controls", () => {
  it("offset stepper accepts a decimal comma and steps 0.5 mm", async () => {
    const seen: PositionDraft[] = [];
    await show({ band: "top", align: "end", offset: "54.0" }, "en", (v) => seen.push(v));
    const field = screen.getByRole("textbox", { name: "Offset from that end" });
    await userEvent.clear(field);
    await userEvent.type(field, "2,5");
    await userEvent.click(screen.getByRole("button", { name: "Increase the offset by 0.5 mm" }));
    expect(field).toHaveValue("3.0");
    await userEvent.click(screen.getByRole("button", { name: "Decrease the offset by 0.5 mm" }));
    await userEvent.click(screen.getByRole("button", { name: "Decrease the offset by 0.5 mm" }));
    expect(field).toHaveValue("2.0");
    expect(seen.at(-1)).toEqual({ band: "top", align: "end", offset: "2.0" });
  });

  it("never steps below zero and writes the German decimal comma", async () => {
    await show({ band: "auto", align: "start", offset: "0,2" }, "de");
    const field = screen.getByRole("textbox", { name: "Abstand von diesem Ende" });
    await userEvent.click(screen.getByRole("button", { name: "Abstand um 0,5 mm verkleinern" }));
    expect(field).toHaveValue("0,0");
    await userEvent.click(screen.getByRole("button", { name: "Abstand um 0,5 mm vergrößern" }));
    expect(field).toHaveValue("0,5");
  });

  it("band and start are radio groups that report the choice", async () => {
    const seen: PositionDraft[] = [];
    await show({ band: "auto", align: "start", offset: "0.0" }, "en", (v) => seen.push(v));
    expect(screen.getByRole("radio", { name: "Automatic" })).toBeChecked();
    await userEvent.click(screen.getByRole("radio", { name: "Bottom" }));
    await userEvent.click(screen.getByRole("radio", { name: "Right or bottom" }));
    expect(seen.at(-1)).toEqual({ band: "bottom", align: "end", offset: "0.0" });
  });
});
