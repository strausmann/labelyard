// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { useState } from "react";
import { Button } from "react-aria-components";
import { HINT, SelectField } from "../settings/controls";
import { formatMm, pageSizeMm } from "../sheets/cellState";
import { LinerPreview } from "../sheets/backSide/LinerPreview";
import { placeStrip, widestMargin } from "../sheets/backSide/strip";
import { PANEL, PRIMARY_BUTTON, SECONDARY_BUTTON } from "../ui";
import { useBackingDesigns, usePatchType, type SheetTypeDetail } from "./api";
import { useArchiveErrorText } from "./errors";
import { ChosenPlace, MarginPosition, OnYourPrinters, type TrayChoice } from "./MarginPlacement";

type Choice = "margin" | "back_side" | "cell";
const PLAIN = "";

/** Cell 1 says where it is; the rest are numbers (a 400-cell type lists 400 of them). */
export function cellOptions(total: number, firstWord: string): { id: string; label: string }[] {
  return Array.from({ length: total }, (_, i) => ({ id: String(i + 1), label: i === 0 ? firstWord : String(i + 1) }));
}

const OTHER =
  "inline-flex min-h-11 cursor-pointer items-center rounded-r1 px-1 font-bold text-link underline underline-offset-2 data-[disabled]:cursor-not-allowed data-[disabled]:text-ink-3 data-[disabled]:no-underline data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link";

/** The chosen place as one card (mockup M2): its name, "Chosen", and what it means for printing. */
function ChosenCard({ choice, automatic }: { choice: Choice; automatic: Choice }) {
  const { t } = useLingui();
  const title = { margin: t`In the margin`, back_side: t`On the back of the liner`, cell: t`In a label cell` }[choice];
  const text = {
    margin: t`Prints the code in the free band around the labels. No extra pass through the printer.`,
    back_side: t`When you activate a new sheet, the hub asks you to turn it over and prints the code on the back of the liner. One extra pass through the printer.`,
    cell: t`No second pass. One cell holds the code and cannot take a label.`,
  }[choice];
  return (
    <div role="group" aria-labelledby="chosen-placement" className="flex flex-col gap-1.5 rounded-r3 border-2 border-accent bg-surface p-4">
      <span className="flex flex-wrap items-center gap-2">
        <span id="chosen-placement" className="font-bold">{title}</span>
        <span className="rounded-full bg-info-soft px-2 py-0.5 text-small font-bold text-ink">{t({ message: "Chosen", context: "placement" })}</span>
        {automatic === choice ? <DefaultBadge /> : null}
      </span>
      <span className="text-ink-2">{text}</span>
    </div>
  );
}

/**
 * "Where the code goes" of a sheet type (#350, mockup 350-1): margin, back side or a reserved cell, the
 * cell number, and the backing design the back preview is drawn from. `auto` shows the option it resolves
 * to, with the note that it was chosen automatically; saving the automatic option keeps `auto`.
 */
