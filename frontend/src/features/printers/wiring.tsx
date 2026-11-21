// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { Link } from "@tanstack/react-router";
import { useSession } from "../../app/session";
import { formatTime } from "../jobs/model";
import { PANEL, SECTION_HEADING } from "../ui";
import type { Printer } from "./api";

/** A printer the hub could not wire (#401): the reason is a fixed code from the hub, never an error text. */
export const isNotWired = (printer: Printer): boolean => printer.wiring === "not_wired";

const upFirst = (text: string, locale: string) => text.charAt(0).toLocaleUpperCase(locale) + text.slice(1);

/**
 * The reason code in words, as a clause that follows "Not wired:". A code this version does not know (a newer hub) and
 * a missing one read as the general sentence. One more reason is one more line here.
 */
export function useWiringWords(): (reason: string | null | undefined) => string {
  const { t } = useLingui();
  return (reason) => {
    switch (reason) {
      case "model_unknown":
        return t`the hub does not know this printer model`;
      case "discovery_failed":
        return t`the printer did not answer when the hub looked for it`;
      case "invalid_config":
        return t`the saved settings are not valid`;
      case "unexpected":
        return t`something unexpected stopped the connection`;
      default:
        return t`the hub is not connected to this printer`;
    }
  };
}

/** What to do about it, per reason; the general advice is to check the address and save again (saving re-wires). */
export function useWiringAdvice(): (reason: string | null | undefined) => string {
  const { t } = useLingui();
  return (reason) => {
    switch (reason) {
      case "invalid_config":
        return t`Open the printer settings, correct the address, then save the printer again.`;
      case "unexpected":
        return t`Check Jobs and the hub log, then save the printer again.`;
      default:
        return t`Check the address, then save the printer again.`;
    }
  };
}

/** The two lines on a printer card: why it is not wired and what to do. Nothing for a wired printer. */
export function WiringLines({ printer }: { printer: Printer }) {
  const { t } = useLingui();
  const words = useWiringWords();
  const advice = useWiringAdvice();
  if (!isNotWired(printer)) return null;
  const reason = words(printer.wiring_reason);
  return (
    <span className="flex flex-col gap-1 text-small [overflow-wrap:anywhere]">
      <span className="text-bad">{t`Not wired: ${reason}.`}</span>
      <span className="text-ink-2">{advice(printer.wiring_reason)}</span>
    </span>
  );
}

/** The detail page: the headline note and the "What the hub saw" panel. */
export function WiringSections({ printer }: { printer: Printer }) {
  const { t, i18n } = useLingui();
  const words = useWiringWords();
  const advice = useWiringAdvice();
  const admin = useSession().scope === "admin";
  if (!isNotWired(printer)) return null;
  const reason = words(printer.wiring_reason);
  const code = printer.wiring_reason ?? null;
  const at = printer.wiring_at ? formatTime(printer.wiring_at) : "";
  return (
    <>
      <div role="note" className="flex min-w-0 flex-col gap-1 rounded-r2 border border-bad bg-sunken p-3 text-small">
        <strong className="[overflow-wrap:anywhere]">{t`Not wired: ${reason}`}</strong>
        <span className="[overflow-wrap:anywhere]">
          {t`The hub started and carried on without it, so nothing else is held up. Jobs for this printer are refused until it is wired.`}{" "}
          {advice(code)} {t`Saving tries the connection again, you do not need to restart the hub.`}
        </span>
        {admin && (
          <Link to="/settings/printers" className="inline-flex min-h-11 w-fit items-center font-bold text-link underline underline-offset-2">
            {t`Open the printer settings`}
          </Link>
        )}
      </div>
      <section aria-labelledby="wiring-h" className={PANEL}>
        <h2 id="wiring-h" className={SECTION_HEADING}>{t`What the hub saw`}</h2>
        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-small">
          <dt className="text-ink-2">{t`State`}</dt>
          <dd className="font-bold">{t`Not wired`}</dd>
          <dt className="text-ink-2">{t`Reason`}</dt>
          <dd className="min-w-0 break-words">
            {upFirst(reason, i18n.locale)}
            {code && <span className="code ml-2 text-ink-3">{code}</span>}
          </dd>
          {at && (
            <>
              <dt className="text-ink-2">{t`Last attempt`}</dt>
              <dd>{at}</dd>
            </>
          )}
          <dt className="text-ink-2">{t`After a restart`}</dt>
          <dd>{t`The hub tries again by itself.`}</dd>
        </dl>
      </section>
    </>
  );
}
