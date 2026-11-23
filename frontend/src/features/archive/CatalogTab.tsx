// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { Link } from "@tanstack/react-router";
import { useEffect, useId, useState } from "react";
import { Button, Checkbox } from "react-aria-components";
import { ApiError } from "../../api/client";
import { formatMm } from "../sheets/cellState";
import { GeometryDiagram } from "../sheets/GeometryDiagram";
import { typeLabel } from "../sheets/typeLabel";
import { HINT, SelectField, TextInput } from "../settings/controls";
import { PANEL, PRIMARY_BUTTON, SECONDARY_BUTTON } from "../ui";
import { useCatalog, useCatalogEntry, useInstallCatalogEntry, type CatalogEntryRead } from "./api";
import { useArchiveErrorText } from "./errors";
import { catalogQuery, MAX_QUERY, type ArchiveSearch } from "./search";

const ANY = "__any__";
const FILTER_DELAY_MS = 200;
/** The catalog route answers at most 200 entries per request; the shipped catalog is far smaller. */
const CATALOG_LIMIT = 200;

/** A small picture of the grid: enough to tell a 5 × 13 sheet from a 3 × 8 one at a glance. */
export function MiniGrid({ cols, rows, className = "" }: { cols: number; rows: number; className?: string }) {
  const c = Math.min(cols, 12);
  const r = Math.min(rows, 30);
  return (
    <svg viewBox={`0 0 ${c * 4} ${r * 4}`} preserveAspectRatio="none" aria-hidden="true" className={`h-16 w-12 shrink-0 rounded-r1 border border-line bg-accent-soft ${className}`}>
      {Array.from({ length: c - 1 }, (_, i) => (
        <line key={`c${i}`} x1={(i + 1) * 4} x2={(i + 1) * 4} y1={0} y2={r * 4} className="stroke-accent/40" strokeWidth={0.4} />
      ))}
      {Array.from({ length: r - 1 }, (_, i) => (
        <line key={`r${i}`} y1={(i + 1) * 4} y2={(i + 1) * 4} x1={0} x2={c * 4} className="stroke-accent/40" strokeWidth={0.4} />
      ))}
    </svg>
  );
}

/** Verified with its day, or not yet validated: a word and a shape, never colour alone. */
export function StateBadge({ verified, on }: { verified: boolean; on: string | null }) {
  const { t, i18n } = useLingui();
  if (verified) {
    const day = on ? new Date(`${on}T00:00:00`).toLocaleDateString(i18n.locale) : null;
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-ok px-2.5 text-small leading-6 font-bold text-ink">
        <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" className="fill-none stroke-ok stroke-2">
          <path d="M2.5 6.5 5 9l4.5-5.5" />
        </svg>
        {day ? t`Verified ${day}` : t`Verified`}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-warn px-2.5 text-small leading-6 font-bold text-ink">
      <svg viewBox="0 0 10 10" width="8" height="8" aria-hidden="true" className="fill-warn">
        <path d="M5 0 10 5 5 10 0 5Z" />
      </svg>
      {t`Not yet validated`}
    </span>
  );
}

function EntryDetails({ entry }: { entry: CatalogEntryRead }) {
  const { t } = useLingui();
  const detail = useCatalogEntry(entry.key);
  if (detail.isPending) return <p className="text-ink-2">{t`Loading…`}</p>;
  if (detail.isError) return <p role="alert">{t`The entry could not be loaded.`}</p>;
  const g = detail.data.geometry;
  const fact = (label: string, value: string | null | undefined) =>
    value ? (
      <div className="contents">
        <dt className="text-ink-2">{label}</dt>
        <dd className="m-0 font-bold [overflow-wrap:anywhere]">{value}</dd>
      </div>
    ) : null;
  // The diagram lists the measurements itself (page, label size, grid, pitch, margins, corner radius); below it
  // stands where the values come from.
  return (
    <div className="flex min-w-0 flex-col gap-4 border-t border-line pt-4">
      <GeometryDiagram geometry={g} hints={[]} />
      <dl className="m-0 grid grid-cols-[auto_minmax(0,1fr)] content-start gap-x-4 gap-y-1.5 text-small">
        {fact(t`Source`, detail.data.source_name ?? detail.data.source_url)}
        {fact(t`Evidence`, detail.data.evidence)}
        {fact(t`Code prefix`, detail.data.prefix)}
      </dl>
    </div>
  );
}

