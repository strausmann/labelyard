// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
export const useIdPlacement = (key: string, enabled = true) =>
  useQuery({
    queryKey: [...typeKey(key), "id-placement"] as const,
    queryFn: () => apiFetch<IdPlacementRow[]>(`${typePath(key)}/id-placement`),
    enabled,
  });

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
