<!--
SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Preprint presets

One JSON file per preset, `<key>.json`, read by `app.catalog.loader.load_preset` (ADR 0028). A file
holds a `PreprintPresetDoc` (`app.sheets.exchange`, #246): `key`, `name`, `page_size` and `zones`, each
zone `x_mm`, `y_mm`, `w_mm`, `h_mm` and an optional `note`, from the page's top left. The first presets
arrive with the measured Avery Zweckform sheets (#246, milestone 1.8.0 Task 10c).
