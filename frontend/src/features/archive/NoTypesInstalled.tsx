// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { Trans } from "@lingui/react/macro";
import { Link } from "@tanstack/react-router";

/**
 * A fresh installation has no sheet types (#335, ADR 0028): they come from the catalog in the sheet archive. Shown
 * where a page needs a type and none is installed, with the way to add one.
 */
export function NoTypesInstalled() {
  return (
    <p className="m-0 max-w-prose text-ink-2">
      <Trans>
        No sheet types are installed yet.{" "}
        <Link to="/sheets/archive" className="text-link underline underline-offset-2">
          Add one from the catalog in the sheet archive
        </Link>
        .
      </Trans>
    </p>
  );
}
