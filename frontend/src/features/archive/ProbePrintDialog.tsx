// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { useMemo, useState, type MouseEvent, type ReactNode } from "react";
import { Button, Checkbox } from "react-aria-components";
import { apiFetch, ApiError } from "../../api/client";
import { useSession } from "../../app/session";
import { InfoIcon } from "../../shell/icons";
import { PRIMARY_BUTTON } from "../../shell/SignIn";
import { probePdfUrl, useCalibration, useCapabilities, usePrintProbePage, type CalibrationRead } from "../calibration/api";
import { useCalibrationErrorText } from "../calibration/errors";
import { usePrinters, usePrinterStatus, type Printer, type PrinterStatus } from "../printers/api";
import { HINT, ModalDialog, SelectField } from "../settings/controls";
import { SECONDARY_BUTTON } from "../ui";

interface TypeRef {
  key: string;
  version: number;
}

interface ProbePrintDialogProps {
  type: TypeRef;
  isOpen: boolean;
  onClose: () => void;
  /** Opens with the measurement sheet chosen (the "Measurement sheet" action). */
  grid?: boolean;
  /** The printer and tray to start with (the calibration page passes its own); else the first page printer and its default tray. */
  printerId?: string;
  tray?: string;
}

/**
 * The probe print on plain paper (#241, design 7.3, mockup M9): every label outlined with its number, held against a
 * real sheet to see whether the labels, the preprinted areas and the sheet code line up. No sheet is used, no cell is
 * reserved; the job goes through the queue like any page. With `grid` it is the measurement sheet: the same page plus a
 * millimetre grid, drawn without correction, to photograph on a real sheet.
 */
export function ProbePrintDialog({ isOpen, onClose, ...rest }: ProbePrintDialogProps) {
  const { t } = useLingui();
  return (
    <ModalDialog title={t`Probe print on plain paper`} isOpen={isOpen} onClose={onClose}>
      {isOpen ? <Body onClose={onClose} {...rest} /> : null}
    </ModalDialog>
  );
}

/**
 * The name the server gives the PDF in its Content-Disposition (`download_probe_page`): `probe-<key>-v<n>.pdf` or
 * `measurement-sheet-<key>-v<n>.pdf`, anything but letters, digits, `.`, `_` and `-` replaced by `_`. A plain click
 * and a middle click then save the same file.
 */
export const probeFileName = (type: TypeRef, grid: boolean): string =>
  `${`${grid ? "measurement-sheet" : "probe"}-${type.key}`.replace(/[^A-Za-z0-9._-]/g, "_")}-v${type.version}.pdf`;

/** A printer whose fresh status says it does not answer. A stale or missing reading never counts (design 7.3). */
const freshlyOffline = (status: PrinterStatus | undefined): boolean => !!status && status.polled !== false && !status.stale && status.online === false;

