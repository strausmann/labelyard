// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { useState } from "react";
import { Button } from "react-aria-components";
import { PRIMARY_BUTTON } from "../../shell/SignIn";
import { ModalDialog, TextInput } from "../settings/controls";
import { SECONDARY_BUTTON } from "../ui";
import { useRestoreCalibration, type CalibrationHistoryEntry } from "./api";
import { useCalibrationErrorText } from "./errors";
import { valueText } from "./historyFormat";

/** Longest note the hub keeps with a history entry (`NOTE_MAX` of the backend). */
const NOTE_MAX = 500;

interface RestoreDialogProps {
  printerId: string;
  printerName: string;
  tray: string;
  trayLabel: string;
  /** The entry to use again; `null` keeps the dialog closed. */
  entry: CalibrationHistoryEntry | null;
  onClose: () => void;
}

/** "Use these values again?" (mockup M7): names the values it restores, takes an optional note, adds a `revert` entry. */
export function RestoreDialog({ printerId, printerName, tray, trayLabel, entry, onClose }: RestoreDialogProps) {
  const { t } = useLingui();
  return (
    <ModalDialog title={t`Use these values again?`} isOpen={entry !== null} onClose={onClose}>
      {entry ? <Body key={entry.id} printerId={printerId} printerName={printerName} tray={tray} trayLabel={trayLabel} entry={entry} onClose={onClose} /> : null}
    </ModalDialog>
  );
}

function Body({ printerId, printerName, tray, trayLabel, entry, onClose }: RestoreDialogProps & { entry: CalibrationHistoryEntry }) {
  const { t, i18n } = useLingui();
  const restore = useRestoreCalibration(printerId);
  const errorText = useCalibrationErrorText();
  const [note, setNote] = useState("");
  const v = (field: Parameters<typeof valueText>[0]) => valueText(field, entry.values, i18n.locale);
  const item = (label: string, value: string) => (
    <div role="group" aria-label={label} className="flex min-w-0 flex-col gap-0.5">
      <span className="text-small text-ink-2">{label}</span>
      <span className="font-mono font-bold [overflow-wrap:anywhere]">{value}</span>
    </div>
  );
  return (
    <>
      <p className="-mt-2 mb-0 text-small text-ink-2 [overflow-wrap:anywhere]">
        {t`${printerName} · ${trayLabel}. This adds a new entry; nothing is deleted.`}
      </p>
      <div className="grid grid-cols-2 gap-x-4 gap-y-3 rounded-r3 bg-sunken p-4">
        {item(t`Across`, `${v("offset_x_mm")} mm`)}
        {item(t`Down`, `${v("offset_y_mm")} mm`)}
        {item(t`Scale`, `${v("scale_x")} % / ${v("scale_y")} %`)}
        {item(t`Rotation`, `${v("rotation_deg")}°`)}
      </div>
      <TextInput label={t`Note (optional)`} value={note} onChange={setNote} maxLength={NOTE_MAX} description={t`Kept in the history with who restored it.`} />
      {restore.isError ? (
        <p role="alert" className="m-0 text-bad">
          {errorText(restore.error, t`The values could not be restored.`)}
        </p>
      ) : null}
      {/* The buttons stay at the bottom of the visible dialog, also with the on-screen keyboard open (spec 12). */}
      <div className="sticky -bottom-6 -mx-6 -mb-6 grid grid-cols-[auto_1fr] gap-3 border-t border-line bg-surface px-6 py-4 sm:flex sm:justify-end">
        <Button onPress={onClose} className={SECONDARY_BUTTON}>{t`Cancel`}</Button>
        <Button
          isDisabled={restore.isPending}
          onPress={() => restore.mutate({ tray, entryId: entry.id, note }, { onSuccess: onClose })}
          className={PRIMARY_BUTTON}
        >
          {t`Use these values`}
        </Button>
      </div>
    </>
  );
}
