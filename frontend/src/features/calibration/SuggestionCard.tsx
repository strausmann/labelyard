// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { Trans, useLingui } from "@lingui/react/macro";
import { useState } from "react";
import { Button } from "react-aria-components";
import { ApiError } from "../../api/client";
import { useInfo } from "../../app/session";
import { useApplySuggestion, useSheetType, type SheetTypeRead } from "../archive/api";
import { browserLabel, buildFeedbackUrl } from "../archive/feedbackLink";
import { HINT } from "../settings/controls";
import { typeLabelWithKey } from "../sheets/typeLabel";
import { PANEL, PRIMARY_BUTTON, SECONDARY_BUTTON, SECTION_HEADING } from "../ui";
import type { FitResponse } from "./api";
import { useCalibrationErrorText } from "./errors";

type Reason = NonNullable<FitResponse["suggestion_unavailable"]>;
type Suggestion = NonNullable<FitResponse["suggestion"]>;

/** The print the readings belong to, as far as the card needs it. */
export interface SuggestionRun {
  id: string;
  run_no: number;
  kind: "calibration_page" | "check_page" | "probe_page";
}

/** The suggested fields, in the order the card lists them, with the geometry field each one changes. */
const FIELDS = [
  ["pitch_x_hmm", "pitch_x", "pitch_x_mm"],
  ["pitch_y_hmm", "pitch_y", "pitch_y_mm"],
  ["margin_left_hmm", "margin_left", "margin_left_mm"],
  ["margin_top_hmm", "margin_top", "margin_top_mm"],
] as const;

/** Why a run gives no suggestion, in words (the reasons of `FitResponse.suggestion_unavailable`). */
export function useUnavailableText(): (reason: Reason | string | undefined) => string {
  const { t } = useLingui();
  return (reason) => {
    switch (reason) {
      case "same_type":
        return t`The saved correction was measured on this same type, so a pitch error would be absorbed into it. Print a check page on another sheet type first.`;
      case "no_check_run":
        return t`This check print was made without a correction, so what it shows is the printer, not the sheet. Print the check page with the correction.`;
      case "unknown_correction":
        return t`It is not known which sheet type the saved correction was measured on (it was typed in, or its history is gone), so the sheet cannot be told apart from the printer.`;
      default:
        return t`These readings give no geometry suggestion.`;
    }
  };
}

export interface SuggestionCardProps {
  /** The sheet type of the page; the suggestion would add a version of it. */
  type: SheetTypeRead;
  run: SuggestionRun | null;
  response: FitResponse;
  /** How many readings the fit took. */
  readings: number;
  /** The type the stored correction was measured on, as shown elsewhere ("Vendor code (KEY)"); null when not known. */
  measuredOn: string | null;
  admin: boolean;
}

/**
 * The geometry suggestion of a check print (#240, design 7.4): what the readings say about the sheet once
 * the correction of another type has taken the printer out. "Apply as new version" is an admin action and
 * also opens the prefilled feedback issue (#230); the issue link itself is there for every role.
 * Without a suggestion the card says why, for check prints only (a calibration page never gives one).
 */
export function SuggestionCard({ type, run, response, readings, measuredOn, admin }: SuggestionCardProps) {
  const { t, i18n } = useLingui();
  const unavailableText = useUnavailableText();
  const suggestion = response.suggestion ?? null;
  if (!suggestion) {
    if (!response.suggestion_unavailable || run?.kind !== "check_page") return null;
    return (
      <div role="note" className="flex gap-3 rounded-r3 bg-info-soft p-4 text-small">
        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" className="mt-0.5 shrink-0 fill-none stroke-current stroke-2 text-link">
          <circle cx="12" cy="12" r="8.5" />
          <path d="M12 11v5.5M12 7.8h.01" />
        </svg>
        <div className="flex min-w-0 flex-col gap-1">
          <strong>{t`No suggestion for this one`}</strong>
          <span>{unavailableText(response.suggestion_unavailable)}</span>
        </div>
      </div>
    );
  }
  return <Suggested type={type} run={run} suggestion={suggestion} readings={readings} measuredOn={measuredOn} admin={admin} locale={i18n.locale} />;
}

