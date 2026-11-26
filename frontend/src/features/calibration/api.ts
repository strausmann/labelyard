// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "../../api/client";
import type { components } from "../../api/schema.gen";
import { queryString } from "../../api/queryString";
import { useEffect, useRef } from "react";
import { releaseClaimPending, watchPrint } from "../jobs/printWatch";
import { statusKey } from "../printers/api";
import { calibrationKey, capabilitiesKey } from "../sheets/keys";

export { calibrationKey, capabilitiesKey } from "../sheets/keys";

type Schemas = components["schemas"];
export type CalibrationRead = Schemas["CalibrationRead"];
export type CalibrationPut = Schemas["CalibrationPut"];
export type FitRequest = Schemas["FitRequest"];
export type FitResponse = Schemas["FitResponse"];
export type CalibrationPageRequest = Schemas["PageRequest"];
export type Capabilities = Schemas["Capabilities"];
export type CalibrationScale = Schemas["CalibrationScale"];

const encode = encodeURIComponent;
const trayPath = (printerId: string, tray: string) => `/api/v1/printers/${encode(printerId)}/calibration/${encode(tray)}`;

/** The mini scale of the printed page, for the figure on the page. Static, so it is fetched once; the figure waits for it. */
export const useCalibrationScale = () =>
  useQuery({
    queryKey: ["calibration-scale"],
    queryFn: () => apiFetch<CalibrationScale>("/api/v1/printers/calibration/scale"),
    staleTime: Infinity,
  });

/** One calibration per tray of the printer. Nested under `["printers", id]`, so a printer event refreshes it. */
export const useCalibration = (printerId: string) =>
  useQuery({
    queryKey: calibrationKey(printerId),
    queryFn: () => apiFetch<CalibrationRead[]>(`/api/v1/printers/${encode(printerId)}/calibration`),
  });

export function useSaveCalibration(printerId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ tray, values }: { tray: string; values: CalibrationPut }) =>
      apiFetch<CalibrationRead>(trayPath(printerId, tray), { method: "PUT", json: values }),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: calibrationKey(printerId) });
      void client.invalidateQueries({ queryKey: capabilitiesKey(printerId) });
    },
  });
}

/** Back to no correction for one tray (the stored values are deleted; no calibration means identity). */
export function useResetCalibration(printerId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ tray }: { tray: string }) => apiFetch<null>(trayPath(printerId, tray), { method: "DELETE" }),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: calibrationKey(printerId) });
      void client.invalidateQueries({ queryKey: capabilitiesKey(printerId) });
    },
  });
}

/** Turns measured readings into correction values; nothing is stored until they are saved. */
export const useFit = (printerId: string) =>
  useMutation({
    mutationFn: ({ tray, request }: { tray: string; request: FitRequest }) =>
      apiFetch<FitResponse>(`${trayPath(printerId, tray)}/fit`, { method: "POST", json: request }),
  });

/** Prints the calibration page (`{job_id}` back). */
export function usePrintCalibrationPage(printerId: string) {
  const client = useQueryClient();
  // The print is registered "claim pending" (the page's status line is about to claim it, so the stack does not
  // announce it twice). That only holds while this page is there: after it went away, nobody will claim it.
  const mounted = useRef(false);
  const registered = useRef<string[]>([]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      registered.current.forEach(releaseClaimPending);
      registered.current = [];
    };
  }, []);
  return useMutation({
    mutationFn: ({ tray, request }: { tray: string; request: CalibrationPageRequest }) =>
      apiFetch<Schemas["PageJobAccepted"]>(`${trayPath(printerId, tray)}/page`, { method: "POST", json: request }),
    onSuccess: (accepted) => {
      registered.current.push(accepted.job_id);
      void watchPrint({ kind: "calibration", printerId, jobIds: [accepted.job_id], claimPending: mounted.current });
    },
    onSettled: () => {
      void client.invalidateQueries({ queryKey: ["jobs"] });
      // The status names the tray of the newest calibration page (#344).
      void client.invalidateQueries({ queryKey: statusKey(printerId) });
      // A check print took a pass: the next dialog offers the one after it.
      void client.invalidateQueries({ queryKey: [...calibrationKey(printerId), "check-sheet"] });
      // Every print is a run: the readings' print picker offers it (and picks it by default) at once.
      void client.invalidateQueries({ queryKey: [...calibrationKey(printerId), "runs"] });
    },
  });
}

