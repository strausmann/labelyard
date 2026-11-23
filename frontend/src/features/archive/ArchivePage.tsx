// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import { Button, Tab, TabList, TabPanel, Tabs } from "react-aria-components";
import { useSession } from "../../app/session";
import { formatMm } from "../sheets/cellState";
import { typeTitle } from "../sheets/typeLabel";
import { FIELD, PRIMARY_BUTTON } from "../../shell/SignIn";
import { LABEL, SelectField, SwitchField, TextInput } from "../settings/controls";
import { PANEL, SECONDARY_BUTTON } from "../ui";
import { useCatalog, useSheetTypeFacets, useSheetTypes, type SheetTypeRead } from "./api";
import { CatalogTab, MiniGrid } from "./CatalogTab";
import { ImportDialog } from "./ImportDialog";
import { RemoveUnusedDialog } from "./RemoveUnusedDialog";
import { MAX_QUERY, parseDecimal, type ArchiveSearch } from "./search";
import { TypeBadges, TypeDetail } from "./TypeDetail";
import { TypeForm } from "./TypeForm";

const ANY = "__any__";
const FILTER_DELAY_MS = 200;

const sizeOf = (value: string) => {
  const n = parseDecimal(value);
  return n !== null && n > 0 && n <= 1000 ? n : undefined;
};

/**
 * Typing in a filter waits a moment before it asks the server (and before the address changes).
 * When the address changes from outside (a link, back and forward), the boxes follow it.
 */
function useTypedFilters(search: ArchiveSearch, commit: (next: Partial<ArchiveSearch>) => void) {
  const [q, setQ] = useState(search.q ?? "");
  const [w, setW] = useState(search.w === undefined ? "" : String(search.w));
  const [h, setH] = useState(search.h === undefined ? "" : String(search.h));
  const typed = { q: q.trim() === "" ? undefined : q.trim(), w: sizeOf(w), h: sizeOf(h) };
  const inSync = typed.q === search.q && typed.w === search.w && typed.h === search.h;

  // The address moved. If it is what we committed ourselves (even if the boxes have moved on
  // since: "4" lands after "45" was typed), leave them alone, or the box would be rewritten under
  // the cursor. Otherwise it came from outside (a link, back and forward): follow it.
  // (React's pattern for state that follows a changing input: set it while rendering, not in an effect.)
  type Filters = { q: string | undefined; w: number | undefined; h: number | undefined };
  // Our own commits the address has not shown yet, oldest first.
  const [committed, setCommitted] = useState<Filters[]>([]);
  const [seen, setSeen] = useState({ q: search.q, w: search.w, h: search.h });
  if (seen.q !== search.q || seen.w !== search.w || seen.h !== search.h) {
    setSeen({ q: search.q, w: search.w, h: search.h });
    const index = committed.findIndex((c) => c.q === search.q && c.w === search.w && c.h === search.h);
    const ours = index >= 0;
    // Ours: it and everything older is shown now. Not ours: the list is stale, a later link to the same filter must follow too.
    setCommitted(ours ? committed.slice(index + 1) : []);
    if (!inSync && !ours) {
      setQ(search.q ?? "");
      setW(search.w === undefined ? "" : String(search.w));
      setH(search.h === undefined ? "" : String(search.h));
    }
  }

  useEffect(() => {
    if (inSync) return;
    const timer = setTimeout(() => {
      setCommitted((list) => [...list, { q: typed.q, w: typed.w, h: typed.h }]);
      commit({ q: typed.q, w: typed.w, h: typed.h });
    }, FILTER_DELAY_MS);
    return () => clearTimeout(timer);
  }, [typed.q, typed.w, typed.h, inSync, commit]);
  return { q, setQ, w, setW, h, setH, clear: () => { setQ(""); setW(""); setH(""); } };
}

const TAB =
  "-mb-px inline-flex min-h-11 cursor-pointer items-center gap-2 whitespace-nowrap border-b-2 border-transparent px-4 font-bold text-ink-2 transition-colors duration-150 data-[selected]:border-accent data-[selected]:text-ink data-[focus-visible]:outline-3 data-[focus-visible]:-outline-offset-3 data-[focus-visible]:outline-link [@media(hover:hover)]:hover:text-ink";

/** The usage state of a type in a word, with a shape (#335, spec 6.1.5). */
function UsageBadge({ state }: { state: SheetTypeRead["usage_state"] }) {
  const { t } = useLingui();
  const words = { in_use: t`In use`, used_before: t`Used before`, unused: t`Unused` };
  const tone = { in_use: "border-ok", used_before: "border-link", unused: "border-line-strong" }[state];
  return <span className={`inline-flex items-center rounded-full border px-2.5 text-small leading-6 font-bold text-ink ${tone}`}>{words[state]}</span>;
}

