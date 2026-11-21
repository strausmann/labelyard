// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import { usePrinters } from "./api";
import { PrinterGrid } from "./PrinterGrid";
import { isNotWired } from "./wiring";

export function PrintersPage() {
  const { t } = useLingui();
  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-title font-bold">{t`Printers`}</h1>
      <NotWiredSummary />
      <PrinterGrid />
    </div>
  );
}

/** One note above the cards when the hub could not wire a printer (#401): which ones, and where to see why. */
function NotWiredSummary() {
  const { t } = useLingui();
  const printers = usePrinters();
  const names = (printers.data ?? []).filter(isNotWired).map((p) => p.name);
  if (names.length === 0) return null;
  const list = names.join(", ");
  return (
    <div role="note" className="flex min-w-0 flex-col gap-1 rounded-r2 border border-bad bg-sunken p-3 text-small">
      <strong>{plural(names.length, { one: "# printer is not wired", other: "# printers are not wired" })}</strong>
      <span className="[overflow-wrap:anywhere]">{t`${list}: open a printer to see why and try again.`}</span>
    </div>
  );
}
