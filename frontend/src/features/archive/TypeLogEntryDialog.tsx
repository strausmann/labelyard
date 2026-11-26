// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { useEffect, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { PRIMARY_BUTTON } from "../../shell/SignIn";
import { useCapabilities } from "../calibration/api";
import { ModalDialog, SelectField } from "../settings/controls";
import { subscribeViewport } from "../../shell/viewport";
import { SECONDARY_BUTTON } from "../ui";
import { LOG_NOTE_MAX, useAddLogNote, usePatchLogEntry, type LogVerdict, type SheetTypeLogEntry } from "./api";
import { useArchiveErrorText } from "./errors";
import { TextAreaField } from "./fields";

export const VERDICTS: LogVerdict[] = ["none", "fits", "sheet_geometry", "feed_variance", "printer"];

export function useVerdictNames(): Record<LogVerdict, string> {
  const { t } = useLingui();
  return { none: t`No verdict`, fits: t`Fits`, sheet_geometry: t`Sheet geometry`, feed_variance: t`Feed variance`, printer: t`Printer` };
}

type DialogState = { mode: "add" } | { mode: "edit"; entry: SheetTypeLogEntry } | null;

interface TypeLogEntryDialogProps {
  typeKey: string;
  typeName: string;
  state: DialogState;
  /** The calibration page's printer and tray: a new note names them, no choice. */
  fixed?: { printerId: string; printerName: string; tray: string; trayLabel: string };
  pagePrinters: { id: string; name: string }[];
  onClose: () => void;
}

/** "Add a note" and "Edit verdict and note" of the sheet-type log (mockup M12, note view). */
export function TypeLogEntryDialog({ typeKey, typeName, state, fixed, pagePrinters, onClose }: TypeLogEntryDialogProps) {
  const { t } = useLingui();
  const title = state?.mode === "edit" ? t`Verdict and note` : t`Add a note`;
  return (
    <ModalDialog title={title} isOpen={state !== null} onClose={onClose}>
      {state ? (
        <Body
          key={state.mode === "edit" ? state.entry.id : "add"}
          typeKey={typeKey}
          typeName={typeName}
          state={state}
          fixed={fixed}
          pagePrinters={pagePrinters}
          onClose={onClose}
        />
      ) : null}
    </ModalDialog>
  );
}

function Body({ typeKey, typeName, state, fixed, pagePrinters, onClose }: TypeLogEntryDialogProps & { state: NonNullable<DialogState> }) {
  const { t } = useLingui();
  const errorText = useArchiveErrorText();
  const names = useVerdictNames();
  const add = useAddLogNote();
  const patch = usePatchLogEntry();
  const editing = state.mode === "edit" ? state.entry : null;
  const [printer, setPrinter] = useState(fixed?.printerId ?? "none");
  const [tray, setTray] = useState(fixed?.tray ?? "none");
  const [verdict, setVerdict] = useState<LogVerdict>(editing?.verdict ?? "none");
  const [note, setNote] = useState(editing?.note ?? "");
  const capabilities = useCapabilities(!fixed && !editing && printer !== "none" ? printer : "");
  const trays = capabilities.data?.trays ?? [];
  const tooLong = note.length > LOG_NOTE_MAX;
  const pending = add.isPending || patch.isPending;
  const error = add.error ?? patch.error;
  // The note is the last field: when the on-screen keyboard covers the page, the dialog shrinks to the visible area and the
  // note would sit under its sticky buttons. Keep the focused note in sight inside the dialog (spec 12, mockup M12).
  const noteBox = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const keep = () => {
      const field = noteBox.current?.querySelector("textarea");
      // jsdom has no scrollIntoView; every browser does.
      if (field && document.activeElement === field && typeof field.scrollIntoView === "function") field.scrollIntoView({ block: "nearest" });
    };
    const later = () => requestAnimationFrame(keep);
    const node = noteBox.current;
    node?.addEventListener("focusin", later);
    const stop = subscribeViewport(later);
    return () => {
      node?.removeEventListener("focusin", later);
      stop();
    };
  }, []);

  const submit = () => {
    const text = note.trim() === "" ? null : note;
    if (editing) {
      patch.mutate({ key: typeKey, id: editing.id, patch: { verdict, note: text } }, { onSuccess: onClose });
      return;
    }
    add.mutate(
      {
        key: typeKey,
        body: {
          verdict,
          note: text,
          ...(printer !== "none" ? { printer_id: printer, ...(tray !== "none" ? { tray } : {}) } : {}),
        },
      },
      { onSuccess: onClose },
    );
  };

  return (
    <>
      <p className="-mt-2 mb-0 text-small text-ink-2 [overflow-wrap:anywhere]">
        {editing ? t`${typeName} · only the verdict and the note change.` : t`${typeName} · saved with your name.`}
      </p>
      {editing ? null : fixed ? (
        <p className="m-0 text-small [overflow-wrap:anywhere]">{t`About ${fixed.printerName} · ${fixed.trayLabel}`}</p>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          <SelectField
            label={t`Printer`}
            value={printer}
            options={[{ id: "none", label: t`No printer` }, ...pagePrinters.map((p) => ({ id: p.id, label: p.name }))]}
            onChange={(id) => {
              setPrinter(id);
              setTray("none");
            }}
          />
          <SelectField
            label={t`Tray`}
            value={tray}
            isDisabled={printer === "none"}
            options={[{ id: "none", label: t`Any tray` }, ...trays.map((x) => ({ id: x.key, label: x.label }))]}
            onChange={setTray}
          />
        </div>
      )}
      <SelectField label={t`Verdict`} value={verdict} options={VERDICTS.map((v) => ({ id: v, label: names[v] }))} onChange={(id) => setVerdict(id as LogVerdict)} />
      <div ref={noteBox} className="flex min-w-0 flex-col [&_textarea]:scroll-mb-24">
        <TextAreaField
          label={t`Note`}
          value={note}
          onChange={setNote}
          rows={4}
          placeholder={t`What did you see?`}
          description={
            <span className={tooLong ? "font-bold text-bad" : undefined}>{t`${note.length} of ${LOG_NOTE_MAX} characters`}</span>
          }
        />
      </div>
      {error ? (
        <p role="alert" className="m-0 text-bad">
          {errorText(error)}
        </p>
      ) : null}
      {/* The buttons stay at the bottom of the visible dialog, also with the on-screen keyboard open (spec 12). */}
      <div className="sticky -bottom-6 -mx-6 -mb-6 grid grid-cols-[auto_1fr] gap-3 border-t border-line bg-surface px-6 py-4 sm:flex sm:justify-end">
        <Button onPress={onClose} className={SECONDARY_BUTTON}>{t`Cancel`}</Button>
        <Button isDisabled={pending || tooLong} onPress={submit} className={PRIMARY_BUTTON}>
          {editing ? t`Save` : t`Add note`}
        </Button>
      </div>
    </>
  );
}
