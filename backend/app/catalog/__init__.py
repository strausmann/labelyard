# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The sheet-type catalog shipped in the image (ADR 0028, #335).

``sheet_types/<key>.json`` holds one exchange document per type, ``meta.json`` what a document
cannot carry (revision, evidence, baseline fields hash), ``index.json`` the generated summary
(``scripts/catalog_index.py``) and ``presets/`` the preprint presets. ``loader`` reads them.
"""