function Body({ type, onClose, grid: initialGrid = false, printerId: initialPrinter, tray: initialTray }: Omit<ProbePrintDialogProps, "isOpen">) {
  const { t, i18n } = useLingui();
  const session = useSession();
  const canPrint = session.scope === "print" || session.scope === "admin";
  const printers = usePrinters();
  const pagePrinters = useMemo(() => (printers.data ?? []).filter((p) => p.backend === "ipp" && p.enabled), [printers.data]);
  const [chosenPrinter, setChosenPrinter] = useState<string | undefined>(initialPrinter);
  const printer: Printer | undefined = pagePrinters.find((p) => p.id === chosenPrinter) ?? pagePrinters[0];
  const printerId = printer?.id ?? "";
  const calibration = useCalibration(printerId);
  const capabilities = useCapabilities(printerId);
  const status = usePrinterStatus(printerId, printerId !== "");
  const trays = useMemo(() => {
    const labels = new Map((capabilities.data?.trays ?? []).map((x) => [x.key, x.label]));
    return (calibration.data ?? []).map((row) => ({ key: row.tray, label: labels.get(row.tray) ?? row.tray, row }));
  }, [calibration.data, capabilities.data]);
  const [chosenTray, setChosenTray] = useState<string | undefined>(initialTray);
  const defaultTray = capabilities.data?.trays?.find((x) => x.is_default)?.key;
  const tray = trays.find((x) => x.key === chosenTray) ?? trays.find((x) => x.key === defaultTray) ?? trays[0];
  const [grid, setGrid] = useState(initialGrid);
  const print = usePrintProbePage();
  const download = useProbeDownload();
  const errorText = useProbeErrorText();
  const correctionWords = useCorrectionWords();
  const offline = freshlyOffline(status.data);
  // A swapped device refuses new probe pages (409 printer-identity-mismatch): the button waits with the note.
  const held = status.data?.identity_mismatch === true;
  const loading = printers.isPending;
  // The capabilities name the default tray: until they answer, the first row would be a guess (rows come by tray key).
  const traysLoading = printer !== undefined && (calibration.isPending || capabilities.isPending);

  // A choice starts with a clean line: the last answer was about the printer or tray chosen before.
  const clear = () => {
    print.reset();
    download.reset();
  };
  const pdfUrl = printer && tray && !traysLoading ? probePdfUrl(printer.id, tray.key, { type: type.key, version: type.version, grid }) : null;
  const refusedOffline = print.error instanceof ApiError && print.error.code === "printer-offline";
  const printError = print.isError && !refusedOffline ? errorText(print.error, t`The page could not be queued.`) : null;
  const downloadError = download.error ? errorText(download.error, t`The PDF could not be made.`) : null;

  const send = () => {
    if (!printer || !tray || print.isPending) return;
    print.mutate({ printerId: printer.id, tray: tray.key, request: { type: type.key, version: type.version, grid } }, { onSuccess: onClose });
  };

  if (loading) return <p role="status" className={`${HINT} m-0`}>{t`Loading…`}</p>;
  if (!printer) return <p className="m-0">{t`There is no page printer yet. Add one under Settings › Printers.`}</p>;

  return (
    <>
      <p className={`${HINT} m-0 [overflow-wrap:anywhere]`}>
        {t`Prints the outline of every label so you can hold the page against a real sheet. The archive uses no sheet and reserves no cell.`}
      </p>
      <SelectField
        label={t`Printer`}
        value={printer.id}
        options={pagePrinters.map((p) => ({ id: p.id, label: p.name }))}
        onChange={(id) => {
          clear();
          setChosenPrinter(id);
          setChosenTray(undefined);
        }}
      />
      <div className="flex min-w-0 flex-col gap-1.5">
        {traysLoading ? (
          <p role="status" className={`${HINT} m-0`}>{t`Loading…`}</p>
        ) : trays.length > 1 && tray ? (
          <SelectField
            label={t`Tray`}
            value={tray.key}
            options={trays.map((x) => ({ id: x.key, label: x.label }))}
            onChange={(key) => {
              clear();
              setChosenTray(key);
            }}
          />
        ) : calibration.isError ? (
          <p role="alert" className="m-0 text-bad [overflow-wrap:anywhere]">
            {errorText(calibration.error, t`The trays of this printer could not be loaded.`)}
          </p>
        ) : (
          <p className="m-0">
            <span className="text-small font-bold">{t`Tray`}</span> <span>{tray ? tray.label : t`This printer has no tray to print from.`}</span>
          </p>
        )}
        {tray && !traysLoading ? <p className={`${HINT} m-0`}>{grid
              ? t`Print it on a real sheet of this type and photograph it to read where the vendor's preprint sits. The grid is drawn without correction, so its millimetres are the page's own.`
              : correctionWords(tray.row)}</p> : null}
      </div>
      <Checkbox
        isSelected={grid}
        onChange={(value) => {
          download.reset();
          setGrid(value);
        }}
        className="group flex min-h-11 items-start gap-3 rounded-r1 py-1 data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link"
      >
        <span
          aria-hidden="true"
          className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-r1 border border-line-strong bg-sunken transition-colors duration-150 group-data-[selected]:border-accent group-data-[selected]:bg-accent"
        >
          <svg viewBox="0 0 12 12" width="12" height="12" className="fill-none stroke-accent-ink stroke-2 opacity-0 group-data-[selected]:opacity-100">
            <path d="M2.5 6.5 5 9l4.5-5.5" />
          </svg>
        </span>
        <span className="[overflow-wrap:anywhere]">{t`Measurement sheet: add a millimetre grid, no correction`}</span>
      </Checkbox>
      {/* Polite: a printer chosen while it is offline disables Print, and the reason is read out with it. */}
      <div aria-live="polite" className="flex min-w-0 flex-col">
      {offline || refusedOffline ? (
        <Note tone="bad" role={refusedOffline ? "alert" : "note"} title={t`${printer.name} is offline`}>
          {status.data?.captured_at
            ? t`Its status was read at ${timeOf(status.data.captured_at, i18n.locale)} and the printer did not answer. Switch it on, or choose another printer.`
            : t`Its last status says it does not answer. Switch it on, or choose another printer.`}
        </Note>
      ) : held ? (
        <Note tone="warn" title={t`Held while the device is swapped`}>
          {grid
            ? t`The hub sees a different device behind this printer. The measurement sheet uses no correction, but it waits too until an admin confirms the device on the printer's page: the hub prints no page for a device nobody has confirmed.`
            : t`The hub sees a different device behind this printer. Like a calibration page, a probe page is not printed until an admin confirms the device on the printer's page: the stored correction would be wrong for it.`}
        </Note>
      ) : (
        <Note tone="info">{t`It goes through the print queue: you can see it under Jobs and retry it.`}</Note>
      )}
      </div>
      {printError ? <p role="alert" className="m-0 text-bad [overflow-wrap:anywhere]">{printError}</p> : null}
      {downloadError ? <p role="alert" className="m-0 text-bad [overflow-wrap:anywhere]">{downloadError}</p> : null}
      {!canPrint ? <p className={`${HINT} m-0`}>{t`Printing needs a print or admin key.`}</p> : null}
      {/* The buttons stay at the bottom of the visible dialog, also with the on-screen keyboard open (spec 12). */}
      <div className="sticky -bottom-6 -mx-6 -mb-6 grid grid-cols-[auto_1fr] gap-3 border-t border-line bg-surface px-6 py-4 sm:flex sm:justify-end">
        {pdfUrl ? (
          <a
            href={pdfUrl}
            download
            aria-disabled={download.isPending || undefined}
            onClick={(event) => download.start(event, pdfUrl, probeFileName(type, grid))}
            className={`${SECONDARY_BUTTON} no-underline aria-disabled:opacity-60`}
          >
            {download.isPending ? t`Downloading…` : t`Download PDF`}
          </a>
        ) : null}
        <Button isDisabled={!canPrint || !tray || traysLoading || offline || held || print.isPending} onPress={send} className={PRIMARY_BUTTON}>
          {t`Print probe page`}
        </Button>
      </div>
    </>
  );
}

