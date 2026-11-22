// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import type { ReactNode } from "react";
import { Button } from "react-aria-components";
import { StatusBadge } from "../../shell/StatusBadge";
import { SECONDARY_BUTTON } from "../ui";
import type { SettingView } from "./api";
import { HINT, LABEL, SelectField, SwitchField, TextInput } from "./controls";
import type { DraftValue } from "./draft";

interface Props {
  view: SettingView;
  label: string;
  /** What the form currently holds; a secret's value is only what was typed in this session. */
  value: DraftValue;
  onChange: (value: DraftValue) => void;
  description?: string;
  /** The secret must be entered again in this save (it is bound to the URL it was saved with). */
  required?: boolean;
  /** Labels for enum choices, by value. */
  choiceLabels?: Record<string, string>;
  mono?: boolean;
  /** A refusal shown under the field; marks it invalid. Only used by text-like fields. */
  error?: string | null;
  /** The environment variable that sets this value; a locked field names it ("Set by LABELYARD_X"). */
  envName?: string;
}

/**
 * One setting as a form control. Locked values (D4) are shown read-only with the reason;
 * secrets (D5) are write-only: the field starts empty and says whether a value is saved.
 */
export function SettingField({ view, label, value, onChange, description, required, choiceLabels, mono, error, envName }: Props) {
  const { t } = useLingui();
  const locked = view.locked;
  const lockedHint = t`Set by the container`;

  let warning: string | null = null;
  if (view.state === "undecryptable") warning = t`Can't be read with the current secret key — enter it again`;
  if (view.state === "env_secret_needs_env_url") {
    warning = t`The secret comes from the container, but the URL does not. Set the URL in the container too.`;
  }
  const badge = warning ? <StatusBadge state="warn" label={warning} /> : null;

  if (view.kind === "secret") {
    if (locked) {
      return (
        <div className="flex flex-col gap-1.5">
          <span className={LABEL}>{label}</span>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
            <StatusBadge state={view.is_set ? "ok" : "idle"} label={view.is_set ? t`Set` : t`Not set`} />
            <span className={HINT}>{lockedHint}</span>
          </div>
          {badge}
        </div>
      );
    }
    const typed = typeof value === "string" ? value : "";
    const removing = value === null;
    const placeholder =
      view.state === "undecryptable" ? t`Enter it again` : view.is_set ? t`Saved — enter a new value to replace` : t`Not set`;
    const reason = required ? t`This secret is saved for the old URL. Enter it again to use the new URL.` : description;
    return (
      <div className="flex flex-col gap-2">
        <TextInput
          label={label}
          value={typed}
          onChange={onChange}
          type="password"
          autoComplete="new-password"
          placeholder={removing ? t`Will be removed when you save` : placeholder}
          description={reason}
          required={required}
          invalid={required && typed === ""}
          mono={mono}
        />
        {badge}
        {view.is_set && !removing && view.state === "ok" ? (
          <Button onPress={() => onChange(null)} className={`${SECONDARY_BUTTON} self-start`}>
            {t`Remove saved value`}
          </Button>
        ) : null}
        {removing ? (
          <Button onPress={() => onChange("")} className={`${SECONDARY_BUTTON} self-start`}>
            {t`Keep saved value`}
          </Button>
        ) : null}
      </div>
    );
  }

  if (view.kind === "bool") {
    return (
      <SwitchField
        label={label}
        isSelected={Boolean(value)}
        onChange={onChange}
        isDisabled={locked}
        description={locked ? lockedHint : description}
      />
    );
  }

  if (view.kind === "enum" && !locked) {
    return (
      <SelectField
        label={label}
        value={String(value)}
        options={view.choices?.map((c) => ({ id: c, label: choiceLabels?.[c] ?? c })) ?? []}
        onChange={onChange}
      />
    );
  }

  const text = String(value ?? "");
  const numeric = view.kind === "int" || view.kind === "float";
  // A locked field says what sets it; its explanation stays next to that note, it does not give way to it.
  const lockNote = envName ? <LockLine text={t`Set by ${envName}`} /> : lockedHint;
  const note = locked ? (description ? <span className="flex flex-col gap-1">{lockNote}<span>{description}</span></span> : lockNote) : description;
  return (
    <TextInput
      label={label}
      value={locked && view.kind === "enum" ? (choiceLabels?.[text] ?? text) : text}
      onChange={locked ? undefined : (v) => onChange(v)}
      type={view.kind === "url" ? "url" : "text"}
      readOnly={locked}
      description={error ? <ErrorLine text={error} extra={note} /> : note}
      invalid={Boolean(error)}
      inputMode={numeric ? (view.kind === "int" ? "numeric" : "decimal") : view.kind === "url" ? "url" : undefined}
      mono={mono ?? view.kind === "url"}
    />
  );
}

/** The lock note of a field the container sets: a small padlock and the variable's name. */
function LockLine({ text }: { text: string }) {
  return (
    <span className="flex items-center gap-2">
      <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false" className="shrink-0 fill-none stroke-current" strokeWidth="1.6">
        <rect x="3" y="7" width="10" height="7" rx="1.5" />
        <path d="M5 7V5a3 3 0 0 1 6 0v2" />
      </svg>
      <span>{text}</span>
    </span>
  );
}

/** The refusal first, in the error colour with an icon and words, not colour alone; the explanation stays below it. */
function ErrorLine({ text, extra }: { text: string; extra?: ReactNode }) {
  return (
    <span className="flex flex-col gap-1">
      <span className="flex items-start gap-2 text-bad">
        <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false" className="mt-0.5 shrink-0 fill-none stroke-current" strokeWidth="1.6">
          <circle cx="8" cy="8" r="6.5" />
          <path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4" />
        </svg>
        <span>{text}</span>
      </span>
      {extra ? <span>{extra}</span> : null}
    </span>
  );
}