/** The address of the calibration page as a PDF (a download, so a plain link, not a fetch). */
export function calibrationPdfUrl(
  printerId: string,
  tray: string,
  options: { type: string; version?: number | null; corrected: boolean },
): string {
  return `${trayPath(printerId, tray)}/page/pdf${queryString({
    type: options.type,
    version: options.version,
    corrected: options.corrected,
  })}`;
}

export const useCapabilities = (printerId: string) =>
  useQuery({
    queryKey: capabilitiesKey(printerId),
    queryFn: () => apiFetch<Capabilities>(`/api/v1/printers/${encode(printerId)}/capabilities`),
    // A screen that has not chosen a printer yet passes "" and asks for nothing.
    enabled: printerId !== "",
  });

export type CalibrationHistoryEntry = Schemas["CalibrationHistoryRead"];
export type CalibrationRun = Schemas["CalibrationRunRead"];

/** Entries per page of the history; the hub keeps the newest 500 per printer and tray. */
export const HISTORY_PAGE = 50;
export const HISTORY_KEPT = 500;

// Under `calibrationKey`, so a save, a reset and a printer event refresh them too.
const historyKey = (printerId: string, tray: string) => [...calibrationKey(printerId), "history", tray] as const;
const runsKey = (printerId: string, tray: string) => [...calibrationKey(printerId), "runs", tray] as const;

/** The saved corrections of a tray, newest first, a page at a time. */
export const useCalibrationHistory = (printerId: string, tray: string) =>
  useInfiniteQuery({
    queryKey: historyKey(printerId, tray),
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      apiFetch<CalibrationHistoryEntry[]>(`${trayPath(printerId, tray)}/history${queryString({ limit: HISTORY_PAGE, offset: pageParam })}`),
    // A full page may have a next one; an empty next page ends the list.
    getNextPageParam: (last, pages) => (last.length === HISTORY_PAGE ? pages.length * HISTORY_PAGE : undefined),
  });

/** Calibration, check and probe pages printed on a tray, newest first (with the retries of their jobs). */
export const useCalibrationRuns = (printerId: string, tray: string) =>
  useQuery({
    queryKey: runsKey(printerId, tray),
    queryFn: () => apiFetch<CalibrationRun[]>(`${trayPath(printerId, tray)}/runs`),
  });

/** Uses an earlier entry's values again: a new `revert` entry, nothing is deleted. */
export function useRestoreCalibration(printerId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ tray, entryId, note }: { tray: string; entryId: string; note: string }) =>
      apiFetch<CalibrationRead>(`${trayPath(printerId, tray)}/history/${encode(entryId)}/restore`, {
        method: "POST",
        json: note.trim() ? { note: note.trim() } : {},
      }),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: calibrationKey(printerId) });
      void client.invalidateQueries({ queryKey: capabilitiesKey(printerId) });
    },
  });
}

export type PageLayoutSummary = Schemas["PageLayoutSummary"];
export type CheckSheetOpen = Schemas["CheckSheetOpen"];
export type CheckSheetMode = NonNullable<CalibrationPageRequest["check_sheet"]>;

/** How a calibration or check page fits this printer and tray: page codes, margin text and run-line slots (nothing is printed). */
export const usePageLayout = (
  printerId: string,
  tray: string,
  type: { key: string; version: number } | null,
  kind: "calibration_page" | "check_page",
  enabled = true,
  /** A check page as pass 1 of a check sheet: the summary counts the sheet's passes and their run-line slots. */
  checkSheet?: CheckSheetMode,
) =>
  useQuery({
    queryKey: [...calibrationKey(printerId), "layout", tray, type?.key, type?.version, kind, checkSheet] as const,
    queryFn: () =>
      apiFetch<PageLayoutSummary>(
        `${trayPath(printerId, tray)}/page/layout${queryString({ type: type!.key, version: type!.version, kind, check_sheet: checkSheet })}`,
      ),
    enabled: enabled && type !== null,
  });

/** The newest check sheet of this printer, tray and type version that has a free pass; `null` when there is none. */
export const useOpenCheckSheet = (printerId: string, tray: string, type: { key: string; version: number } | null, enabled = true) =>
  useQuery({
    queryKey: [...calibrationKey(printerId), "check-sheet", tray, type?.key, type?.version] as const,
    queryFn: () =>
      apiFetch<CheckSheetOpen | null>(`${trayPath(printerId, tray)}/check-sheets/open${queryString({ type: type!.key, version: type!.version })}`),
    enabled: enabled && type !== null,
  });
