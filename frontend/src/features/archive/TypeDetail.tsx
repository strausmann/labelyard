// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { Button, Tab, TabList, TabPanel, Tabs } from "react-aria-components";
import { ApiError } from "../../api/client";
import { PRIMARY_BUTTON } from "../../shell/SignIn";
import { GeometryDiagram } from "../sheets/GeometryDiagram";
import { formatMm } from "../sheets/cellState";
import { typeLabel } from "../sheets/typeLabel";
import { ModalDialog } from "../settings/controls";
import { SECTION_HEADING, SECONDARY_BUTTON } from "../ui";
import {
  exportTypeUrl,
  useArchiveType,
  useCatalogEntry,
  useCatalogUpdate,
  useDeleteType,
  useSheetType,
  type SheetTypeDetail,
} from "./api";
import { useArchiveErrorText } from "./errors";
import { Badge } from "./fields";
import { FeedbackLinks } from "./FeedbackLinks";
import { IdPlacementSection } from "./IdPlacementSection";
import { ProbePrintDialog } from "./ProbePrintDialog";
import { TypeForm } from "./TypeForm";
import { httpUrl, linkHost } from "./search";
import { TypeSheetsList } from "./TypeSheetsList";
import { TypeVersions } from "./TypeVersions";
import { useGeometryRows } from "./geometryRows";


/** Numbers in the usage of a type: what is left to print on, and how much it is used (#335, spec 6.1.5). */
function UsageTiles({ type }: { type: SheetTypeDetail }) {
  const { t, i18n } = useLingui();
  const last = type.last_used_at ? new Date(type.last_used_at).toLocaleDateString(i18n.locale) : null;
  const tile = (value: number, label: string) => (
    <div className="flex min-w-0 flex-col gap-1 rounded-r3 bg-sunken p-4">
      <span className="text-title font-bold tabular-nums">{value}</span>
      <span className="text-small text-ink-2">{label}</span>
    </div>
  );
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      {tile(type.sheets_with_free_cells, t`Sheets with free cells`)}
      {tile(type.free_labels, t`Free labels`)}
      {tile(type.sheets_total, last ? t`Sheets in all, last used ${last}` : t`Sheets in all, never used`)}
    </div>
  );
}

/**
 * An unedited catalog type with a newer catalog revision: the change, old → new, and the button that applies it
 * (#335, spec 6.1.3). An edited type only hears that the catalog is newer.
 */
