// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { I18nProvider } from "@lingui/react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeAll, describe, expect, it } from "vitest";
import { i18n, initI18n } from "../../i18n";
import { ReadingStepper } from "./ReadingStepper";

beforeAll(() => initI18n("en"));

function Harness({ step = 0.1, initial = "" }: { step?: number; initial?: string }) {
  const [text, setText] = useState(initial);
  return (
    <I18nProvider i18n={i18n}>
      <ReadingStepper label="Across at S1 · Z1 (mm)" text={text} onChange={setText} step={step} />
    </I18nProvider>
  );
}

describe("ReadingStepper", () => {
  it("stepper steps 0.1 mm by key and button", async () => {
    render(<Harness />);
    const field = screen.getByRole("textbox", { name: "Across at S1 · Z1 (mm)" });
    const plus = screen.getByRole("button", { name: "More: Across at S1 · Z1 (mm)" });
    const minus = screen.getByRole("button", { name: "Less: Across at S1 · Z1 (mm)" });

    await userEvent.click(plus);
    await userEvent.click(plus);
    expect(field).toHaveValue("0.2");
    field.focus();
    await userEvent.keyboard("{ArrowUp}");
    expect(field).toHaveValue("0.3");
    await userEvent.keyboard("{ArrowDown}{ArrowDown}");
    expect(field).toHaveValue("0.1");
    await userEvent.click(minus);
    expect(field).toHaveValue("0");
    // Counter-test: the size never goes below zero; the direction picker holds the sign.
    await userEvent.click(minus);
    await userEvent.keyboard("{ArrowDown}");
    expect(field).toHaveValue("0");
  });

  it("steps from a typed value that is off the grid onto the grid", async () => {
    render(<Harness initial="0,43" />);
    await userEvent.click(screen.getByRole("button", { name: "More: Across at S1 · Z1 (mm)" }));
    expect(screen.getByRole("textbox", { name: "Across at S1 · Z1 (mm)" })).toHaveValue("0.5");
  });

  it("follows the printed scale when the 0.5 mm step is chosen", async () => {
    render(<Harness step={0.5} />);
    const plus = screen.getByRole("button", { name: "More: Across at S1 · Z1 (mm)" });
    await userEvent.click(plus);
    await userEvent.click(plus);
    expect(screen.getByRole("textbox", { name: "Across at S1 · Z1 (mm)" })).toHaveValue("1");
  });

  it("asks for the decimal keyboard and keeps the buttons out of the tab order", () => {
    render(<Harness />);
    expect(screen.getByRole("textbox", { name: "Across at S1 · Z1 (mm)" })).toHaveAttribute("inputmode", "decimal");
    expect(screen.getByRole("button", { name: "More: Across at S1 · Z1 (mm)" })).toHaveAttribute("tabindex", "-1");
  });
});
