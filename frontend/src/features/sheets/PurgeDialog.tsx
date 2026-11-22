// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import { useState } from "react";
import { Button } from "react-aria-components";
import { PRIMARY_BUTTON } from "../../shell/SignIn";
import { ModalDialog, TextInput } from "../settings/controls";
import { SECONDARY_BUTTON } from "../ui";
import { usePurgeSheets, type PurgeResult } from "./api";
import { useSheetErrorText } from "./errors";

const DEFAULT_DAYS = "365";
/** The codes listed before the confirmation; the rest is a count. */
const SHOWN_CODES = 12;
/** The most sheets one call purges (S5). */
const PURGE_LIMIT = 1000;

function parseDays(text: string): number | null {
  const value = Number(text.trim());
  return Number.isInteger(value) && value >= 1 && value <= 36500 ? value : null;
}

/**
 * "Purge discarded sheets" (admin, #204): the dry run comes first and shows what would go, the confirmation deletes
 * exactly that many (oldest first). While the server says more match, the next dry run follows the confirmation.
 */
export function PurgeDialog({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  const { t } = useLingui();
  const errorText = useSheetErrorText();
  const purge = usePurgeSheets();
  const [daysText, setDaysText] = useState(DEFAULT_DAYS);
  const [preview, setPreview] = useState<{ days: number; result: PurgeResult } | null>(null);
  const [purged, setPurged] = useState(0);
  const days = parseDays(daysText);

  function close() {
    purge.reset();
    setPreview(null);
    setPurged(0);
    setDaysText(DEFAULT_DAYS);
    onClose();
  }

  function check(forDays: number) {
    purge.mutate({ older_than_days: forDays, dry_run: true, limit: PURGE_LIMIT }, { onSuccess: (result) => setPreview({ days: forDays, result }) });
  }

  function confirm() {
    if (!preview) return;
    const forDays = preview.days;
    purge.mutate(
      { older_than_days: forDays, dry_run: false, limit: preview.result.codes.length },
      {
        onSuccess: (result) => {
          setPurged((n) => n + result.codes.length);
          // The batch is gone: its codes may never be offered again, even when the next dry run fails.
          setPreview(null);
          if (result.more) check(forDays);
          else setPreview({ days: forDays, result: { codes: [], dry_run: true, more: false } });
        },
      },
    );
  }

  const codes = preview?.result.codes ?? [];
  const shown = codes.slice(0, SHOWN_CODES);
  const rest = codes.length - shown.length;

  return (
    <ModalDialog title={t`Purge discarded sheets`} isOpen={isOpen} onClose={close}>
      <p>{t`Deletes discarded sheets with their cells for good. Their codes are never given out again; jobs that printed on them stay in the history.`}</p>
      <TextInput
        label={t`Discarded more than (days) ago`}
        type="number"
        inputMode="numeric"
        value={daysText}
        invalid={days === null}
        onChange={(value) => {
          setDaysText(value);
          setPreview(null);
          purge.reset();
        }}
      />
      {purged > 0 ? <p role="status">{plural(purged, { one: "# sheet purged.", other: "# sheets purged." })}</p> : null}
      {preview ? (
        <div role="status" className="flex flex-col gap-1.5">
          {codes.length === 0 ? (
            <p>{purged > 0 ? t`No further discarded sheet matches.` : t`No discarded sheet matches.`}</p>
          ) : (
            <>
              <p className="font-bold">
                {preview.result.more
                  ? plural(codes.length, { one: "The oldest # sheet goes in this step; more follow.", other: "The oldest # sheets go in this step; more follow." })
                  : plural(codes.length, { one: "# sheet would be purged:", other: "# sheets would be purged:" })}
              </p>
              <p className="font-mono text-small [overflow-wrap:anywhere]">
                {shown.join(", ")}
                {rest > 0 ? ` ${t`and ${rest} more`}` : ""}
              </p>
            </>
          )}
        </div>
      ) : null}
      {purge.isError ? (
        <p role="alert" className="font-bold text-bad">
          {errorText(purge.error, t`The sheets could not be purged.`)}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-3">
        {preview && codes.length > 0 ? (
          <Button isDisabled={purge.isPending} onPress={confirm} className={PRIMARY_BUTTON}>
            {plural(codes.length, { one: "Purge # sheet", other: "Purge # sheets" })}
          </Button>
        ) : (
          <Button isDisabled={purge.isPending || days === null} onPress={() => days !== null && check(days)} className={PRIMARY_BUTTON}>
            {t`Check what would go`}
          </Button>
        )}
        <Button onPress={close} className={SECONDARY_BUTTON}>
          {preview && codes.length === 0 ? t`Close` : t`Cancel`}
        </Button>
      </div>
    </ModalDialog>
  );
}
