// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "../../api/client";
import type { components } from "../../api/schema.gen";
import { queryString } from "../../api/queryString";
import { sheetsListKey, typeKey, typesKey, typesListKey } from "../sheets/keys";

export { typeKey, typesKey } from "../sheets/keys";

type Schemas = components["schemas"];
export type SheetTypeRead = Schemas["SheetTypeRead"];
export type SheetTypeDetail = Schemas["SheetTypeDetail"];
export type SheetTypeCreate = Schemas["SheetTypeCreate"];
export type SheetTypePatch = Schemas["SheetTypePatch"];
export type VersionCreate = Schemas["VersionCreate"];
export type VersionRead = Schemas["VersionRead"];
export type ExportDoc = Schemas["ExportDoc"];
export type ImportResponse = Schemas["ImportResponse"];

export interface TypeFilters {
  page?: "A4" | "Letter";
  vendor?: string;
  q?: string;
  /** Label width in mm, ±0.5. */
  w?: number;
  /** Label height in mm, ±0.5. */
  h?: number;
  include_archived?: boolean;
  usage_state?: "in_use" | "used_before" | "unused";
  sort?: "key" | "usage" | "last_used";
  /** Not used on or after this day (YYYY-MM-DD); never used types included. */
  unused_since?: string;
}

/** How an import treats a type whose key exists: refuse, add a version, or store it under a new key. */
export interface ImportOptions {
  mode?: "strict" | "new_version" | "as_new_key";
  new_key?: string;
  new_prefix?: string;
}

const encode = encodeURIComponent;
const typePath = (key: string) => `/api/v1/sheet-types/${encode(key)}`;

export const useSheetTypes = (filters: TypeFilters = {}) =>
  useQuery({
    queryKey: typesKey(filters),
    queryFn: () => apiFetch<SheetTypeRead[]>(`/api/v1/sheet-types${queryString({ ...filters })}`),
  });

export type SheetTypeFacets = Schemas["SheetTypeFacets"];
export type TypeFacetFilters = Omit<TypeFilters, "vendor">;

/**
 * Vendor and page-size counts under every filter except the vendor, so choosing a vendor
 * keeps the other vendors in the list. Lives under the list key: a type write refreshes it.
 */
export const useSheetTypeFacets = (filters: TypeFacetFilters = {}) =>
  useQuery({
    queryKey: [...typesListKey, "facets", filters] as const,
    queryFn: () => apiFetch<SheetTypeFacets>(`/api/v1/sheet-types/facets${queryString({ ...filters })}`),
  });

export const useSheetType = (key: string, enabled = true) =>
  useQuery({ queryKey: typeKey(key), queryFn: () => apiFetch<SheetTypeDetail>(typePath(key)), enabled });

/**
 * A type write changes the lists, the type and (through the usage counts) the sheet lists.
 * `keyOf` names the type that changed: from the request, or (an import) from the answer.
 */
function useTypeWrite<V, R>(
  run: (v: V) => Promise<R>,
  keyOf: (v: V, data: R | undefined) => string | null,
  { allDetails = false, keysOf }: { allDetails?: boolean; keysOf?: (v: V) => string[] } = {},
) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: run,
    onSettled: (data, _error, v) => {
      void client.invalidateQueries({ queryKey: typesListKey });
      void client.invalidateQueries({ queryKey: sheetsListKey });
      // A catalog write can change other types with it (`also_updated`, a shared backing design): every open type.
      if (allDetails) {
        void client.invalidateQueries({ queryKey: typeDetailsKey });
        return;
      }
      const key = keyOf(v, data);
      if (key) void client.invalidateQueries({ queryKey: typeKey(key) });
      for (const other of keysOf?.(v) ?? []) void client.invalidateQueries({ queryKey: typeKey(other) });
    },
  });
}

const typeDetailsKey = ["sheet-types", "detail"] as const;

export const useCreateType = () =>
  useTypeWrite((body: SheetTypeCreate) => apiFetch<SheetTypeRead>("/api/v1/sheet-types", { method: "POST", json: body }), () => null);

export const usePatchType = () =>
  useTypeWrite(
    ({ key, patch }: { key: string; patch: SheetTypePatch }) =>
      apiFetch<SheetTypeRead>(typePath(key), { method: "PATCH", json: patch }),
    (v) => v.key,
  );

export const useAddVersion = () =>
  useTypeWrite(
    ({ key, version }: { key: string; version: VersionCreate }) =>
      apiFetch<VersionRead>(`${typePath(key)}/versions`, { method: "POST", json: version }),
    (v) => v.key,
  );