function UsageLines({ type }: { type: SheetTypeRead }) {
  const { t, i18n } = useLingui();
  const last = type.last_used_at ? new Date(type.last_used_at).toLocaleDateString(i18n.locale) : "—";
  return (
    <>
      <span className="text-small text-ink-2">
        {plural(type.sheets_total, { one: "# sheet", other: "# sheets" })} · {t`last used ${last}`}
      </span>
      <span className="text-small text-ink-2">
        {type.sheets_total === 0
          ? t`Never used`
          : type.sheets_with_free_cells === 0
            ? t`No sheet with free cells`
            : `${plural(type.sheets_with_free_cells, { one: "# sheet with free cells", other: "# sheets with free cells" })} · ${plural(type.free_labels, { one: "# free label", other: "# free labels" })}`}
      </span>
    </>
  );
}

/** A fresh installation has no sheet types (#335): they come from the catalog. Mockup M4, view "empty". */
function NoTypesYet({ admin, onCatalog, onImport }: { admin: boolean; onCatalog: () => void; onImport: () => void }) {
  const { t } = useLingui();
  return (
    <section aria-labelledby="no-types-heading" className={`${PANEL} flex max-w-2xl flex-col gap-4`}>
      <h2 id="no-types-heading" className="text-section font-bold">{t`No sheet types yet`}</h2>
      <p className="m-0">{t`A new hub starts with no sheet types. Add the ones you use from the catalog; each takes one click. You can also import a type from a file.`}</p>
      <div className="flex flex-wrap gap-3">
        <Button onPress={onCatalog} className={PRIMARY_BUTTON}>{t`Open the catalog`}</Button>
        {admin ? <Button onPress={onImport} className={SECONDARY_BUTTON}>{t`Import type`}</Button> : null}
      </div>
      <p className="m-0 rounded-r2 bg-info-soft p-3 text-small">
        {t`Existing installations keep every type they have. The catalog only adds entries and offers updates; it never changes an installed type by itself.`}
      </p>
    </section>
  );
}

function TypeRow({ type, selected }: { type: SheetTypeRead; selected: boolean }) {
  const { i18n } = useLingui();
  const g = type.geometry;
  const mm = (v: number) => formatMm(v, i18n.locale);
  return (
    <li>
      <Link
        to="/sheets/archive"
        search={(prev) => ({ ...prev, type: type.key })}
        aria-current={selected ? "true" : undefined}
        className={`press flex min-h-11 min-w-0 gap-3 rounded-r2 border p-3 no-underline transition-colors duration-150 focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-link ${
          selected ? "border-accent bg-accent-soft" : "border-line bg-surface hover:bg-sunken"
        }`}
      >
        <MiniGrid cols={g.cols} rows={g.rows} />
        <span className="flex min-w-0 flex-col gap-1">
          <span className="font-bold text-ink [overflow-wrap:anywhere]">{typeTitle(type)}</span>
          <span className="font-mono text-small text-ink-2 [overflow-wrap:anywhere]">{type.key}</span>
          <span className="text-small text-ink-2">
            {g.page_size} · {mm(g.label_w)} × {mm(g.label_h)} · {plural(g.cols * g.rows, { one: "# label", other: "# labels" })}
          </span>
          <span className="flex flex-wrap gap-1.5">
            <UsageBadge state={type.usage_state} />
            <TypeBadges type={type} />
          </span>
          <UsageLines type={type} />
        </span>
      </Link>
    </li>
  );
}