export function IdPlacementSection({ type, admin }: { type: SheetTypeDetail; admin: boolean }) {
  const { t, i18n } = useLingui();
  const errorText = useArchiveErrorText();
  const patch = usePatchType();
  const designs = useBackingDesigns();
  const g = type.geometry;
  const total = g.cols * g.rows;
  // The server decides what `auto` means (M7): the client never re-derives it from the geometry.
  const automatic: Choice = type.id_placement_auto;
  const marginOk = automatic === "margin";
  const stored: Choice = type.id_placement === "auto" ? automatic : type.id_placement;
  const [choice, setChoice] = useState<Choice>(stored);
  /** "Choose automatically" was pressed on an explicit setting: saving writes `auto` again. */
  const [backToAuto, setBackToAuto] = useState(false);
  const [cell, setCell] = useState(String(type.id_cell ?? 1));
  const [design, setDesign] = useState(type.backing_design?.key ?? PLAIN);
  const [saved, setSaved] = useState(false);
  /** The tray picked on a printer row; the chosen place follows it. */
  const [trays, setTrays] = useState<TrayChoice>({});

  const sameSize = (designs.data ?? []).filter((d) => d.page_size === g.page_size);
  const chosenDesign = sameSize.find((d) => d.key === design) ?? (design === type.backing_design?.key ? type.backing_design : null);
  const strips = chosenDesign?.strips ?? [];
  const where = placeStrip(strips);
  const widest = formatMm(Math.max(0, widestMargin(g)), i18n.locale);
  const autoNow = type.id_placement === "auto" || (backToAuto && choice === automatic);
  const dirty =
    choice !== stored ||
    (backToAuto && type.id_placement !== "auto") ||
    (choice === "cell" && cell !== String(type.id_cell ?? 1)) ||
    design !== (type.backing_design?.key ?? PLAIN);

  function save() {
    const setting = choice === automatic && autoNow ? "auto" : choice;
    patch.mutate(
      {
        key: type.key,
        patch: {
          id_placement: setting,
          ...(choice === "cell" ? { id_cell: Number(cell) } : {}),
          backing_design: design === PLAIN ? null : design,
        },
      },
      { onSuccess: () => setSaved(true) },
    );
  }
  function reset() {
    setChoice(stored);
    setBackToAuto(false);
    setCell(String(type.id_cell ?? 1));
    setDesign(type.backing_design?.key ?? PLAIN);
  }

  const pick = (next: Choice) => {
    setChoice(next);
    setBackToAuto(false);
    setSaved(false);
  };
  const others: { id: string; label: string; disabled?: boolean; choose: () => void }[] = [
    ...(autoNow
      ? []
      : [
          {
            id: "auto",
            label: t({ message: "Automatic", context: "placement" }),
            choose: () => {
              setChoice(automatic);
              setBackToAuto(true);
              setSaved(false);
            },
          },
        ]),
    ...(choice !== "margin" ? [{ id: "margin", label: t`In the margin`, disabled: !marginOk, choose: () => pick("margin") }] : []),
    ...(choice !== "back_side" ? [{ id: "back_side", label: t`Back of the liner`, choose: () => pick("back_side") }] : []),
    ...(choice !== "cell" ? [{ id: "cell", label: t`A label cell`, choose: () => pick("cell") }] : []),
  ];

  return (
    <div className="grid min-w-0 items-start gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,18rem)]">
      <section aria-labelledby="placement-h" className="flex min-w-0 flex-col gap-4">
        <div className="flex flex-col gap-1">
          <h3 id="placement-h" className="text-section font-bold">{t`Where the sheet code goes`}</h3>
          <p className="text-ink-2">{t`The sheet code identifies a sheet when you scan it. Choose where the hub prints it.`}</p>
        </div>
        <ChosenCard choice={choice} automatic={automatic} />
        {admin ? (
          <div className="flex flex-col gap-1">
            <p className="m-0 flex flex-wrap items-center gap-x-1 text-small text-ink-2">
              <span id="other-choices">{t`Other choices:`}</span>
              {others.map((o, i) => (
                <span key={o.id} className="inline-flex items-center gap-x-1">
                  {i > 0 ? <span aria-hidden="true">·</span> : null}
                  <Button
                    aria-describedby="other-choices"
                    isDisabled={o.disabled}
                    onPress={o.choose}
                    className={OTHER}
                  >
                    {o.label}
                  </Button>
                </span>
              ))}
            </p>
            {!marginOk && choice !== "margin" ? (
              <p className="m-0 text-small text-ink-2">
                {t`In the margin is not available for this sheet: no margin is wide enough for the code (the widest is ${widest}).`}
              </p>
            ) : null}
          </div>
        ) : null}
        {choice === "cell" ? (
          <div className="flex max-w-xs flex-col gap-1">
            <SelectField label={t`Cell`} value={cell} options={cellOptions(total, t`1 (top left)`)} onChange={setCell} isDisabled={!admin} />
            <p className={HINT}>{t`Choose any cell. It is left out of planning.`}</p>
          </div>
        ) : null}
        {autoNow ? <p className="m-0 text-small text-ink-2">{t`Chosen automatically from the usable margin. You can change it.`}</p> : null}
        {choice === "back_side" ? (
        <div className="flex max-w-sm flex-col gap-1">
          <SelectField
            label={t`Print on the back of the liner`}
            value={design}
            options={[{ id: PLAIN, label: t`Plain liner` }, ...sameSize.map((d) => ({ id: d.key, label: d.name }))]}
            onChange={(v) => {
              setDesign(v);
              setSaved(false);
            }}
            isDisabled={!admin}
          />
          <p className={HINT}>{t`The free strips of the print decide where the code goes on the back.`}</p>
        </div>
        ) : null}
        {admin ? (
          <div className="flex flex-wrap items-center gap-3 border-t border-line pt-4">
            <Button onPress={save} isDisabled={!dirty || patch.isPending} className={PRIMARY_BUTTON}>
              {t`Save`}
            </Button>
            <Button onPress={reset} isDisabled={!dirty} className={SECONDARY_BUTTON}>
              {t`Cancel`}
            </Button>
            <span role="status" className="text-small text-ink-2">
              {saved && !dirty ? t`Saved.` : null}
            </span>
          </div>
        ) : null}
        {patch.isError ? <p role="alert" className="font-bold text-bad">{errorText(patch.error)}</p> : null}
        {choice === "margin" ? (
          <>
            <MarginPosition type={type} admin={admin} />
            <OnYourPrinters type={type} trays={trays} onTray={(printer, tray) => setTrays((prev) => ({ ...prev, [printer]: tray }))} />
          </>
        ) : null}
      </section>
      {choice === "margin" ? (
        <ChosenPlace type={type} trays={trays} />
      ) : (
      <aside aria-labelledby="liner-h" className={`${PANEL} flex h-fit flex-col gap-3`}>
        <h4 id="liner-h" className="font-bold">{t`Preview`}</h4>
        <div className="flex justify-center rounded-r2 bg-sunken p-3">
          <LinerPreview page={pageSizeMm(g)} strips={strips} />
        </div>
        <p className="text-small text-ink-2">
          {where ? t`Back of the liner. The code goes in the marked strip.` : t`No free strip of this print takes the code.`}
        </p>
      </aside>
      )}
    </div>
  );
}

function DefaultBadge() {
  const { t } = useLingui();
  return <span className="rounded-full bg-accent-soft px-2 py-0.5 text-small font-bold text-ink">{t`Default for this sheet`}</span>;
}
