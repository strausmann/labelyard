// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { pageSizeMm, type GeometryMm } from "../sheets/cellState";
import { SheetFrontDiagram, SheetFrontLegend, type RectMm } from "../sheets/GeometryDiagram";
import { PANEL } from "../ui";
import { useIdPlacement, type IdPlacementRow } from "./api";
import type { Zone } from "./preprint";

/** The rectangle of a row that fits, or null. */
export function rectOf(row: IdPlacementRow | undefined): RectMm | null {
  if (!row || !row.fits || row.x_mm == null || row.y_mm == null || row.w_mm == null || row.h_mm == null) return null;
  return { x_mm: row.x_mm, y_mm: row.y_mm, w_mm: row.w_mm, h_mm: row.h_mm };
}

/** The top band and a bit of the first row, in mm: what the enlarged halves show. */
export function topBandView(g: GeometryMm): number {
  return Math.min(pageSizeMm(g).h, Math.max(g.margin_top + Math.min(g.label_h, 12), 20));
}

/**
 * The sheet front beside Edit details (mockup M1): the labels, the areas being edited (the refused one dashed) and the
 * sheet code where the saved rules put it on the first page printer, plus the top band enlarged in two halves.
 */
export function SheetFrontPanel({
  geometry: g,
  typeKey,
  zones,
  refused,
}: {
  geometry: GeometryMm;
  typeKey: string;
  zones: readonly Zone[];
  refused: readonly number[];
}) {
  const { t } = useLingui();
  const rows = useIdPlacement(typeKey);
  const idRect = rectOf(rows.data?.find((r) => r.fits));
  const page = pageSizeMm(g);
  const depth = topBandView(g);
  const half = page.w / 2;
  return (
    <aside aria-labelledby="front-h" className={`${PANEL} flex min-w-0 flex-col gap-4 xl:sticky xl:top-20`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="front-h" className="text-section font-bold">{t`Sheet front`}</h3>
        <span className="rounded-full border border-line-strong px-2 py-0.5 text-small font-bold text-ink-2">{t`Preview of the saved rules`}</span>
      </div>
      <div className="mx-auto w-full max-w-80">
        <SheetFrontDiagram
          geometry={g}
          zones={zones}
          refused={refused}
          idRect={idRect}
          label={t`Sheet front with the preprinted areas hatched and the planned sheet code outlined`}
        />
      </div>
      <div className="flex flex-col gap-2">
        <p className="m-0 text-small font-bold">{t`Top band, enlarged in two halves`}</p>
        <SheetFrontDiagram geometry={g} zones={zones} refused={refused} idRect={idRect} view={[0, 0, half, depth]} label={t`Left half of the top band, enlarged`} />
        <SheetFrontDiagram geometry={g} zones={zones} refused={refused} idRect={idRect} view={[half, 0, half, depth]} label={t`Right half of the top band, enlarged`} />
        <p className="m-0 text-small text-ink-2">
          {idRect
            ? t`The sheet code is drawn where the saved rules put it on the first page printer. Saving moves it.`
            : t`No page printer has room for the sheet code in the margin by the saved rules.`}
        </p>
      </div>
      <SheetFrontLegend />
    </aside>
  );
}