export type VersionFromSuggestion = Schemas["VersionFromSuggestion"];

/**
 * Applies a geometry suggestion from a check print as a new version (admin; design 7.4). The server names the run in
 * the change note and writes the sheet-type log entry.
 */
export const useApplySuggestion = () =>
  useTypeWrite(
    ({ key, body }: { key: string; body: VersionFromSuggestion }) =>
      apiFetch<VersionRead>(`${typePath(key)}/versions/from-suggestion`, { method: "POST", json: body }),
    (v) => v.key,
  );

export type VersionHistoryRead = Schemas["VersionHistoryRead"];

/** The versions with what changed against the one before, newest first. Under the type's key: a type write refreshes it. */
export const useTypeVersions = (key: string) =>
  useQuery({
    queryKey: [...typeKey(key), "versions"] as const,
    queryFn: () => apiFetch<VersionHistoryRead[]>(`${typePath(key)}/versions`),
  });

/** Adds a version with version `n`'s geometry ("Restored from version n"); refused while a sheet uses a newer one. */
export const useRestoreVersion = () =>
  useTypeWrite(
    ({ key, version }: { key: string; version: number }) =>
      apiFetch<VersionRead>(`${typePath(key)}/versions/${version}/restore`, { method: "POST" }),
    (v) => v.key,
  );

/** Archives a type, or brings it back with `unarchive: true`. */
export const useArchiveType = () =>
  useTypeWrite(
    ({ key, unarchive = false }: { key: string; unarchive?: boolean }) =>
      apiFetch<SheetTypeRead>(`${typePath(key)}/${unarchive ? "unarchive" : "archive"}`, { method: "POST" }),
    (v) => v.key,
  );

export const useDeleteType = () =>
  useTypeWrite((key: string) => apiFetch<void>(typePath(key), { method: "DELETE" }), (key) => key);

/** The download address of a type's export file (a version, or the current one). */
export function exportTypeUrl(key: string, version?: number | null): string {
  return `${typePath(key)}/export${queryString({ version })}`;
}

export const useImportType = () =>
  useTypeWrite(
    ({ doc, options = {} }: { doc: ExportDoc; options?: ImportOptions }) =>
      apiFetch<ImportResponse>(`/api/v1/sheet-types/import${queryString({ ...options })}`, { method: "POST", json: doc }),
    // The type the import touched: a new version changes an open detail, a new key has none cached yet.
    (_v, data) => data?.key ?? null,
  );

export type PreprintPresetRead = Schemas["PreprintPresetRead"];
export type IdPlacementRow = Schemas["IdPlacementRow"];

/** The vendor prints on the front of a sheet (#246); a type links one of them. */
export const usePreprintPresets = (enabled = true) =>
  useQuery({ queryKey: ["preprint-presets"], queryFn: () => apiFetch<PreprintPresetRead[]>("/api/v1/preprint-presets"), enabled });

/**
 * Per page printer, whether and where the margin sheet code of a type fits (#246). Lives under the type's key: a write
 * to the type (its position, its zones) refreshes it.
 */
export const idPlacementQuery = (key: string, tray: string | null = null) => ({
  queryKey: [...typeKey(key), "id-placement", tray] as const,
  queryFn: () => apiFetch<IdPlacementRow[]>(`${typePath(key)}/id-placement${queryString({ tray: tray ?? undefined })}`),
});

export const useIdPlacement = (key: string, enabled = true) => useQuery({ ...idPlacementQuery(key), enabled });

export type BackingDesignRead = Schemas["BackingDesignRead"];

/** The vendor prints on the back of a liner (#350); a type links one of them. */
export const useBackingDesigns = (enabled = true) =>
  useQuery({ queryKey: ["backing-designs"], queryFn: () => apiFetch<BackingDesignRead[]>("/api/v1/backing-designs"), enabled });

export type CatalogEntryRead = Schemas["CatalogEntryRead"];
export type CatalogEntryDetail = Schemas["CatalogEntryDetail"];
export type CatalogPage = Schemas["CatalogPage"];
export type CatalogWriteRead = Schemas["CatalogWriteRead"];

export interface CatalogFilters {
  q?: string;
  page?: "A4" | "Letter";
  vendor?: string;
  w?: number;
  h?: number;
  verified?: boolean;
  limit?: number;
}

/** The catalog lives under the type list key: installing or updating a type changes its `installed` flags. */
const catalogKey = (filters: object) => [...typesListKey, "catalog", filters] as const;
const catalogEntryKey = (key: string) => [...typesListKey, "catalog-entry", key] as const;