/**
 * How an entry is added after a conflict: `prefix` keeps the entry's key with another prefix (`409 prefix_taken`),
 * `copy` installs it under a new key (`409 type_exists`). Once `copy` was entered it stays until cancelled: a later
 * `prefix_taken` for the copy only asks for another prefix, never drops the new key.
 */
type ConflictMode = "prefix" | "copy";

function ConflictForm({
  entry,
  mode,
  newKey,
  setNewKey,
  prefix,
  setPrefix,
  prefixTakenAgain,
  onInstall,
  onCancel,
  pending,
}: {
  entry: CatalogEntryRead;
  mode: ConflictMode;
  newKey: string;
  setNewKey: (v: string) => void;
  prefix: string;
  setPrefix: (v: string) => void;
  /** The last answer was `prefix_taken` while adding under a new key. */
  prefixTakenAgain: boolean;
  onInstall: () => void;
  onCancel: () => void;
  pending: boolean;
}) {
  const { t } = useLingui();
  const copy = mode === "copy";
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (prefix.trim() && (!copy || newKey.trim())) onInstall();
      }}
      className="flex flex-col gap-3 rounded-r2 bg-info-soft p-3"
    >
      <p className="m-0 text-small">
        {copy
          ? t`A type with the key ${entry.key} exists already. Add this entry under a new key and code prefix; it is then your own copy without catalog updates.`
          : t`Another type already uses the code prefix of this entry. Add it with a new prefix:`}
      </p>
      {copy && prefixTakenAgain ? <p role="alert" className="m-0 text-small font-bold">{t`Another type already uses this code prefix. Choose another one.`}</p> : null}
      <div className="grid gap-3 sm:grid-cols-2">
        {copy ? <TextInput label={t`New key`} value={newKey} onChange={setNewKey} mono /> : null}
        <TextInput label={t`New code prefix`} value={prefix} onChange={setPrefix} mono maxLength={10} />
      </div>
      <div className="flex flex-wrap gap-3">
        <Button type="submit" isDisabled={pending || !prefix.trim() || (copy && !newKey.trim())} className={PRIMARY_BUTTON}>
          {copy ? t`Add under this key` : t`Add with this prefix`}
        </Button>
        <Button onPress={onCancel} isDisabled={pending} className={SECONDARY_BUTTON}>{t`Cancel`}</Button>
      </div>
    </form>
  );
}