/** "dx +0.41 mm, dy −0.66 mm": the offsets the tray's correction moves the page by, with a real minus sign. */
function useSigned(): (n: number) => string {
  const { i18n } = useLingui();
  return (n) =>
    new Intl.NumberFormat(i18n.locale, { maximumFractionDigits: 2, minimumFractionDigits: 2, signDisplay: "always", useGrouping: false })
      .format(n)
      .replace("-", "−");
}

/** What the probe page does with the tray's correction, in one sentence. */
function useCorrectionWords(): (row: CalibrationRead) => string {
  const { t } = useLingui();
  const signed = useSigned();
  return (row) => {
    const stored = [row.offset_x_mm, row.offset_y_mm, row.scale_x, row.scale_y, row.rotation_deg].some((v) => v != null);
    if (!stored || row.method === "none") return t`This tray has no correction stored: the page is printed as it comes.`;
    const dx = signed(row.offset_x_mm ?? 0);
    const dy = signed(row.offset_y_mm ?? 0);
    return t`The current correction of this tray is applied: dx ${dx} mm, dy ${dy} mm.`;
  };
}

/** The words for a refused probe print or download; the offline refusal has its own note in the dialog. */
function useProbeErrorText(): (error: unknown, fallback: string) => string {
  const { t } = useLingui();
  const calibrationText = useCalibrationErrorText();
  return (error, fallback) => {
    if (error instanceof ApiError) {
      switch (error.code) {
        case "render-timeout":
          return t`The page took too long to draw. Try again later.`;
        case "render-unavailable":
          return t`The hub cannot draw the page right now. Try again later.`;
        case "too-many-previews":
          return t`Too many pages are being drawn right now. Try again in a moment.`;
        case "calibration_grid_too_large":
          return t`This sheet type has too many labels for a probe page.`;
      }
    }
    return calibrationText(error, fallback);
  };
}

/**
 * The PDF is fetched before it is handed to the browser, so a refusal (a page that takes too long to draw, a key
 * that may not use the printer) is said in words instead of a broken download. The link keeps its address for a
 * second click, a middle click and a copied link.
 */
function useProbeDownload() {
  const [state, setState] = useState<{ pending: boolean; error: unknown }>({ pending: false, error: null });
  return {
    isPending: state.pending,
    error: state.error,
    reset: () => setState({ pending: false, error: null }),
    start: (event: MouseEvent<HTMLAnchorElement>, url: string, fileName: string) => {
      // A modified click (new tab, save as) is the browser's: it gets the plain address.
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      if (state.pending) return;
      setState({ pending: true, error: null });
      apiFetch<Blob>(url, { as: "blob", headers: { Accept: "application/pdf" } }).then(
        (blob) => {
          const href = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = href;
          link.download = fileName;
          link.click();
          // The browser has taken the file once the click ran; the address is freed after it.
          setTimeout(() => URL.revokeObjectURL(href), 0);
          setState({ pending: false, error: null });
        },
        (error: unknown) => setState({ pending: false, error }),
      );
    },
  };
}

function timeOf(iso: string, locale: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
}

function Note({ tone, title, role = "note", children }: { tone: "info" | "warn" | "bad"; title?: string; role?: "note" | "alert"; children: ReactNode }) {
  const look = {
    info: "bg-info-soft",
    warn: "border border-warn bg-accent-soft",
    bad: "border border-bad bg-sunken",
  }[tone];
  const icon = { info: "stroke-link", warn: "stroke-warn", bad: "stroke-bad" }[tone];
  return (
    <div role={role} className={`flex gap-3 rounded-r2 p-3 text-small text-ink ${look}`}>
      <InfoIcon width="18" height="18" className={`mt-0.5 shrink-0 ${icon}`} />
      <div className="flex min-w-0 flex-col gap-0.5 [overflow-wrap:anywhere]">
        {title ? <strong>{title}</strong> : null}
        <span>{children}</span>
      </div>
    </div>
  );
}
