# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Derivation of sheet type keys and code prefixes from vendor and product code."""

from __future__ import annotations

import re

from app.sheets.geometry import Geometry

KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")

# Keys a type must not have, because a fixed path segment under /api/v1/sheet-types/ would shadow
# its detail route. The floor below holds without any route imported (service-level callers, unit
# tests); ``create_app()`` adds the segments of every registered route through
# ``reserve_static_segments``, so a later route (the catalogue of Task 8c) reserves its own.
RESERVED_TYPE_KEYS: set[str] = {"facets", "import"}


def reserve_static_segments(prefix: str, paths: list[str]) -> None:
    """Reserve the first segment after ``prefix`` of every route path that is not a parameter."""
    lead = prefix.rstrip("/") + "/"
    for path in paths:
        if not path.startswith(lead):
            continue
        segment = path[len(lead) :].split("/", 1)[0]
        if segment and not segment.startswith("{"):
            RESERVED_TYPE_KEYS.add(segment)


PREFIX_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
_PACK_SUFFIX = re.compile(r"-\d+$")
_REV_SUFFIX = re.compile(r"REV$")
_QR_MIN_SIDE_HMM = 1000  # 10 mm (OC-28, issue #330)


def derive_prefix(vendor: str, first_code: str) -> str:
    code = _PACK_SUFFIX.sub("", first_code.strip().upper())
    code = _REV_SUFFIX.sub("", code)
    code = re.sub(r"[^A-Z0-9]", "", code)
    if code and code[0].isdigit():
        letters = re.sub(r"[^A-Za-z]", "", vendor)
        if not letters:
            raise ValueError("vendor has no letter to prefix a numeric code")
        code = letters[0].upper() + code
    if not PREFIX_PATTERN.fullmatch(code):
        raise ValueError(f"cannot derive a valid prefix from {first_code!r}")
    return code


def derive_key(vendor: str, first_code: str) -> str:
    base = _PACK_SUFFIX.sub("", first_code.strip())
    key = re.sub(r"[^a-z0-9]+", "-", f"{vendor} {base}".lower()).strip("-")
    if not KEY_PATTERN.fullmatch(key):
        raise ValueError(f"cannot derive a valid key from {vendor!r} {first_code!r}")
    return key


def default_allow_qr(g: Geometry) -> bool:
    """A QR code is allowed by default when the shorter label side is at least 10 mm.

    Evidence: on Avery Zweckform L4731REV (25.4 x 9.98 mm) a QR code with a short number beside
    it prints and scans. The value is only the default for a new type; the type keeps its own
    switch afterwards.
    """
    return min(g.label_w, g.label_h) >= _QR_MIN_SIDE_HMM  # OC-28