function Suggested({
  type,
  run,
  suggestion,
  readings,
  measuredOn,
  admin,
  locale,
}: Omit<SuggestionCardProps, "response"> & { suggestion: Suggestion; locale: string }) {
  const { t } = useLingui();
  const errorText = useCalibrationErrorText();
  const unavailableText = useUnavailableText();
  const info = useInfo();
  const detail = useSheetType(type.key);
  const apply = useApplySuggestion();
  const [opened, setOpened] = useState<boolean | null>(null);

  const version = detail.data?.versions.find((v) => v.version === type.current_version);
  const geometry = (version?.geometry ?? type.geometry) as unknown as Record<string, number | null | undefined>;
  const mm = (v: number) => new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2, useGrouping: false }).format(v);
  const names: Record<(typeof FIELDS)[number][1], string> = {
    pitch_x: t`Pitch across`,
    pitch_y: t`Pitch down`,
    margin_left: t`Left margin`,
    margin_top: t`Top margin`,
  };
  const proposed = FIELDS.flatMap(([hmm, field, body]) => {
    const value = suggestion[hmm];
    return value === null ? [] : [{ field, body, mm: value / 100, now: geometry[field] ?? null }];
  });

  const hubVersion = info.data?.version;
  const issue =
    run && version && hubVersion
      ? buildFeedbackUrl({
          kind: "wrong",
          sheetType: type,
          version,
          hubVersion,
          scheme: window.location.protocol === "https:" ? "https" : "http",
          browser: browserLabel(navigator.userAgent),
          suggestion: { runNo: run.run_no, ...suggestion },
        })
      : undefined;

  const applyNow = () => {
    if (!run) return;
    setOpened(null);
    const body = { run_id: run.id, ...Object.fromEntries(proposed.map((p) => [p.body, p.mm])) };
    apply.mutate(
      { key: type.key, body },
      {
        onSuccess: () => {
          // The issue opens with the result, within the press that applied it (the browser lets it open).
          if (!issue) return setOpened(false);
          const tab = window.open(issue, "_blank");
          if (tab) tab.opener = null;
          setOpened(tab !== null);
        },
      },
    );
  };

  const applyError = (error: unknown): string => {
    if (error instanceof ApiError) {
      switch (error.code) {
        case "run_version_not_current":
          return t`This sheet type has a newer version than the one the check print was made for. Print a new check page and read it again.`;
        case "run_not_a_check_print":
          return t`These readings are not from a check print, so they cannot change the sheet type.`;
        case "run_not_for_this_type":
          return t`This check print was made for another sheet type.`;
        case "suggestion_unavailable":
          return unavailableText(typeof error.errorDetail?.reason === "string" ? error.errorDetail.reason : undefined);
        case "invalid_geometry":
          return t`The suggested values do not make a valid sheet: the labels would overlap or leave the page. Nothing was changed.`;
      }
    }
    return errorText(error, t`The new version could not be added.`);
  };

  const typeName = typeLabelWithKey(type);
  const next = type.current_version + 1;
  const current = type.current_version;
  const runNo = run?.run_no ?? 0;
  const added = apply.data?.version ?? next;
  const key = type.key;

  return (
    <section aria-labelledby="suggestion-h" className={`${PANEL} flex min-w-0 flex-col gap-4 border-link`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="suggestion-h" className={`${SECTION_HEADING} m-0`}>{t`Geometry suggestion`}</h3>
        <span className="rounded-full bg-info-soft px-2.5 py-0.5 text-small font-bold text-link">{t`From ${readings} check readings`}</span>
      </div>
      <p className="m-0 [overflow-wrap:anywhere]">
        {measuredOn ? (
          <Trans>
            These readings were printed with the correction measured on <strong>{measuredOn}</strong>, a different type. What is left looks like
            the sheet, not the printer.
          </Trans>
        ) : (
          t`These readings were printed with the correction of a different type. What is left looks like the sheet, not the printer.`
        )}
      </p>
      {proposed.length > 0 && (
        <dl className="m-0 grid grid-cols-2 gap-x-6 gap-y-3">
          {proposed.map((p) => (
            <div key={p.field} className="min-w-0">
              <dt className={HINT}>{names[p.field]}</dt>
              <dd className="m-0 font-bold tabular-nums">{p.now === null ? `${mm(p.mm)} mm` : `${mm(p.now)} → ${mm(p.mm)} mm`}</dd>
            </div>
          ))}
        </dl>
      )}
      {suggestion.uneven && <p className="m-0 font-bold text-warn">{t`Uneven: the readings follow no straight line. Check the sheet, not the printer.`}</p>}
      {proposed.length === 0 && !suggestion.uneven && <p className="m-0">{t`No change to pitch or margins.`}</p>}
      <div className="flex flex-wrap gap-3">
        {admin && proposed.length > 0 && run && (
          <Button onPress={applyNow} isDisabled={apply.isPending || apply.isSuccess} className={PRIMARY_BUTTON}>
            {t`Apply as new version`}
          </Button>
        )}
        {issue && (
          <a href={issue} target="_blank" rel="noopener noreferrer" className={`${SECONDARY_BUTTON} no-underline`}>
            {t`Fill the feedback issue`}
            <span className="sr-only"> {t`(opens in a new tab)`}</span>
          </a>
        )}
      </div>
      {admin && proposed.length > 0 && (
        <p className={`${HINT} m-0`}>
          {t`Adds version ${next} of ${typeName} with a change note naming print #${runNo}, and a log entry. Admins only. Sheets already made keep version ${current}.`}
        </p>
      )}
      {apply.isError && (
        <p role="alert" className="m-0 text-bad">
          {applyError(apply.error)}
        </p>
      )}
      {apply.isSuccess && opened !== null && (
        <p role="status" className="m-0 text-ok">
          {opened
            ? t`Version ${added} of ${key} is added. The feedback issue opened in a new tab.`
            : t`Version ${added} of ${key} is added. Open the feedback issue with the link above.`}
        </p>
      )}
    </section>
  );
}
