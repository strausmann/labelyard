// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { useState } from "react";
import { Button, Label, Radio, RadioGroup } from "react-aria-components";
import { PRIMARY_BUTTON } from "../../shell/SignIn";
import { HINT, LABEL, ModalDialog } from "../settings/controls";
import { SECONDARY_BUTTON } from "../ui";
import { useOpenCheckSheet, usePageLayout, type CheckSheetMode, type PageLayoutSummary } from "./api";

interface TypeRef {
    key: string;
    version: number;
}

/**
 * What a page will lack on this printer and tray, in words, before it is printed (design 7.2): fewer than four page
 * codes, margin text left out (operator rule of 2026-11-25: below 4.5 pt a margin block is left out), a page that names
 * its run nowhere, and, on a check sheet, later passes without their run line.
 */
export function useLayoutNotes(): (layout: PageLayoutSummary, options?: { laterPasses: boolean }) => string[] {
    const { t } = useLingui();
    return (layout, options = { laterPasses: false }) =>
        layoutNotes(layout, options, {
            nowhere: t`This page names its run nowhere: no page code and no margin text fit on this printer. Write the run number from the history on the sheet after printing.`,
            someCodes: (codes) => t`This sheet type leaves room for ${codes} of the 4 page codes on this printer.`,
            noCodes: t`This sheet type leaves no room for the page codes on this printer; the margin text names the run.`,
            noText: t`This page prints without margin text; the page codes carry the run and correction.`,
            noTop: t`This page prints without its top margin text (run, date, sheet type); the page codes carry them.`,
            noBottom: t`This page prints without its bottom margin text (the correction); the page codes carry it.`,
            noRunLine: t`Later check passes on this sheet will print without their run line.`,
        });
}

interface NoteWords {
    nowhere: string;
    someCodes: (codes: number) => string;
    noCodes: string;
    noText: string;
    noTop: string;
    noBottom: string;
    noRunLine: string;
}

/** The decision behind ``useLayoutNotes``, without the words: which notes a layout calls for. */
export function layoutNotes(layout: PageLayoutSummary, options: { laterPasses: boolean }, words: NoteWords): string[] {
    const notes: string[] = [];
    const codes = layout.page_codes;
    const top = layout.margin_text.top === "omitted";
    const bottom = layout.margin_text.bottom === "omitted";
    if (codes === 0 && top && bottom) {
        notes.push(words.nowhere);
    } else {
        if (codes > 0 && codes < 4) notes.push(words.someCodes(codes));
        if (codes === 0) notes.push(words.noCodes);
        if (top && bottom) notes.push(words.noText);
        else if (top) notes.push(words.noTop);
        else if (bottom) notes.push(words.noBottom);
    }
    if (options.laterPasses && layout.passes_total > 1 && layout.run_line_slots < layout.passes_total - 1) {
        notes.push(words.noRunLine);
    }
    return notes;
}

/** The notes as a list a screen reader announces as one note; nothing when there is nothing to say. */
export function LayoutNotes({ notes, label }: { notes: string[]; label: string }) {
    if (notes.length === 0) return null;
    return (
        <div role="note" aria-label={label} className="rounded-r2 bg-accent-soft p-3 text-small text-ink">
            <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
                {notes.map((note) => (
                    <li key={note} className="flex gap-2 [overflow-wrap:anywhere]">
                        <svg
                            viewBox="0 0 10 10"
                            width="10"
                            height="10"
                            aria-hidden="true"
                            focusable="false"
                            className="mt-1 shrink-0 fill-current text-warn"
                        >
                            <circle cx="5" cy="5" r="4" />
                        </svg>
                        <span>{note}</span>
                    </li>
                ))}
            </ul>
        </div>
    );
}

/** Inline notes for the calibration page (printed without correction, no dialog in front of it). */
export function CalibrationPageNotes({ printerId, tray, type }: { printerId: string; tray: string; type: TypeRef }) {
    const { t } = useLingui();
    const notesOf = useLayoutNotes();
    const layout = usePageLayout(printerId, tray, type, "calibration_page");
    if (!layout.data) return null;
    return <LayoutNotes notes={notesOf(layout.data)} label={t`Before you print`} />;
}

interface CheckPrintDialogProps {
    printerId: string;
    tray: string;
    type: TypeRef;
    isOpen: boolean;
    onClose: () => void;
    /** Prints: a pass of the open sheet (`next`) or pass 1 of a new one (`new`). */
    onPrint: (sheet: CheckSheetMode) => void;
}

/**
 * Before a check print (design 7.2, mockup M3): print the next pass on the sheet that is already in use ("pass 2 of
 * 3"), or start a new sheet, with what the page will lack on this printer said first.
 */
