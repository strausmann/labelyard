// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import { useId } from "react";
import type { components } from "../../api/schema.gen";
import { asymmetryHints, cellRect, formatMm, pageSizeMm, remainingMargins, type GeometryMm } from "./cellState";

type ZoneMm = components["schemas"]["PreprintZoneMm"];
/** A rectangle on the page in mm from its top left: the planned sheet code. */
export interface RectMm {
  x_mm: number;
  y_mm: number;
  w_mm: number;
  h_mm: number;
}

/**
 * Read-only reference of a sheet type's measurements: a drawing of the top-left corner plus every
 * number in words (the drawing is for the eye, the list is the content). Margins right and bottom are
 * computed from the page size. The asymmetry hint is the server's (`SheetTypeRead.hints`) when given.
 */
export function GeometryDiagram({ geometry: g, hints }: { geometry: GeometryMm; hints?: string[] }) {
  const { t, i18n } = useLingui();
  const mm = (v: number) => formatMm(v, i18n.locale);
  const page = pageSizeMm(g);
  const { right, bottom } = remainingMargins(g);
  const active = hints ?? asymmetryHints(g);
  const round = g.shape === "round";
  const gapX = g.pitch_x - g.label_w;
  const gapY = g.pitch_y - g.label_h;

  const radius = round ? t`round` : g.corner_radius == null ? t`unknown` : mm(g.corner_radius);
  const rows: [string, string][] = [
    [t`Page`, `${g.page_size} · ${mm(page.w)} × ${mm(page.h)}`],
    [t`Label size`, `${mm(g.label_w)} × ${mm(g.label_h)}`],
    [
      t`Grid`,
      `${plural(g.cols, { one: "# column", other: "# columns" })} × ${plural(g.rows, { one: "# row", other: "# rows" })}`,
    ],
    [t`Pitch`, `${mm(g.pitch_x)} × ${mm(g.pitch_y)}`],
    [t`Gap between columns`, mm(gapX)],
    [t`Gap between rows`, mm(gapY)],
    [t`Top margin`, mm(g.margin_top)],
    [t`Left margin`, mm(g.margin_left)],
    [t`Right margin`, mm(right)],
    [t`Bottom margin`, mm(bottom)],
    [t`Corner radius`, radius],
  ];

  // Drawing: two columns by two rows from the page corner, in mm.
  const cols = Math.min(2, g.cols);
  const rws = Math.min(2, g.rows);
  const vbW = g.margin_left + (cols - 1) * g.pitch_x + g.label_w + Math.max(g.margin_left, 4) * 0.6;
  const vbH = g.margin_top + (rws - 1) * g.pitch_y + g.label_h + Math.max(g.margin_top, 4) * 0.5;
  const labels = Array.from({ length: cols * rws }, (_, i) => ({
    x: g.margin_left + (i % cols) * g.pitch_x,
    y: g.margin_top + Math.floor(i / cols) * g.pitch_y,
  }));
  const dim = "stroke-link";
  const fs = Math.max(vbW, vbH) / 24;

  return (
    <div className="flex flex-col gap-4 sm:flex-row sm:items-start">
      <svg viewBox={`-1 -1 ${vbW + 2} ${vbH + 2}`} aria-hidden="true" className="block w-full max-w-72 shrink-0 self-center sm:self-start">
        <rect width={vbW} height={vbH} className="fill-surface stroke-line-strong" strokeWidth={vbW / 300} />
        {labels.map((l, i) =>
          round ? (
            <circle key={i} cx={l.x + g.label_w / 2} cy={l.y + g.label_h / 2} r={g.label_w / 2} className="fill-accent-soft stroke-accent" strokeWidth={vbW / 300} />
          ) : (
            <rect key={i} x={l.x} y={l.y} width={g.label_w} height={g.label_h} rx={g.corner_radius ?? 1} className="fill-accent-soft stroke-accent" strokeWidth={vbW / 300} />
          ),
        )}
        {/* label width and height on the first label, left and top margin to the page edge */}
        <line x1={g.margin_left} x2={g.margin_left + g.label_w} y1={g.margin_top - fs * 0.7} y2={g.margin_top - fs * 0.7} className={dim} strokeWidth={vbW / 300} />
        <line x1={g.margin_left - fs * 0.7} x2={g.margin_left - fs * 0.7} y1={g.margin_top} y2={g.margin_top + g.label_h} className={dim} strokeWidth={vbW / 300} />
        <line x1={0} x2={g.margin_left} y1={g.margin_top + g.label_h / 2} y2={g.margin_top + g.label_h / 2} className={dim} strokeWidth={vbW / 300} strokeDasharray={`${fs / 3} ${fs / 3}`} />
        <line x1={g.margin_left + g.label_w - fs} x2={g.margin_left + g.label_w - fs} y1={0} y2={g.margin_top} className={dim} strokeWidth={vbW / 300} strokeDasharray={`${fs / 3} ${fs / 3}`} />
        <text x={g.margin_left + g.label_w / 2} y={g.margin_top - fs * 1.1} fontSize={fs} textAnchor="middle" className="fill-ink-2 stroke-surface" strokeWidth={fs * 0.35} paintOrder="stroke">{mm(g.label_w)}</text>
      </svg>
      <div className="flex min-w-0 flex-1 flex-col gap-3">
        <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-small">
          {rows.map(([name, value]) => (
            <div key={name} className="contents">
              <dt className="text-ink-2">{name}</dt>
              <dd className="m-0 font-bold tabular-nums">{value}</dd>
            </div>
          ))}
        </dl>
        {active.length > 0 && (
          <div role="note" className="flex flex-col gap-1 rounded-r2 border border-warn bg-accent-soft p-3 text-small">
            {active.includes("margins_asymmetric_x") && (
              <p className="m-0">{t`The left and right margins differ (${mm(g.margin_left)} and ${mm(right)}). Measure the sheet again if the labels print off-centre.`}</p>
            )}
            {active.includes("margins_asymmetric_y") && (
              <p className="m-0">{t`The top and bottom margins differ (${mm(g.margin_top)} and ${mm(bottom)}). Measure the sheet again if the labels print off-centre.`}</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * The whole front of a sheet (#246, mockups M1/M2): the labels, the preprinted areas hatched, a refused area dashed
 * and the planned sheet code outlined. `view` crops it to a part of the page in mm (the enlarged band).
 */
export function SheetFrontDiagram({
  geometry: g,
  zones,
  refused = [],
  idRect,
  label,
  view,
  className = "",
}: {
  geometry: GeometryMm;
  zones: readonly ZoneMm[];
  /** Indexes into `zones` the server refused. */
  refused?: readonly number[];
  idRect?: RectMm | null;
  label: string;
  view?: [number, number, number, number];
  className?: string;
}) {
  const page = pageSizeMm(g);
  const box = view ?? [0, 0, page.w, page.h];
  const hatch = `${useId().replace(/[^a-zA-Z0-9]/g, "")}-zone`;
  const line = Math.max(box[2], box[3]) / 400;
  const radius = g.corner_radius ?? 1;
  const round = g.shape === "round";
  const cells = Array.from({ length: g.cols * g.rows }, (_, i) => cellRect(g, i + 1));
  return (
    <svg role="img" aria-label={label} viewBox={box.join(" ")} preserveAspectRatio="xMidYMid meet" className={`block w-full ${className}`}>
      <defs>
        <pattern id={hatch} patternUnits="userSpaceOnUse" width="2.2" height="2.2" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="2.2" className="stroke-ink-3" strokeWidth="0.45" />
        </pattern>
      </defs>
      <rect width={page.w} height={page.h} className="fill-surface stroke-line-strong" strokeWidth={line * 2} />
      {cells.map((c, i) =>
        round ? (
          <circle key={i} data-cell-shape="" cx={c.x + c.w / 2} cy={c.y + c.h / 2} r={c.w / 2} className="fill-accent-soft stroke-accent" strokeWidth={line} />
        ) : (
          <rect key={i} data-cell-shape="" x={c.x} y={c.y} width={c.w} height={c.h} rx={radius} className="fill-accent-soft stroke-accent" strokeWidth={line} />
        ),
      )}
      {zones.map((z, i) =>
        refused.includes(i) ? (
          <rect key={i} data-zone="refused" x={z.x_mm} y={z.y_mm} width={z.w_mm} height={z.h_mm} fill={`url(#${hatch})`} className="stroke-bad" strokeWidth={line * 2.5} strokeDasharray={`${line * 8} ${line * 5}`} />
        ) : (
          <rect key={i} data-zone="kept" x={z.x_mm} y={z.y_mm} width={z.w_mm} height={z.h_mm} fill={`url(#${hatch})`} className="stroke-ink-3" strokeWidth={line} />
        ),
      )}
      {idRect ? (
        <rect data-id-rect="" x={idRect.x_mm} y={idRect.y_mm} width={idRect.w_mm} height={idRect.h_mm} className="fill-info-soft stroke-link" strokeWidth={line * 3} />
      ) : null}
    </svg>
  );
}

/** The key to the front diagram: shapes, not colour alone. */
export function SheetFrontLegend() {
  const { t } = useLingui();
  const swatch = "block h-3.5 w-5.5 shrink-0";
  return (
    <ul aria-label={t`Legend`} className="m-0 flex list-none flex-wrap gap-x-4 gap-y-2 p-0 text-small text-ink-2">
      <li className="flex items-center gap-2">
        <svg viewBox="0 0 22 14" aria-hidden="true" className={swatch}>
          <rect x="1" y="1" width="20" height="12" className="fill-none stroke-ink-3" />
          <path d="M3 12 12 2M8 12l9-10M13 12l6-8" className="fill-none stroke-ink-3" />
        </svg>
        {t`Preprinted area`}
      </li>
      <li className="flex items-center gap-2">
        <svg viewBox="0 0 22 14" aria-hidden="true" className={swatch}>
          <rect x="1" y="1" width="20" height="12" className="fill-info-soft stroke-link" strokeWidth="2" />
        </svg>
        {t`Sheet code`}
      </li>
      <li className="flex items-center gap-2">
        <svg viewBox="0 0 22 14" aria-hidden="true" className={swatch}>
          <rect x="1" y="1" width="20" height="12" className="fill-none stroke-bad" strokeDasharray="3 2" />
        </svg>
        {t`Refused or does not fit`}
      </li>
      <li className="flex items-center gap-2">
        <svg viewBox="0 0 22 14" aria-hidden="true" className={swatch}>
          <rect x="1" y="1" width="20" height="12" className="fill-accent-soft stroke-accent" />
        </svg>
        {t`Label`}
      </li>
    </ul>
  );
}

