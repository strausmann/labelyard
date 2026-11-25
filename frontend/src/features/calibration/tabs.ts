// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

/** The tabs of the calibration page (mockups M7, M8), in their order; the first is the default. */
export const CALIBRATION_TABS = ["print", "readings", "result", "history"] as const;
export type CalibrationTab = (typeof CALIBRATION_TABS)[number];