function EntryCard({ entry, admin }: { entry: CatalogEntryRead; admin: boolean }) {
  const { t, i18n } = useLingui();
  const errorText = useArchiveErrorText();
  const install = useInstallCatalogEntry();
  const [open, setOpen] = useState(false);
  const detailsId = useId();
  const mm = (v: number) => formatMm(v, i18n.locale);
  const title = typeLabel({ key: entry.key, vendor: entry.vendor, product_codes: entry.product_codes });
  // The conflict form lives here, not in the form: a retry clears the mutation's error while it runs, and the chosen
  // mode, new key and prefix must survive that.
  const [mode, setMode] = useState<ConflictMode | null>(null);
  const [newKey, setNewKey] = useState("");
  const [prefix, setPrefix] = useState("");
  const [prefixTakenAgain, setPrefixTakenAgain] = useState(false);
  const conflictCode = (error: unknown) =>
    error instanceof ApiError && (error.code === "prefix_taken" || error.code === "type_exists") ? error : null;
  const run = (body: { prefix?: string; asNewKey?: string } = {}) =>
    install.mutate(
      { key: entry.key, ...body },
      {
        onError: (error) => {
          const conflict = conflictCode(error);
          if (!conflict) return;
          const suggested = typeof conflict.errorDetail?.suggested_prefix === "string" ? conflict.errorDetail.suggested_prefix : null;
          if (conflict.code === "type_exists") {
            // The copy: a new key, and from here on every retry carries it.
            if (mode !== "copy") setNewKey(`${entry.key}-2`);
            setMode("copy");
            setPrefixTakenAgain(false);
          } else {
            if (mode !== "copy") setMode("prefix");
            setPrefixTakenAgain(mode === "copy");
          }
          if (suggested) setPrefix(suggested);
        },
      },
    );
  const cancelConflict = () => {
    setMode(null);
    setNewKey("");
    setPrefix("");
    setPrefixTakenAgain(false);
    install.reset();
  };
  const conflictShown = mode !== null && !install.isSuccess;
  const installedAs = install.data && install.data.key !== entry.key ? install.data.key : null;

  let action = null;
  if (entry.installed) {
    action = entry.update_available ? (
      <div className="flex flex-wrap items-center justify-end gap-2">
        <span className="inline-flex items-center rounded-full bg-info-soft px-2.5 text-small leading-6 font-bold text-ink">{t`Update: catalog version ${entry.revision}`}</span>
        <Link to="/sheets/archive" search={{ type: entry.key }} className={`${SECONDARY_BUTTON} no-underline`}>
          {t`Update`}
        </Link>
      </div>
    ) : (
      <span className="inline-flex min-h-11 items-center gap-1.5 font-bold text-ink-2">
        <svg viewBox="0 0 12 12" width="14" height="14" aria-hidden="true" className="fill-none stroke-current stroke-2">
          <path d="M2.5 6.5 5 9l4.5-5.5" />
        </svg>
        {t`Installed`}
      </span>
    );
  } else if (admin && !install.isSuccess) {
    action = (
      <Button isDisabled={install.isPending || conflictShown} onPress={() => run()} className={`${PRIMARY_BUTTON} gap-1.5 px-4`}>
        <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" className="fill-none stroke-current stroke-2">
          <path d="M6 1.5v9M1.5 6h9" />
        </svg>
        {t`Add`}
      </Button>
    );
  }

  return (
    <article aria-label={entry.key} className={`flex min-w-0 flex-col gap-3 rounded-r3 border bg-surface p-4 transition-colors duration-150 ${open ? "border-accent" : "border-line"}`}>
      <div className="flex min-w-0 gap-4">
        <MiniGrid cols={entry.cols} rows={entry.rows} />
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <div className="flex min-w-0 flex-wrap items-start justify-between gap-x-3 gap-y-2">
            <h3 className="min-w-0 font-bold text-ink [overflow-wrap:anywhere]">{title}</h3>
            {action}
          </div>
          <p className="m-0 text-small text-ink-2">
            {entry.cols} × {entry.rows} · {mm(entry.label_w_mm).replace(/ mm$/, "")} × {mm(entry.label_h_mm)} · {entry.page_size}
          </p>
          <p className="m-0 font-mono text-small text-ink-2 [overflow-wrap:anywhere]">{entry.key}</p>
          <div className="flex flex-wrap items-center gap-2">
            <StateBadge verified={entry.verified} on={entry.verified_on} />
          </div>
          {entry.evidence ? <p className="m-0 text-small text-ink-2 [overflow-wrap:anywhere]">{entry.evidence}</p> : null}
          {entry.installed && entry.catalog_newer ? (
            <p className="m-0 text-small text-ink-2">{t`Changed here; the catalog has a newer version (${entry.revision}).`}</p>
          ) : null}
          {install.isSuccess ? (
            <p role="status" className="m-0 text-small font-bold">
              {installedAs ? t`Added as ${installedAs}.` : t`Added.`}
              {install.data.also_updated.length > 0 ? ` ${t`Also updated: ${install.data.also_updated.join(", ")}`}` : null}
            </p>
          ) : null}
          <Button
            aria-expanded={open}
            aria-controls={detailsId}
            onPress={() => setOpen((v) => !v)}
            className="inline-flex min-h-11 items-center gap-1.5 self-start text-small font-bold text-link underline underline-offset-2 data-[focus-visible]:outline-3 data-[focus-visible]:outline-link"
          >
            {open ? t`Hide details` : t`Show details`}
          </Button>
        </div>
      </div>
      {conflictShown ? (
        <ConflictForm
          entry={entry}
          mode={mode}
          newKey={newKey}
          setNewKey={setNewKey}
          prefix={prefix}
          setPrefix={setPrefix}
          prefixTakenAgain={prefixTakenAgain}
          onInstall={() => run({ prefix: prefix.trim(), asNewKey: mode === "copy" ? newKey.trim() : undefined })}
          onCancel={cancelConflict}
          pending={install.isPending}
        />
      ) : null}
      {install.error && !conflictCode(install.error) ? <p role="alert" className="m-0">{errorText(install.error)}</p> : null}
      <div id={detailsId} hidden={!open}>
        {open ? <EntryDetails entry={entry} /> : null}
      </div>
    </article>
  );
}