export function CheckPrintDialog({ printerId, tray, type, isOpen, onClose, onPrint }: CheckPrintDialogProps) {
    const { t } = useLingui();
    return (
        <ModalDialog title={t`Print the check page`} isOpen={isOpen} onClose={onClose}>
            {isOpen ? <Body printerId={printerId} tray={tray} type={type} onClose={onClose} onPrint={onPrint} /> : null}
        </ModalDialog>
    );
}

function Body({ printerId, tray, type, onClose, onPrint }: Omit<CheckPrintDialogProps, "isOpen">) {
    const { t } = useLingui();
    const notesOf = useLayoutNotes();
    const layout = usePageLayout(printerId, tray, type, "check_page");
    const open = useOpenCheckSheet(printerId, tray, type);
    const loading = layout.isPending || open.isPending;
    const sheet = open.data ?? null;
    // The open sheet is the obvious next step; a person who put a fresh sheet in the tray picks "New sheet".
    const samePass = (first: number | undefined) =>
        first !== undefined
            ? t`Put the sheet of run #${first} back into the tray. The new crosses land beside the earlier ones.`
            : t`Put the sheet back into the tray. The new crosses land beside the earlier ones.`;
    const [choice, setChoice] = useState<CheckSheetMode | null>(null);
    const mode: CheckSheetMode = choice ?? (sheet ? "next" : "new");
    const total = sheet?.passes_total ?? layout.data?.passes_total ?? 1;
    const passNo = mode === "next" && sheet ? sheet.next_pass : 1;

    const notes = layout.data
        ? passNo === 1
            ? notesOf(layout.data, { laterPasses: true })
            : // Passes 2 and 3 print no codes and no margin text: only the run line can be missing.
              notesOf(
                  {
                      ...layout.data,
                      page_codes: 4,
                      margin_text: { top: "full", bottom: "full" },
                      run_line_slots: sheet?.run_line_slots ?? layout.data.run_line_slots,
                  },
                  { laterPasses: true },
              )
        : [];

    return (
        <>
            {loading ? (
                <p role="status" className={`${HINT} m-0`}>{t`Loading…`}</p>
            ) : sheet ? (
                <RadioGroup value={mode} onChange={(v) => setChoice(v as CheckSheetMode)} className="flex min-w-0 flex-col gap-1">
                    <Label className={LABEL}>{t`Which sheet?`}</Label>
                    <SheetOption
                        value="next"
                        title={t`Print on the same sheet (pass ${sheet.next_pass} of ${sheet.passes_total})`}
                        description={samePass(sheet.passes[0]?.run_no)}
                    />
                    <SheetOption value="new" title={t`New sheet`} description={t`Pass 1 of ${total} on a fresh sheet of labels.`} />
                </RadioGroup>
            ) : (
                <p className="m-0 [overflow-wrap:anywhere]">
                    {total > 1
                        ? t`This sheet takes ${total} check passes. This is pass 1, on a fresh sheet: keep the sheet for the next check.`
                        : t`This sheet takes one check pass: the check page uses up the sheet.`}
                </p>
            )}
            {!loading ? <LayoutNotes notes={notes} label={t`Before you print`} /> : null}
            {/* The buttons stay at the bottom of the visible dialog, also with the on-screen keyboard open (spec 12). */}
            <div className="sticky -bottom-6 -mx-6 -mb-6 grid grid-cols-[auto_1fr] gap-3 border-t border-line bg-surface px-6 py-4 sm:flex sm:justify-end">
                <Button onPress={onClose} className={SECONDARY_BUTTON}>{t`Cancel`}</Button>
                <Button isDisabled={loading} onPress={() => onPrint(mode)} className={PRIMARY_BUTTON}>
                    {total > 1 ? t`Print pass ${passNo} of ${total}` : t`Print the check page`}
                </Button>
            </div>
        </>
    );
}

const RADIO =
    "group flex min-h-11 cursor-pointer items-start gap-3 rounded-r2 py-2 data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link";

function SheetOption({ value, title, description }: { value: CheckSheetMode; title: string; description: string }) {
    return (
        <Radio value={value} className={RADIO}>
            <span
                aria-hidden="true"
                className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full border-2 border-line-strong bg-sunken group-data-[selected]:border-accent"
            >
                <span className="size-2.5 scale-0 rounded-full bg-accent transition-transform duration-150 ease-out group-data-[selected]:scale-100" />
            </span>
            <span className="flex min-w-0 flex-col">
                <span className="font-bold text-ink [overflow-wrap:anywhere]">{title}</span>
                <span className="text-small text-ink-2 [overflow-wrap:anywhere]">{description}</span>
            </span>
        </Radio>
    );
}
