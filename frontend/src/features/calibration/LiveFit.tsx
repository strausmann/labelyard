// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { useEffect, useState } from "react";
import { apiFetch } from "../../api/client";
import { HINT } from "../settings/controls";
import { PANEL, SECTION_HEADING } from "../ui";
import type { FitRequest, FitResponse } from "./api";
import { useCalibrationErrorText, useRangeWords } from "./errors";
import { crossingName, MIN_READINGS } from "./model";

/** The pause after the last change before the preview asks the server (design 7.4). */
export const LIVE_FIT_DELAY_MS = 400;

/** A fit and the request it answers, so the table can name each crossing. */
export interface FitResult {
  request: FitRequest;
  response: FitResponse;
}

interface LiveState {
  result: FitResult | null;
  /** The request (as JSON) `result` answers. */
  resultKey: string | null;
  error: unknown;
  errorKey: string | null;
}

const trayPath = (printerId: string, tray: string) => `/api/v1/printers/${encodeURIComponent(printerId)}/calibration/${encodeURIComponent(tray)}`;

/**
 * The fit, run on every change once three readings are complete: 400 ms after the last change, and a
 * request still out is cancelled when the readings change again. Nothing is stored. An unchanged request
 * is not sent again. Below three readings there is no preview.
 */
export function useLiveFit(printerId: string, tray: string, request: FitRequest | null) {
  const key = request && request.readings.length >= MIN_READINGS ? JSON.stringify(request) : null;
  const [state, setState] = useState<LiveState>({ result: null, resultKey: null, error: null, errorKey: null });
  const answered = state.resultKey === key || state.errorKey === key;

  useEffect(() => {
    if (key === null || answered) return;
    const sent = JSON.parse(key) as FitRequest;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      apiFetch<FitResponse>(`${trayPath(printerId, tray)}/fit`, { method: "POST", json: sent, signal: controller.signal }).then(
        (response) => {
          if (!controller.signal.aborted) setState((s) => ({ ...s, result: { request: sent, response }, resultKey: key }));
        },
        (error: unknown) => {
          if (!controller.signal.aborted) setState((s) => ({ ...s, error, errorKey: key }));
        },
      );
    }, LIVE_FIT_DELAY_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
    // `answered` only stops a repeat of the same request; it is not a reason to send again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [printerId, tray, key]);

  return {
    /** The newest answer while three readings are complete, also while a newer one is on its way. */
    result: key === null ? null : state.result,
    error: key !== null && state.errorKey === key ? state.error : null,
    updating: key !== null && !answered,
  };
}

/** Residuals over this are a probable misread (`backend/app/calibration/fit.py`, FLAG_MM); the server flags them. */
export const MISREAD_MM = 0.3;

interface LiveFitPanelProps {
  result: FitResult | null;
  error: unknown;
  updating: boolean;
  /** Complete readings in the form now. */
  complete: number;
  /** The rows (1-based) of the complete readings, for an error that names one. */
  readingRows: number[];
}

/** The live fit (mockup M8): the five values, then the residual of each crossing with probable misreads marked. */
export function LiveFitPanel({ result, error, updating, complete, readingRows }: LiveFitPanelProps) {
  const { t, i18n } = useLingui();
  const errorText = useCalibrationErrorText();
  const rangeWords = useRangeWords();
  const fixed = (digits: number, sign = true) =>
    new Intl.NumberFormat(i18n.locale, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
      useGrouping: false,
      signDisplay: sign ? "exceptZero" : "auto",
    });
  const mm = (v: number) => `${fixed(2).format(v)} mm`;
  const percent = (factor: number) => `${fixed(3).format((factor - 1) * 100)} %`;
  const values = result?.response.values;
  const outOfRange = result?.response.out_of_range ?? [];
  const outOfRangeList = outOfRange.map((name) => rangeWords[name] ?? name).join(", ");
  const misreads = result?.response.flagged.length ?? 0;
  const across = values ? mm(values.offset_x_mm) : "";
  const down = values ? mm(values.offset_y_mm) : "";
  // What a screen reader hears after each recalculation: the shift and how many readings look misread.
  const summary = values
    ? `${t`Across ${across}, down ${down}.`} ${misreads === 0 ? t`No probable misreads.` : misreads === 1 ? t`1 probable misread.` : t`${misreads} probable misreads.`}`
    : "";

  return (
    <section aria-labelledby="live-fit-h" className={`${PANEL} flex min-w-0 flex-col gap-4`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="live-fit-h" className={`${SECTION_HEADING} m-0`}>{t`Live fit`}</h3>
        <span className="rounded-full border border-line-strong px-2.5 py-0.5 text-small font-bold text-ink-2">{t`Updates as you type`}</span>
      </div>
      <p className={`${HINT} m-0`}>
        {complete < MIN_READINGS
          ? t`Needs three readings. Recalculated 0.4 s after your last change.`
          : updating
            ? t`Recalculating…`
            : t`Recalculated 0.4 s after your last change.`}
      </p>
      {error !== null && (
        <p role="alert" className="m-0 text-bad">
          {errorText(error, t`The fit failed.`, readingRows)}
        </p>
      )}
      {values && (
        <dl className="m-0 grid grid-cols-2 gap-x-6 gap-y-3">
          <div className="min-w-0">
            <dt className={HINT}>{t`Across`}</dt>
            <dd className="m-0 font-bold tabular-nums">{mm(values.offset_x_mm)}</dd>
          </div>
          <div className="min-w-0">
            <dt className={HINT}>{t`Down`}</dt>
            <dd className="m-0 font-bold tabular-nums">{mm(values.offset_y_mm)}</dd>
          </div>
          <div className="min-w-0">
            <dt className={HINT}>{t`Scale`}</dt>
            <dd className="m-0 font-bold tabular-nums">
              {percent(values.scale_x)} / {percent(values.scale_y)}
            </dd>
          </div>
          <div className="min-w-0">
            <dt className={HINT}>{t`Rotation`}</dt>
            <dd className="m-0 font-bold tabular-nums">{`${fixed(3).format(values.rotation_deg)}°`}</dd>
          </div>
        </dl>
      )}
      {/* Always in the page and polite: it is read out when the list changes, not on every recalculation. */}
      <div aria-live="polite" className="empty:hidden">
        {outOfRange.length > 0 && (
          <p className="m-0 text-bad">
            {t`These fitted values are outside the allowed range: ${outOfRangeList}. More than that is a fault of the printer, not something to correct.`}
          </p>
        )}
      </div>
      <p role="status" aria-live="polite" aria-label={t`Live fit result`} className="sr-only">
        {summary}
      </p>
      {result && (
        // A box that scrolls sideways has to be reachable with the keyboard, and it needs a name (267-08).
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable region must take focus (axe: scrollable-region-focusable)
        <div role="region" aria-label={t`Result of the fit`} tabIndex={0} className="overflow-x-auto rounded-r2 border border-line focus-visible:outline-3 focus-visible:outline-link">
          <table aria-label={t`Result of the fit`} className="w-full border-collapse text-left text-small">
            <thead>
              <tr className="text-ink-2">
                <th scope="col" className="px-3 py-2 font-bold">{t`Crossing`}</th>
                <th scope="col" className="px-3 py-2 text-right font-bold">{t`Residual`}</th>
                <th scope="col" className="px-3 py-2">
                  <span className="sr-only">{t`Verdict`}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {result.request.readings.map((c, i) => {
                const residual = result.response.residuals_mm[i] ?? 0;
                const flagged = result.response.flagged.includes(i);
                return (
                  <tr key={i} className={`border-t border-line ${flagged ? "bg-accent-soft" : ""}`}>
                    <th scope="row" className="px-3 py-2 font-bold whitespace-nowrap tabular-nums">{crossingName(c.col_gap, c.row_gap)}</th>
                    <td className="px-3 py-2 text-right whitespace-nowrap tabular-nums">{`${fixed(2, false).format(residual)} mm`}</td>
                    <td className="px-3 py-2">
                      {flagged ? <Badge tone="warn">{t`probable misread`}</Badge> : <Badge tone="ok">{t`fits`}</Badge>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {result && <p className={`${HINT} m-0`}>{t`A residual over 0.3 mm is marked: read that crossing again before you save.`}</p>}
    </section>
  );
}

function Badge({ tone, children }: { tone: "ok" | "warn"; children: string }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-bold ${tone === "ok" ? "bg-sunken text-ok" : "text-warn"}`}>
      <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" className="fill-none stroke-current stroke-2">
        {tone === "ok" ? <path d="m5 12.5 4.5 4.5L19 7.5" /> : <path d="M12 4 21 19.5H3L12 4ZM12 10v4.5M12 17h.01" />}
      </svg>
      {children}
    </span>
  );
}
