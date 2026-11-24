// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import { useId } from "react";
import { Button, Group, Input, Label, Radio, RadioGroup, Text, TextField } from "react-aria-components";
import { HINT, LABEL } from "../settings/controls";
import { formatTenth, OFFSET_STEP_MM, type MarginAlign, type MarginBand } from "./preprint";
import { parseDecimal } from "./search";

/** The position of the margin sheet code as typed: the offset stays text until it is saved. */
export interface PositionDraft {
  band: MarginBand;
  align: MarginAlign;
  offset: string;
}

const FOCUS = "data-[focus-visible]:outline-3 data-[focus-visible]:outline-offset-2 data-[focus-visible]:outline-link";
const SEGMENTS = "flex w-fit max-w-full flex-wrap gap-1 rounded-r2 border border-line-strong bg-surface p-1";
const SEGMENT = `press flex min-h-10 cursor-pointer items-center rounded-r1 px-3 text-small font-bold text-ink transition-colors duration-150 data-[hovered]:bg-sunken data-[selected]:bg-ink data-[selected]:text-surface data-[disabled]:cursor-not-allowed data-[disabled]:opacity-60 ${FOCUS}`;
const STEP = `press flex size-11 shrink-0 items-center justify-center text-ink transition-colors duration-150 data-[hovered]:bg-line data-[pressed]:bg-line data-[disabled]:opacity-50 ${FOCUS}`;

/** The draft from what the type stores. */
export function positionOf(type: { id_margin_band: MarginBand; id_margin_align: MarginAlign; id_margin_offset_mm: number }, locale: string): PositionDraft {
  return { band: type.id_margin_band, align: type.id_margin_align, offset: formatTenth(type.id_margin_offset_mm, locale) };
}

/**
 * The three controls of the sheet code's position (#246, mockups M1 and M2): the band, the end it starts from and the
 * offset from that end. The offset takes a decimal comma or point and steps by 0.5 mm; it never goes below zero.
 */
export function IdPositionControls({
  value,
  onChange,
  isDisabled,
  invalidOffset,
}: {
  value: PositionDraft;
  onChange: (next: PositionDraft) => void;
  isDisabled?: boolean;
  invalidOffset?: boolean;
}) {
  const { t, i18n } = useLingui();
  const bandHint = useId();
  const bands: { id: MarginBand; label: string }[] = [
    { id: "auto", label: t({ message: "Automatic", context: "margin band" }) },
    { id: "top", label: t({ message: "Top", context: "margin band" }) },
    { id: "bottom", label: t({ message: "Bottom", context: "margin band" }) },
    { id: "left", label: t({ message: "Left", context: "margin band" }) },
    { id: "right", label: t({ message: "Right", context: "margin band" }) },
  ];
  const step = (direction: 1 | -1) => {
    const current = parseDecimal(value.offset) ?? 0;
    const next = Math.max(0, Math.round((current + direction * OFFSET_STEP_MM) * 100) / 100);
    onChange({ ...value, offset: formatTenth(next, i18n.locale) });
  };
  const current = parseDecimal(value.offset);

  return (
    <div className="flex flex-col gap-4">
      <RadioGroup
        value={value.band}
        onChange={(v) => onChange({ ...value, band: v as MarginBand })}
        orientation="horizontal"
        isDisabled={isDisabled}
        aria-describedby={bandHint}
        className="flex min-w-0 flex-col gap-1.5"
      >
        <Label className={LABEL}>{t`Band`}</Label>
        <div className={SEGMENTS}>
          {bands.map((b) => (
            <Radio key={b.id} value={b.id} className={SEGMENT}>
              {b.label}
            </Radio>
          ))}
        </div>
        <p id={bandHint} className={HINT}>
          {t`Automatic chooses the largest free band. A band you choose is a rule: if it has no room on a printer, the code is not printed there.`}
        </p>
      </RadioGroup>
      <div className="grid gap-4 sm:grid-cols-2">
        <RadioGroup
          value={value.align}
          onChange={(v) => onChange({ ...value, align: v as MarginAlign })}
          orientation="horizontal"
          isDisabled={isDisabled}
          className="flex min-w-0 flex-col gap-1.5"
        >
          <Label className={LABEL}>{t`Start from`}</Label>
          <div className={SEGMENTS}>
            <Radio value="start" className={SEGMENT}>{t`Left or top`}</Radio>
            <Radio value="end" className={SEGMENT}>{t`Right or bottom`}</Radio>
          </div>
        </RadioGroup>
        <TextField
          value={value.offset}
          onChange={(offset) => onChange({ ...value, offset })}
          isDisabled={isDisabled}
          isInvalid={invalidOffset}
          className="flex min-w-0 flex-col gap-1.5"
        >
          <Label className={LABEL}>{t`Offset from that end`}</Label>
          <Group className="flex h-11 w-fit items-stretch overflow-hidden rounded-r2 border border-line-strong bg-sunken data-[invalid]:border-bad has-[input:focus-visible]:outline-3 has-[input:focus-visible]:outline-offset-2 has-[input:focus-visible]:outline-link">
            <Button
              aria-label={t`Decrease the offset by 0.5 mm`}
              onPress={() => step(-1)}
              isDisabled={isDisabled || (current !== null && current <= 0)}
              className={`${STEP} border-r border-line-strong`}
            >
              <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" className="fill-none stroke-current stroke-2 [stroke-linecap:round]">
                <path d="M3.5 8h9" />
              </svg>
            </Button>
            <span className="flex items-center gap-1 bg-surface px-3">
              <Input
                inputMode="decimal"
                spellCheck={false}
                autoComplete="off"
                enterKeyHint="done"
                className="w-16 bg-transparent text-right text-[16px] font-bold tabular-nums text-ink outline-none"
              />
              <span aria-hidden="true" className="text-small text-ink-2">mm</span>
            </span>
            <Button aria-label={t`Increase the offset by 0.5 mm`} onPress={() => step(1)} isDisabled={isDisabled} className={`${STEP} border-l border-line-strong`}>
              <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" className="fill-none stroke-current stroke-2 [stroke-linecap:round]">
                <path d="M3.5 8h9M8 3.5v9" />
              </svg>
            </Button>
          </Group>
          <Text slot="description" className={HINT}>
            {t`Steps of 0.5 mm. Distance from the end of the label grid to where the code starts.`}
          </Text>
        </TextField>
      </div>
    </div>
  );
}