/** `GET /api/v1/sheet-catalog`: the entries the image ships, with their state here and the facets. */
export const useCatalog = (filters: CatalogFilters = {}, enabled = true) =>
  useQuery({
    queryKey: catalogKey(filters),
    queryFn: () => apiFetch<CatalogPage>(`/api/v1/sheet-catalog${queryString({ ...filters })}`),
    enabled,
  });

/** One catalog entry with its geometry (the grid preview, the update's diff). */
export const useCatalogEntry = (key: string | null) =>
  useQuery({
    queryKey: catalogEntryKey(key ?? ""),
    queryFn: () => apiFetch<CatalogEntryDetail>(`/api/v1/sheet-catalog/${encode(key ?? "")}`),
    enabled: !!key,
  });

/** `POST /api/v1/sheet-catalog/{key}/install`: under its own key, or (`prefix`, `as_new_key`) a new prefix or key. */
export const useInstallCatalogEntry = () =>
  useTypeWrite(
    ({ key, prefix, asNewKey }: { key: string; prefix?: string; asNewKey?: string }) =>
      apiFetch<CatalogWriteRead>(`/api/v1/sheet-catalog/${encode(key)}/install`, {
        method: "POST",
        json: { ...(asNewKey ? { as_new_key: asNewKey } : {}), ...(prefix ? { prefix } : {}) },
      }),
    (_v, data) => data?.key ?? null,
    { allDetails: true },
  );

/** `POST /api/v1/sheet-types/{key}/catalog-update`: the newer catalog revision as the next version. */
export const useCatalogUpdate = () =>
  useTypeWrite((key: string) => apiFetch<CatalogWriteRead>(`${typePath(key)}/catalog-update`, { method: "POST" }), (key) => key, {
    allDetails: true,
  });

/** `POST /api/v1/sheet-types/delete-unused`: all the named unused types, or none. */
export const useDeleteUnused = () =>
  useTypeWrite(
    (keys: string[]) => apiFetch<Schemas["DeleteUnusedResult"]>("/api/v1/sheet-types/delete-unused", { method: "POST", json: { keys } }),
    () => null,
    // Every removed type: an open detail of one must not keep showing it.
    { keysOf: (keys) => keys },
  );

export type SheetTypeLogEntry = Schemas["SheetTypeLogRead"];
export type LogNoteCreate = Schemas["LogNoteCreate"];
export type LogEntryPatch = Schemas["LogEntryPatch"];
export type LogVerdict = SheetTypeLogEntry["verdict"];
export type LogKind = SheetTypeLogEntry["kind"];

/** Entries per page of the log; the hub keeps the newest 500 per sheet type (#351). */
export const LOG_PAGE = 50;
export const LOG_KEPT = 500;
/** Longest note of a log entry (`NOTE_MAX` of `sheets/type_log.py`). */
export const LOG_NOTE_MAX = 2000;

export interface LogFilters {
  printer?: string;
  tray?: string;
  kind?: LogKind;
}

/** The log of a sheet type, newest first, a page at a time. Under the type's key: every type write refreshes it. */
export const useTypeLog = (key: string, filters: LogFilters = {}, enabled = true) =>
  useInfiniteQuery({
    queryKey: [...typeKey(key), "log", filters] as const,
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      apiFetch<SheetTypeLogEntry[]>(`${typePath(key)}/log${queryString({ ...filters, limit: LOG_PAGE, offset: pageParam })}`),
    // A full page may have a next one; an empty next page ends the list.
    getNextPageParam: (last, pages) => (last.length === LOG_PAGE ? pages.length * LOG_PAGE : undefined),
    enabled,
  });

/** A hand entry (admin): a note with a verdict, optionally about a printer and tray. */
export const useAddLogNote = () =>
  useTypeWrite(
    ({ key, body }: { key: string; body: LogNoteCreate }) =>
      apiFetch<SheetTypeLogEntry>(`${typePath(key)}/log`, { method: "POST", json: body }),
    (v) => v.key,
  );

/** Sets the verdict and note of an entry (admin); nothing else of it changes. */
export const usePatchLogEntry = () =>
  useTypeWrite(
    ({ key, id, patch }: { key: string; id: string; patch: LogEntryPatch }) =>
      apiFetch<SheetTypeLogEntry>(`${typePath(key)}/log/${encode(id)}`, { method: "PATCH", json: patch }),
    (v) => v.key,
  );
