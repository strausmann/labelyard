// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import { useId, useState } from "react";
import { Button } from "react-aria-components";
import { FIELD } from "../../shell/SignIn";
import { typeLabel, typeLabelWithKey } from "../sheets/typeLabel";
import { LABEL, ModalDialog } from "../settings/controls";
import { SECONDARY_BUTTON } from "../ui";
import { useDeleteUnused, useSheetTypes, type SheetTypeRead } from "./api";
import { useArchiveErrorText } from "./errors";

const DANGER_BUTTON =
  "press inline-flex h-11 items-center justify-center gap-2 rounded-r2 border border-bad bg-surface px-4 text-small font-bold text-bad transition-colors duration-150 hover:bg-sunken data-[disabled]:opacity-60 data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link";

const matches = (type: SheetTypeRead, filter: string) => {
  const needle = filter.trim().toLowerCase();
  if (!needle) return true;
  return [typeLabelWithKey(type), type.description ?? ""].some((s) => s.toLowerCase().includes(needle));
};

function Body({ onClose, onRemoved }: { onClose: () => void; onRemoved: (keys: string[]) => void }) {
  const { t } = useLingui();
  const errorText = useArchiveErrorText();
  // Exactly the types in state unused, archived ones included: none is chosen until the person ticks it.
  const list = useSheetTypes({ usage_state: "unused", include_archived: true });
  const remove = useDeleteUnused();
  const [filter, setFilter] = useState("");
  const [chosen, setChosen] = useState<ReadonlySet<string>>(new Set());
  const filterId = useId();
  const types = list.data ?? [];
  const shown = types.filter((type) => matches(type, filter));
  const keys = types.map((type) => type.key).filter((key) => chosen.has(key));
  // A chosen type the filter hides stays chosen: the line below says so, so nothing is removed unseen.
  const chosenCount = keys.length;
  const hiddenChosen = keys.filter((key) => !shown.some((type) => type.key === key)).length;
  const originWord = { seed: null, imported: t`imported`, custom: t`custom` };
  // The short name (vendor and code); a type without them is named by its description, then its key.
  const nameOf = (type: SheetTypeRead) => {
    const label = typeLabel(type);
    const base = label === type.key && type.description?.trim() ? type.description.trim() : label;
    const word = originWord[type.origin];
    return word ? `${base} (${word})` : base;
  };

  const toggle = (key: string) =>
    setChosen((current) => {
      const next = new Set(current);
      if (!next.delete(key)) next.add(key);
      return next;
    });

  return (
    <>
      <p className="-mt-2 text-small text-ink-2">
        {list.data
          ? plural(types.length, {
              one: "This type has no sheets. Nothing is selected: tick the ones to remove.",
              other: "These # types have no sheets. Nothing is selected: tick the ones to remove.",
            })
          : null}
      </p>
      {list.isPending ? <p className="text-ink-2">{t`Loading…`}</p> : null}
      {list.isError ? <p role="alert">{t`The sheet types could not be loaded.`}</p> : null}
      {list.data && types.length === 0 ? <p>{t`Every installed type has sheets. There is nothing to remove.`}</p> : null}
      {types.length > 0 ? (
        <div className="flex flex-col gap-1.5">
          <label htmlFor={filterId} className={LABEL}>{t`Filter`}</label>
          <input
            id={filterId}
            type="search"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder={t`Filter by name`}
            spellCheck={false}
            className={FIELD}
          />
        </div>
      ) : null}
      {shown.length > 0 ? (
        <ul aria-label={t`Unused types`} className="m-0 flex list-none flex-col gap-2 p-0">
          {shown.map((type) => (
            <li key={type.key}>
              <label className="grid min-h-11 cursor-pointer grid-cols-[auto_minmax(0,1fr)] items-center gap-x-3 rounded-r2 border border-line p-3 has-[:checked]:border-accent has-[:focus-visible]:outline-3 has-[:focus-visible]:outline-link">
                <input type="checkbox" checked={chosen.has(type.key)} onChange={() => toggle(type.key)} className="row-span-2 size-5 shrink-0 accent-accent" />
                <span className="font-bold [overflow-wrap:anywhere]">{nameOf(type)}</span>
                <span className="text-small text-ink-2 [overflow-wrap:anywhere]">
                  {t`never used`} · {type.geometry.page_size} · {type.geometry.cols} × {type.geometry.rows} · <span className="font-mono">{type.key}</span>
                </span>
              </label>
            </li>
          ))}
        </ul>
      ) : null}
      {types.length > 0 && shown.length === 0 ? <p className="text-ink-2">{t`No unused type matches the filter.`}</p> : null}
      {hiddenChosen > 0 ? (
        <p role="status" className="m-0 text-small font-bold">
          {t`${chosenCount} chosen, ${hiddenChosen} of them hidden by the filter.`}
        </p>
      ) : null}
      <p className="m-0 rounded-r2 bg-info-soft p-3 text-small">
        {t`Sheet codes are never issued twice: the counter of a prefix stays, so a type you add back continues its numbering. A linked backing design is kept.`}
      </p>
      {remove.error ? <p role="alert">{errorText(remove.error)}</p> : null}
      {/* The buttons stay at the bottom of the visible dialog while the list scrolls (keyboard open included). */}
      <div className="sticky -bottom-6 -mx-6 -mb-6 flex flex-wrap justify-end gap-3 border-t border-line bg-surface px-6 py-4">
        <Button onPress={onClose} className={SECONDARY_BUTTON}>{t`Cancel`}</Button>
        <Button
          isDisabled={keys.length === 0 || remove.isPending}
          onPress={() =>
            remove.mutate(keys, {
              onSuccess: (result) => {
                onRemoved(result.deleted);
                onClose();
              },
            })
          }
          className={DANGER_BUTTON}
        >
          <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" className="fill-none stroke-current stroke-[1.5]">
            <path d="M2 3h8M4.5 3V1.8h3V3M3 3l.5 7.5h5L9 3" />
          </svg>
          {plural(keys.length, { one: "Remove # type", other: "Remove # types" })}
        </Button>
      </div>
    </>
  );
}

/** "Remove unused types" (#335, mockup M5): exactly the types in state unused, none preselected, all or nothing. */
export function RemoveUnusedDialog({
  isOpen,
  onClose,
  onRemoved = () => {},
}: {
  isOpen: boolean;
  onClose: () => void;
  /** The keys the server deleted. */
  onRemoved?: (keys: string[]) => void;
}) {
  const { t } = useLingui();
  return (
    <ModalDialog title={t`Remove unused types`} isOpen={isOpen} onClose={onClose}>
      {isOpen ? <Body onClose={onClose} onRemoved={onRemoved} /> : null}
    </ModalDialog>
  );
}
