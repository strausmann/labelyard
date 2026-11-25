// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { Button } from "react-aria-components";
import { exportTypeUrl, useRestoreVersion, useTypeVersions, type SheetTypeDetail, type VersionHistoryRead } from "./api";
import { useArchiveErrorText } from "./errors";
import { useGeometryRows } from "./geometryRows";

const SMALL_BUTTON =
  "press inline-flex min-h-11 items-center justify-center gap-2 rounded-r2 border border-line-strong bg-surface px-3 text-small font-bold text-ink transition-colors duration-150 hover:bg-sunken data-[disabled]:opacity-60 data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link";

/** The detail's versions until the version list has answered: the same rows, without what changed. */
const fromDetail = (type: SheetTypeDetail): VersionHistoryRead[] =>
  [...type.versions]
    .sort((a, b) => b.version - a.version)
    .map((v) => ({ ...v, created_by: null, changed_fields: {}, restorable: false }));

/**
 * The versions of a type, newest first (#336, spec 7.5): what changed against the version before (old → new), who and
 * when, the change note, and "Restore as new version" for an older version while no sheet uses a newer one.
 */
export function TypeVersions({ type, admin }: { type: SheetTypeDetail; admin: boolean }) {
  const { t, i18n } = useLingui();
  const list = useTypeVersions(type.key);
  const restore = useRestoreVersion();
  const errorText = useArchiveErrorText();
  const rows = useGeometryRows();
  const versions = list.data ?? fromDetail(type);
  const newest = Math.max(...versions.map((v) => v.version));
  const date = (iso: string | null) => (iso ? new Date(iso).toLocaleString(i18n.locale, { dateStyle: "medium", timeStyle: "short" }) : "");
  const number = (value: unknown) => (typeof value === "number" ? value.toLocaleString(i18n.locale, { maximumFractionDigits: 3 }) : String(value ?? "—"));
  const who = (by: string | null) => (by === null ? null : by.startsWith("system:") ? t`from the catalog` : by);
  const changes = (v: VersionHistoryRead) =>
    rows
      .filter((row) => v.changed_fields[row.field])
      .map((row) => {
        const change = v.changed_fields[row.field]!;
        const unit = row.mm ? " mm" : "";
        return { field: row.field, text: `${row.label}: ${number(change.old)} → ${number(change.new)}${unit}` };
      });
  // A sheet pinned to a newer version keeps an older one from being restored; the server says so with `restorable`.
  const newerInUse = (v: VersionHistoryRead) => versions.some((other) => other.version > v.version && other.sheets > 0);

  return (
    <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
      <table aria-label={t`Versions`} className="w-full min-w-0 border-collapse text-left">
        <thead>
          <tr className="text-small text-ink-2">
            <th scope="col" className="py-2 pr-3 font-bold">{t`Version`}</th>
            <th scope="col" className="py-2 pr-3 font-bold">{t`What changed`}</th>
            <th scope="col" className="py-2 text-right font-bold">{t`Sheets`}</th>
          </tr>
        </thead>
        <tbody>
          {versions.map((v) => {
            const pending = restore.isPending && restore.variables?.version === v.version;
            const failed = restore.isError && restore.variables?.version === v.version;
            const by = who(v.created_by);
            return (
              <tr key={v.version} className="border-t border-line align-top">
                <th scope="row" className="py-3 pr-3 whitespace-nowrap">
                  {v.version}
                  {v.version === type.current_version ? <span className="ml-2 text-small font-normal text-ink-2">{t`current`}</span> : null}
                </th>
                <td className="min-w-0 py-3 pr-3 [overflow-wrap:anywhere]">
                  <div className="flex flex-col gap-1">
                    <span>{v.change_note ?? <span className="text-ink-2">{t`first version`}</span>}</span>
                    {changes(v).length > 0 ? (
                      <ul aria-label={t`Changed measurements`} className="m-0 flex list-none flex-col p-0 font-mono text-small">
                        {changes(v).map((c) => (
                          <li key={c.field}>{c.text}</li>
                        ))}
                      </ul>
                    ) : null}
                    <span className="text-small text-ink-2">{[date(v.created_at), by].filter(Boolean).join(" · ")}</span>
                    <div className="flex flex-wrap items-center gap-x-4">
                      <a href={exportTypeUrl(type.key, v.version)} download className="inline-flex min-h-11 items-center text-small text-link underline underline-offset-2">
                        {t`Export version ${v.version}`}
                      </a>
                      {admin && v.restorable ? (
                        <Button
                          isDisabled={restore.isPending}
                          onPress={() => restore.mutate({ key: type.key, version: v.version })}
                          aria-label={t`Restore as new version (version ${v.version})`}
                          className={SMALL_BUTTON}
                        >
                          {pending ? t`Restoring…` : t`Restore as new version`}
                        </Button>
                      ) : null}
                    </div>
                    {admin && !v.restorable && v.version < newest && newerInUse(v) ? (
                      <span className="text-small text-ink-2">{t`A sheet uses a newer version, so this one can only be shown.`}</span>
                    ) : null}
                    {failed ? (
                      <p role="alert" className="m-0 text-small text-bad">
                        {errorText(restore.error, t`The version could not be restored.`)}
                      </p>
                    ) : null}
                  </div>
                </td>
                <td className="py-3 text-right tabular-nums">{v.sheets}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