/** The Catalog tab of the sheet archive (#335, mockup M4): search, filters, and one-click add. */
export function CatalogTab({ search, admin, commit }: { search: ArchiveSearch; admin: boolean; commit: (next: Partial<ArchiveSearch>) => void }) {
  const { t } = useLingui();
  const [typed, setTyped] = useState(search.q ?? "");
  useEffect(() => {
    const value = typed.trim() === "" ? undefined : typed.trim().slice(0, MAX_QUERY);
    if (value === search.q) return;
    const timer = setTimeout(() => commit({ q: value }), FILTER_DELAY_MS);
    return () => clearTimeout(timer);
  }, [typed, search.q, commit]);

  const query = catalogQuery(search.q);
  const filters = { ...query, page: search.page, vendor: search.vendor, verified: search.verified, limit: CATALOG_LIMIT };
  const list = useCatalog(filters);
  const vendors = (list.data?.facets.vendors ?? []).map((f) => f.value);
  if (search.vendor && !vendors.includes(search.vendor)) vendors.push(search.vendor);
  // The catalog's `total` counts under the filters; "4 of 39" needs the whole catalog (the tab count's query).
  const all = useCatalog({ limit: 1 });
  const total = all.data?.total ?? list.data?.total;

  return (
    <div className="grid min-w-0 gap-6 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)] lg:items-start">
      <form role="search" aria-label={t`Search the catalog`} onSubmit={(e) => e.preventDefault()} className={`${PANEL} flex min-w-0 flex-col gap-4`}>
        <TextInput label={t`Search`} value={typed} onChange={setTyped} maxLength={MAX_QUERY} description={t`Vendor, product code or size.`} />
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
          <SelectField
            label={t`Page`}
            value={search.page ?? ANY}
            options={[
              { id: ANY, label: t`Any page size` },
              { id: "A4", label: "A4" },
              { id: "Letter", label: "Letter" },
            ]}
            onChange={(id) => commit({ page: id === "A4" || id === "Letter" ? id : undefined })}
          />
          <SelectField
            label={t`Vendor`}
            value={search.vendor ?? ANY}
            options={[{ id: ANY, label: t`All vendors (${vendors.length})` }, ...vendors.map((v) => ({ id: v, label: v }))]}
            onChange={(id) => commit({ vendor: id === ANY ? undefined : id })}
          />
        </div>
        <div className="flex flex-col gap-1">
          <Checkbox
            isSelected={search.verified === true}
            onChange={(on) => commit({ verified: on ? true : undefined })}
            className="group flex min-h-11 items-center gap-3 rounded-r1 data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link"
          >
            <span aria-hidden="true" className="flex size-5 shrink-0 items-center justify-center rounded-r1 border border-line-strong bg-sunken group-data-[selected]:border-accent group-data-[selected]:bg-accent">
              <svg viewBox="0 0 12 12" width="12" height="12" className="fill-none stroke-accent-ink stroke-2 opacity-0 group-data-[selected]:opacity-100">
                <path d="M2.5 6.5 5 9l4.5-5.5" />
              </svg>
            </span>
            <span>{t`Verified only`}</span>
          </Checkbox>
          <p className={HINT}>{t`Hides entries nobody has checked on a real sheet.`}</p>
        </div>
      </form>

      <div className="flex min-w-0 flex-col gap-3">
        {list.isPending ? <p className="text-ink-2">{t`Loading…`}</p> : null}
        {list.isError ? <p role="alert">{t`The catalog could not be loaded.`}</p> : null}
        {list.data ? (
          <>
            <p className="m-0 text-small text-ink-2" aria-live="polite">
              {t`${list.data.items.length} of ${total} entries`}
            </p>
            {list.data.items.length === 0 ? (
              <p className="text-ink-2">{t`No catalog entry matches.`}</p>
            ) : (
              // The list is never taller than the visible area (keyboard open included) and scrolls inside.
              <ul
                aria-label={t`Catalog entries`}
                data-testid="catalog-results"
                className="m-0 flex max-h-[calc(var(--vvh)-1rem)] list-none flex-col gap-2 overflow-y-auto overscroll-contain p-0 lg:p-1"
              >
                {list.data.items.map((entry) => (
                  <li key={entry.key}>
                    <EntryCard entry={entry} admin={admin} />
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : null}
      </div>
    </div>
  );
}