function CatalogUpdatePanel({ type, admin }: { type: SheetTypeDetail; admin: boolean }) {
  const { t, i18n } = useLingui();
  const errorText = useArchiveErrorText();
  const rowsOf = useGeometryRows();
  const entry = useCatalogEntry(type.update_available ? (type.catalog_key ?? type.key) : null);
  const update = useCatalogUpdate();
  const catalogKey = type.catalog_key ?? type.key;

  if (type.catalog_missing) {
    return <p className="rounded-r2 bg-info-soft p-3 text-small">{t`This type is no longer in the catalog. It keeps working as it is.`}</p>;
  }
  if (type.catalog_newer && !type.update_available) {
    return (
      <p className="rounded-r2 bg-info-soft p-3 text-small">
        {t`The catalog has a newer version of this type. It was changed here, so the update is not offered: export it to compare, or delete it and add it again.`}
      </p>
    );
  }
  if (update.isSuccess) {
    const also = update.data.also_updated;
    return (
      <div role="status" className="flex flex-col gap-1 rounded-r2 bg-info-soft p-3 text-small">
        <span className="font-bold">{t`Updated to the catalog version.`}</span>
        {also.length > 0 ? <span>{t`Also updated: ${also.join(", ")}`}</span> : null}
      </div>
    );
  }
  if (!type.update_available) return null;

  const installed = type.catalog_revision ?? 1;
  const show = (value: unknown, mm: boolean) => (value == null ? "—" : mm && typeof value === "number" ? formatMm(value, i18n.locale) : String(value));
  const diff = entry.data ? rowsOf.filter((r) => (type.geometry[r.field] ?? null) !== (entry.data.geometry[r.field] ?? null)) : [];
  const revision = entry.data?.revision;

  return (
    <section aria-labelledby="catalog-update-heading" className="flex min-w-0 flex-col gap-4 rounded-r3 border border-link bg-surface p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="catalog-update-heading" className={SECTION_HEADING}>{t`Update available`}</h3>
        {revision ? <span className="inline-flex items-center rounded-full bg-info-soft px-2.5 text-small leading-6 font-bold">{t`Catalog version ${revision}`}</span> : null}
      </div>
      {entry.isPending ? <p className="text-ink-2">{t`Loading…`}</p> : null}
      {entry.isError ? <p role="alert">{t`The catalog entry could not be loaded.`}</p> : null}
      {entry.data ? (
        <>
          <p className="m-0">
            {diff.length > 0
              ? t`You installed catalog version ${installed} and have not changed this type. Version ${revision} changes these measurements. Sheets you already made keep the version they were created with.`
              : t`You installed catalog version ${installed} and have not changed this type. Version ${revision} keeps the measurements and changes the description or the source.`}
          </p>
          {diff.length > 0 ? (
            <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
              <table aria-label={t`What the update changes`} className="w-full border-collapse overflow-hidden rounded-r2 text-left">
                <thead>
                  <tr className="text-small">
                    <th scope="col" className="py-2 pr-3 font-bold">{t`Measurement`}</th>
                    <th scope="col" className="py-2 pr-3 text-right font-bold">{t`Installed (v${installed})`}</th>
                    <th scope="col" className="py-2 text-right font-bold">{t`Catalog (v${revision})`}</th>
                  </tr>
                </thead>
                <tbody>
                  {diff.map((r) => (
                    <tr key={r.field} className="border-t border-line">
                      <th scope="row" className="py-2 pr-3 font-normal">{r.label}</th>
                      <td className="py-2 pr-3 text-right font-mono tabular-nums">{show(type.geometry[r.field], r.mm)}</td>
                      <td className="py-2 text-right font-mono tabular-nums">{show(entry.data.geometry[r.field], r.mm)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          <div className="flex flex-wrap gap-3">
            {admin ? (
              <Button
                isDisabled={update.isPending}
                onPress={() => update.mutate(type.key)}
                className={PRIMARY_BUTTON}
              >
                {t`Update to catalog version ${revision}`}
              </Button>
            ) : null}
            <Link to="/sheets/archive" search={{ tab: "catalog", q: catalogKey }} className={`${SECONDARY_BUTTON} no-underline`}>
              {t`Show in the catalog`}
            </Link>
          </div>
          {update.error ? <p role="alert" className="m-0">{errorText(update.error)}</p> : null}
          <p className="m-0 text-small text-ink-2">
            {t`A type you changed yourself shows "The catalog has a newer version" without this button. Export it, compare, or delete it and add it again.`}
          </p>
        </>
      ) : null}
    </section>
  );
}

const TAB =
  "-mb-px inline-flex min-h-11 cursor-pointer items-center whitespace-nowrap border-b-2 border-transparent px-3 font-bold text-ink-2 transition-colors duration-150 data-[selected]:border-accent data-[selected]:text-ink data-[focus-visible]:outline-3 data-[focus-visible]:-outline-offset-3 data-[focus-visible]:outline-link [@media(hover:hover)]:hover:text-ink";

/** The words that mark a type: where it came from, whether anyone measured it, whether it is retired. */
export function TypeBadges({ type }: { type: { origin: "seed" | "custom" | "imported"; verified: boolean; archived_at: string | null } }) {
  const { t } = useLingui();
  const origin = { seed: t`seed`, custom: t`custom`, imported: t`imported` }[type.origin];
  return (
    <span className="flex flex-wrap gap-1.5">
      <Badge>{origin}</Badge>
      {type.verified ? null : <Badge tone="warn">{t`unverified`}</Badge>}
      {type.archived_at ? <Badge tone="warn">{t`archived`}</Badge> : null}
    </span>
  );
}

/** A few codes inline; a long list (the server allows 50) folds away behind its count. */
function ProductCodes({ codes }: { codes: string[] }) {
  const { t } = useLingui();
  if (codes.length === 0) return null;
  const list = <span className="font-mono text-small [overflow-wrap:anywhere]">{codes.join(", ")}</span>;
  if (codes.length <= 5) return <p className="text-small text-ink-2">{t`Product codes`}: {list}</p>;
  return (
    <details className="text-small text-ink-2">
      <summary className="min-h-11 cursor-pointer py-2.5">{plural(codes.length, { one: "# product code", other: "# product codes" })}</summary>
      {list}
    </details>
  );
}

function Source({ type }: { type: SheetTypeDetail }) {
  const url = httpUrl(type.source_url);
  const host = linkHost(type.source_url);
  const name = type.source_name;
  if (!name && !type.source_url) return null;
  return (
    <p>
      {url ? (
        <>
          <a href={url} target="_blank" rel="noopener noreferrer" className="text-link underline underline-offset-2 [overflow-wrap:anywhere]">
            {name ?? url}
          </a>
          {host ? <span className="ml-2 font-mono text-small text-ink-2 [overflow-wrap:anywhere]">{host}</span> : null}
        </>
      ) : (
        <>
          {name ? <span>{name}</span> : null}
          {name && type.source_url ? " " : null}
          {type.source_url ? <span className="font-mono text-small text-ink-2 [overflow-wrap:anywhere]">{type.source_url}</span> : null}
        </>
      )}
    </p>
  );
}

/** One type in full: measurements, versions, provenance and (for admins) the actions. */
export function TypeDetail({ typeKey, admin, onLeave }: { typeKey: string; admin: boolean; onLeave: () => void }) {
  const { t, i18n } = useLingui();
  const errorText = useArchiveErrorText();
  const query = useSheetType(typeKey);
  const archive = useArchiveType();
  const remove = useDeleteType();
  const [editing, setEditing] = useState<"geometry" | "details" | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  // The probe print (#241): null while closed, else whether it opens as the measurement sheet.
  const [probe, setProbe] = useState<{ grid: boolean } | null>(null);

  if (query.isPending) return <p className="text-ink-2">{t`Loading…`}</p>;
  if (query.isError) {
    const gone = query.error instanceof ApiError && query.error.code === "sheet_type_not_found";
    return (
      <p role="alert" className="text-ink">
        {gone ? t`That sheet type does not exist.` : t`The sheet type could not be loaded.`}
      </p>
    );
  }
  const type = query.data;
  const archived = type.archived_at !== null;
  // Any type without sheets can go (#335); with sheets the server answers has_sheets, so the button says why it waits.
  const hasSheets = type.sheets_total > 0;
  const title = typeLabel(type);
  const actionError = archive.error ?? remove.error;
  const g = type.geometry;
  const mm = (v: number) => formatMm(v, i18n.locale);

  return (
    <article aria-label={type.key} className="flex min-w-0 flex-col gap-6">
      <Link to="/sheets/archive" search={(prev) => ({ ...prev, type: undefined })} className="inline-flex min-h-11 items-center text-link underline underline-offset-2 lg:hidden">
        {t`Back to the list`}
      </Link>
      <header className="flex flex-col gap-2">
        <h2 className="text-title font-bold [overflow-wrap:anywhere]">{title}</h2>
        {title !== type.key ? <p className="m-0 font-mono text-ink-2 [overflow-wrap:anywhere]">{type.key}</p> : null}
        <TypeBadges type={type} />
        <p className="text-ink-2 [overflow-wrap:anywhere]">
          {[type.vendor, type.description].filter(Boolean).join(" · ") || null}
        </p>
        <p className="text-small text-ink-2">
          {mm(g.label_w)} × {mm(g.label_h)} · {plural(g.cols * g.rows, { one: "# label", other: "# labels" })} ·{" "}
          {t`Code prefix`} <span className="font-mono">{type.prefix}</span>
        </p>
        <ProductCodes codes={type.product_codes} />
      </header>

      {editing ? (
        <TypeForm mode={{ kind: editing, type }} onDone={() => setEditing(null)} />
      ) : (
        <>
          <div className="flex min-w-0 flex-col gap-3">
            <div className="flex flex-wrap gap-3">
              {admin ? (
                <>
                  <Button onPress={() => setEditing("geometry")} className={PRIMARY_BUTTON}>{t`Edit geometry`}</Button>
                  <Button onPress={() => setEditing("details")} className={SECONDARY_BUTTON}>{t`Edit details`}</Button>
                </>
              ) : null}
              <Button onPress={() => setProbe({ grid: false })} className={SECONDARY_BUTTON}>{t`Probe print on plain paper`}</Button>
              <Button onPress={() => setProbe({ grid: true })} className={SECONDARY_BUTTON}>{t`Measurement sheet`}</Button>
              <a href={exportTypeUrl(type.key)} download className={`${SECONDARY_BUTTON} no-underline`}>
                {t`Export`}
              </a>
              {admin ? (
                <Button
                  isDisabled={archive.isPending}
                  onPress={() => archive.mutate({ key: type.key, unarchive: archived })}
                  className={SECONDARY_BUTTON}
                >
                  {archived ? t`Unarchive` : t`Archive`}
                </Button>
              ) : null}
              {admin ? (
                <Button isDisabled={hasSheets} onPress={() => setConfirmDelete(true)} className={`${SECONDARY_BUTTON} text-bad`}>
                  {t`Delete type`}
                </Button>
              ) : null}
            </div>
            {admin && hasSheets ? (
              <p className="m-0 text-small text-ink-2">
                {plural(type.sheets_total, {
                  one: "Delete is only possible while no sheet uses the type. This type has # sheet: archive it instead, or discard and purge it first.",
                  other: "Delete is only possible while no sheet uses the type. This type has # sheets: archive it instead, or discard and purge them first.",
                })}
              </p>
            ) : null}
            {actionError ? <p role="alert">{errorText(actionError)}</p> : null}
          </div>
        <Tabs className="flex min-w-0 flex-col gap-6">
          {/* The tabs of mockup M2 (#246). They wrap on a narrow screen instead of scrolling. A panel without a control is a
              tab stop (WAI-ARIA tabs) and shows the ring; no `outline-none` next to the ring, it would win and hide it. */}
          <TabList aria-label={t`Sheet type`} className="flex flex-wrap gap-1 border-b border-line">
            <Tab id="measurements" className={TAB}>{t`Measurements`}</Tab>
            <Tab id="sheet-code" className={TAB}>{t`Sheet code`}</Tab>
            <Tab id="versions" className={TAB}>{t`Versions`}</Tab>
            <Tab id="sheets" className={TAB}>
              {t`Sheets`}
              <span className="ml-1.5 text-small font-normal text-ink-2 tabular-nums">{type.sheets_total}</span>
            </Tab>
          </TabList>
          <TabPanel id="measurements" className="flex min-w-0 flex-col gap-6 rounded-r2 data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link">
            <CatalogUpdatePanel type={type} admin={admin} />
            <section aria-labelledby="geometry-heading" className="flex flex-col gap-3">
              <h3 id="geometry-heading" className={SECTION_HEADING}>{t`Measurements`}</h3>
              <GeometryDiagram geometry={g} hints={type.hints} />
              <p className="text-ink-2">{type.allow_qr ? t`QR codes on labels: allowed` : t`QR codes on labels: not allowed`}</p>
            </section>
            {type.source_name || type.source_url || type.notes ? (
              <section aria-labelledby="source-heading" className="flex flex-col gap-3">
                <h3 id="source-heading" className={SECTION_HEADING}>{t`Where the numbers come from`}</h3>
                <Source type={type} />
                {type.notes ? <p className="whitespace-pre-wrap text-ink-2 [overflow-wrap:anywhere]">{type.notes}</p> : null}
              </section>
            ) : null}
            <FeedbackLinks type={type} admin={admin} />
          </TabPanel>
          <TabPanel id="sheet-code" className="rounded-r2 data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link">
            <IdPlacementSection key={type.key} type={type} admin={admin} />
          </TabPanel>
          <TabPanel id="versions" className="flex min-w-0 flex-col gap-6 rounded-r2 data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link">
            <section aria-labelledby="versions-heading" className="flex flex-col gap-3">
              <h3 id="versions-heading" className={SECTION_HEADING}>{t`Versions`}</h3>
              <TypeVersions type={type} admin={admin} />
            </section>
          </TabPanel>
          <TabPanel id="sheets" className="flex min-w-0 flex-col gap-6 rounded-r2 data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link">
            <UsageTiles type={type} />
            <TypeSheetsList typeKey={type.key} />
          </TabPanel>
        </Tabs>
        </>
      )}

      <ProbePrintDialog
        type={{ key: type.key, version: type.current_version }}
        grid={probe?.grid ?? false}
        isOpen={probe !== null}
        onClose={() => setProbe(null)}
      />
      <ModalDialog title={t`Delete this type?`} isOpen={confirmDelete} onClose={() => setConfirmDelete(false)} alert>
        <p>{t`${type.key} has no sheets. Deleting it removes it and all its versions for good.`}</p>
        <div className="flex flex-wrap gap-3">
          <Button
            isDisabled={remove.isPending}
            onPress={() =>
              remove.mutate(type.key, {
                onSuccess: () => {
                  setConfirmDelete(false);
                  onLeave();
                },
                onError: () => setConfirmDelete(false),
              })
            }
            className={PRIMARY_BUTTON}
          >
            {t`Delete type`}
          </Button>
          <Button onPress={() => setConfirmDelete(false)} className={SECONDARY_BUTTON}>{t`Cancel`}</Button>
        </div>
      </ModalDialog>
    </article>
  );
}