/** `/sheets/archive`: the catalogue of sheet types, their versions, and (for admins) the ways to change them. */
export function SheetArchivePage() {
  const { t } = useLingui();
  const session = useSession();
  const admin = session.scope === "admin";
  const search = useSearch({ strict: false }) as ArchiveSearch;
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState(false);
  // On a phone the filters take the first screen: they start folded unless one is in use.
  const [filtersOpen, setFiltersOpen] = useState(() => [search.q, search.page, search.vendor, search.w, search.h, search.archived, search.state, search.since].some(Boolean));

  const commit = useCallback(
    (next: Partial<ArchiveSearch>) => void navigate({ to: "/sheets/archive", search: (prev) => ({ ...prev, ...next }), replace: true }),
    [navigate],
  );
  const typed = useTypedFilters(search, commit);

  const [removing, setRemoving] = useState(false);
  const catalogTab = search.tab === "catalog";
  const list = useSheetTypes({
    page: search.page,
    vendor: search.vendor,
    q: search.q,
    w: search.w,
    h: search.h,
    include_archived: search.archived,
    usage_state: search.state,
    sort: search.sort,
    unused_since: search.since,
  });
  // The counts of the two tabs: every installed type, and every catalog entry.
  // Archived types included: a hub whose only types are archived is not a fresh one.
  const installedAll = useSheetTypes({ include_archived: true });
  const catalogAll = useCatalog({ limit: 1 });
  const installedCount = installedAll.data?.filter((type) => type.archived_at === null).length;
  const catalogCount = catalogAll.data?.total;
  const freshHub = installedAll.data?.length === 0;
  // Changing tabs starts the other tab without the filters of this one.
  const showTab = (tab: "installed" | "catalog") =>
    void navigate({ to: "/sheets/archive", search: tab === "catalog" ? { tab: "catalog" } : {} });
  // The vendors to choose from are counted under every filter except the vendor,
  // so choosing one does not hide the others (and a long list is not fetched twice).
  const facets = useSheetTypeFacets({
    page: search.page,
    q: search.q,
    w: search.w,
    h: search.h,
    include_archived: search.archived,
    usage_state: search.state,
  });
  const vendors = (facets.data?.vendors ?? []).map((f) => f.value);
  if (search.vendor && !vendors.includes(search.vendor)) vendors.push(search.vendor);

  const filtered = Boolean(search.q || search.page || search.vendor || search.w || search.h || search.state || search.since);
  const activeFilters = [search.q, search.page, search.vendor, search.w, search.h, search.archived, search.state, search.since].filter(Boolean).length;
  const open = search.type;

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-title font-bold">{t`Sheet archive`}</h1>
        {admin && !creating ? (
          <div className="flex flex-wrap gap-3">
            {!catalogTab && !freshHub ? (
              <Button onPress={() => setRemoving(true)} className={`${SECONDARY_BUTTON} gap-2`}>
                <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" className="fill-none stroke-current stroke-[1.5]">
                  <path d="M2 3h8M4.5 3V1.8h3V3M3 3l.5 7.5h5L9 3" />
                </svg>
                {t`Remove unused types`}
              </Button>
            ) : null}
            <Button onPress={() => setCreating(true)} className={PRIMARY_BUTTON}>{t`New type`}</Button>
            <Button onPress={() => setImporting(true)} className={SECONDARY_BUTTON}>{t`Import type`}</Button>
          </div>
        ) : null}
      </div>

      {creating ? (
        <TypeForm
          mode={{ kind: "create" }}
          onDone={(key) => {
            setCreating(false);
            if (key) void navigate({ to: "/sheets/archive", search: (prev) => ({ ...prev, type: key }) });
          }}
        />
      ) : (
        <Tabs selectedKey={catalogTab ? "catalog" : "installed"} onSelectionChange={(key) => showTab(key === "catalog" ? "catalog" : "installed")} className="flex min-w-0 flex-col gap-6">
          <TabList aria-label={t`Sheet types`} className="flex flex-wrap gap-1 border-b border-line">
            <Tab id="installed" className={TAB}>
              {t`Installed`}
              {installedCount !== undefined ? <span className="text-small font-normal text-ink-2 tabular-nums">{installedCount}</span> : null}
            </Tab>
            <Tab id="catalog" className={TAB}>
              {t`Catalog`}
              {catalogCount !== undefined ? <span className="text-small font-normal text-ink-2 tabular-nums">{catalogCount}</span> : null}
            </Tab>
          </TabList>
          <TabPanel id="catalog" className="min-w-0 rounded-r2 data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link">
            <CatalogTab search={search} admin={admin} commit={commit} />
          </TabPanel>
          <TabPanel id="installed" className="min-w-0 rounded-r2 data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link">
        {freshHub && !open ? (
          <NoTypesYet admin={admin} onCatalog={() => showTab("catalog")} onImport={() => setImporting(true)} />
        ) : (
        <div className="grid min-w-0 gap-8 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)] lg:items-start">
          <div className={`flex min-w-0 flex-col gap-4 ${open ? "hidden lg:flex" : ""}`}>
            <Button
              aria-expanded={filtersOpen}
              aria-controls="archive-filters"
              onPress={() => setFiltersOpen((open) => !open)}
              className={`${SECONDARY_BUTTON} w-full justify-between lg:hidden`}
            >
              <span>{activeFilters > 0 ? t`Filters (${activeFilters} active)` : t`Filters`}</span>
              <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" className={`fill-none stroke-current stroke-2 transition-transform duration-150 ${filtersOpen ? "rotate-180" : ""}`}>
                <path d="M2 4.5 6 8.5 10 4.5" />
              </svg>
            </Button>
            <form
              id="archive-filters"
              role="search"
              aria-label={t`Filter the sheet types`}
              onSubmit={(e) => e.preventDefault()}
              className={`${PANEL} ${filtersOpen ? "flex" : "hidden"} flex-col gap-4 lg:flex`}
            >
              <TextInput label={t`Search`} value={typed.q} onChange={typed.setQ} maxLength={MAX_QUERY} description={t`Vendor, key, description or product code`} />
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
                <SelectField
                  label={t`Page size`}
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
                  options={[{ id: ANY, label: t`Any vendor` }, ...vendors.map((v) => ({ id: v, label: v }))]}
                  onChange={(id) => commit({ vendor: id === ANY ? undefined : id })}
                  isDisabled={facets.isPending}
                  placeholder={facets.isPending ? t`Loading vendors…` : undefined}
                />
                {facets.isError ? <p className="text-small text-ink-2">{t`The vendors could not be loaded.`}</p> : null}
              </div>
              <div className="grid grid-cols-2 gap-4">
                <TextInput label={t`Label width (mm)`} value={typed.w} onChange={typed.setW} inputMode="decimal" />
                <TextInput label={t`Label height (mm)`} value={typed.h} onChange={typed.setH} inputMode="decimal" />
              </div>
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
                <SelectField
                  label={t`State`}
                  value={search.state ?? ANY}
                  options={[
                    { id: ANY, label: t`All` },
                    { id: "in_use", label: t`In use` },
                    { id: "used_before", label: t`Used before` },
                    { id: "unused", label: t`Unused` },
                  ]}
                  onChange={(id) => commit({ state: id === "in_use" || id === "used_before" || id === "unused" ? id : undefined })}
                />
                <SelectField
                  label={t`Sort by`}
                  value={search.sort ?? ANY}
                  options={[
                    { id: ANY, label: t`Key` },
                    { id: "usage", label: t`Usage` },
                    { id: "last_used", label: t`Last used` },
                  ]}
                  onChange={(id) => commit({ sort: id === "usage" || id === "last_used" ? id : undefined })}
                />
              </div>
              <div className="flex min-w-0 flex-col gap-1.5">
                <label htmlFor="archive-since" className={LABEL}>{t`Not used since`}</label>
                <input
                  id="archive-since"
                  type="date"
                  value={search.since ?? ""}
                  onChange={(e) => commit({ since: /^\d{4}-\d{2}-\d{2}$/.test(e.target.value) ? e.target.value : undefined })}
                  className={FIELD}
                />
              </div>
              <SwitchField label={t`Show archived types`} isSelected={search.archived === true} onChange={(on) => commit({ archived: on ? true : undefined })} />
              {filtered ? (
                <Button
                  onPress={() => {
                    typed.clear();
                    commit({ q: undefined, page: undefined, vendor: undefined, w: undefined, h: undefined, state: undefined, since: undefined });
                  }}
                  className={`${SECONDARY_BUTTON} self-start`}
                >
                  {t`Clear filters`}
                </Button>
              ) : null}
            </form>

            {list.isPending ? <p className="text-ink-2">{t`Loading…`}</p> : null}
            {list.isError ? <p role="alert">{t`The sheet types could not be loaded.`}</p> : null}
            {list.data ? (
              list.data.length === 0 ? (
                <p className="text-ink-2">{t`No sheet type matches.`}</p>
              ) : (
                <ul aria-label={t`Sheet types`} className="flex flex-col gap-2 lg:max-h-[65dvh] lg:overflow-y-auto lg:p-1">
                  {list.data.map((type) => (
                    <TypeRow key={type.key} type={type} selected={type.key === open} />
                  ))}
                </ul>
              )
            ) : null}
          </div>

          <div className="min-w-0">
            {open ? (
              <TypeDetail
                key={open}
                typeKey={open}
                admin={admin}
                onLeave={() => void navigate({ to: "/sheets/archive", search: (prev) => ({ ...prev, type: undefined }) })}
              />
            ) : (
              <p className="hidden text-ink-2 lg:block">{t`Choose a sheet type to see its measurements and versions.`}</p>
            )}
          </div>
        </div>
        )}
          </TabPanel>
        </Tabs>
      )}

      {admin ? <RemoveUnusedDialog isOpen={removing} onClose={() => setRemoving(false)} /> : null}
      {admin ? (
        <ImportDialog
          isOpen={importing}
          onClose={() => setImporting(false)}
          onShow={(key) => void navigate({ to: "/sheets/archive", search: (prev) => ({ ...prev, type: key }) })}
        />
      ) : null}
    </div>
  );
}
