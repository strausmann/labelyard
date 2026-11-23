// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { useSheetsPage, type SheetSummary } from "../sheets/api";
import { SECONDARY_BUTTON, SECTION_HEADING } from "../ui";

type Group = "in_use" | "used_up" | "discarded";
const groupOf = (s: SheetSummary): Group => (s.state === "discarded" ? "discarded" : s.free === 0 ? "used_up" : "in_use");
const RANK: Record<Group, number> = { in_use: 0, used_up: 1, discarded: 2 };

/** In use first, then used up, then discarded; inside a group the most recently used first. */
export function orderSheets(rows: SheetSummary[]): SheetSummary[] {
  const time = (s: SheetSummary) => (s.last_used_at ? Date.parse(s.last_used_at) : 0);
  return [...rows].sort((a, b) => RANK[groupOf(a)] - RANK[groupOf(b)] || time(b) - time(a) || a.code.localeCompare(b.code));
}

function StatePill({ group }: { group: Group }) {
  const { t } = useLingui();
  const words: Record<Group, string> = {
    in_use: t({ message: "In use", context: "sheet state" }),
    used_up: t({ message: "Used up", context: "sheet state" }),
    discarded: t({ message: "Discarded", context: "sheet state" }),
  };
  const tone = group === "in_use" ? "border-ok" : group === "used_up" ? "border-line-strong" : "border-warn";
  return <span className={`inline-flex items-center rounded-full border px-2.5 text-small leading-6 font-bold text-ink ${tone}`}>{words[group]}</span>;
}

/**
 * The sheets of one type (#335, spec 6.1.6): in use first, then used up, discarded behind a toggle; each row links to
 * its sheet, and the whole list to the sheets page filtered by the type.
 */
export function TypeSheetsList({ typeKey }: { typeKey: string }) {
  const { t, i18n } = useLingui();
  const [showDiscarded, setShowDiscarded] = useState(false);
  const page = useSheetsPage({ type: typeKey, state: "all" });
  const rows = orderSheets(page.data?.rows ?? []);
  const discarded = rows.filter((s) => s.state === "discarded").length;
  const shown = showDiscarded ? rows : rows.filter((s) => s.state !== "discarded");
  const day = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString(i18n.locale) : "—");

  return (
    <section aria-labelledby="type-sheets-heading" className="flex min-w-0 flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 id="type-sheets-heading" className={SECTION_HEADING}>{t`Its sheets`}</h3>
        <Link to="/sheets" search={{ type: typeKey }} className={`${SECONDARY_BUTTON} gap-2 no-underline`}>
          <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" className="fill-none stroke-current stroke-[1.5]">
            <path d="M7 1.5h3.5V5M10.5 1.5 5.5 6.5M9 7.5v3H1.5V3h3" />
          </svg>
          {t`Show in the sheets list`}
        </Link>
      </div>
      {page.isPending ? <p className="text-ink-2">{t`Loading…`}</p> : null}
      {page.isError ? <p className="text-ink-2">{t`The sheets could not be loaded.`}</p> : null}
      {page.data && rows.length === 0 ? <p className="text-ink-2">{t`No sheet of this type yet.`}</p> : null}
      {shown.length > 0 ? (
        <ul aria-label={t`Its sheets`} className="m-0 flex list-none flex-col overflow-hidden rounded-r3 border border-line p-0">
          {shown.map((s) => (
            <li key={s.code} className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 border-t border-line bg-surface px-4 py-3 first:border-t-0">
              <div className="flex min-w-0 flex-col gap-0.5">
                <Link to="/sheets/$code" params={{ code: s.code }} className="inline-flex min-h-11 items-center self-start font-mono font-bold text-link underline underline-offset-2 sm:min-h-0">
                  {s.code}
                </Link>
                <span className="text-small text-ink-2 tabular-nums">{t`${s.used} of ${s.total} used · ${s.free} free`}</span>
              </div>
              <div className="flex flex-wrap items-center gap-3 text-small text-ink-2">
                <StatePill group={groupOf(s)} />
                <span>{t`last use ${day(s.last_used_at)}`}</span>
              </div>
            </li>
          ))}
        </ul>
      ) : null}
      {page.data?.more ? <p className="text-small text-ink-2">{t`Only the first 500 sheets are listed here; the sheets list shows them all.`}</p> : null}
      {discarded > 0 ? (
        <label className="flex min-h-11 items-center gap-3 self-start">
          <input type="checkbox" checked={showDiscarded} onChange={(e) => setShowDiscarded(e.target.checked)} className="size-5 accent-accent" />
          <span>{t`Show discarded (${discarded})`}</span>
        </label>
      ) : null}
    </section>
  );
}
