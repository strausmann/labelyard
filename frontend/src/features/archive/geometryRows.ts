// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useLingui } from "@lingui/react/macro";
import type { SheetTypeDetail } from "./api";

type Geometry = SheetTypeDetail["geometry"];

/** The measurements an update can change, in the order the diff lists them. */
export function useGeometryRows(): { field: keyof Geometry; label: string; mm: boolean }[] {
  const { t } = useLingui();
  return [
    { field: "page_size", label: t`Page size`, mm: false },
    { field: "cols", label: t`Columns`, mm: false },
    { field: "rows", label: t`Rows`, mm: false },
    { field: "label_w", label: t`Label width`, mm: true },
    { field: "label_h", label: t`Label height`, mm: true },
    { field: "pitch_x", label: t`Horizontal pitch`, mm: true },
    { field: "pitch_y", label: t`Vertical pitch`, mm: true },
    { field: "margin_top", label: t`Top margin`, mm: true },
    { field: "margin_left", label: t`Left margin`, mm: true },
    { field: "corner_radius", label: t`Corner radius`, mm: true },
    { field: "shape", label: t`Label shape`, mm: false },
  ];
}
