#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Generate and check the index of the sheet-type catalog (ADR 0028, spec 6.1.1).

The catalog lives in ``backend/app/catalog/``: one exchange document per type under
``sheet_types/<key>.json``, the sidecar ``meta.json`` and the generated ``index.json``.

    python3 scripts/catalog_index.py            # write index.json
    python3 scripts/catalog_index.py --check    # exit 1 when index.json is stale (CI)
    python3 scripts/catalog_index.py --from-seed   # one-off: convert the 1.7.x seed file

Stdlib only; it imports nothing from the app. The two hash rules are therefore duplicated here:
the geometry hash of ``app.sheets.geometry.geometry_hash`` and the fields hash of
``app.catalog.loader.fields_hash``. ``backend/tests/unit/catalog/test_catalog_index.py`` pins both
against the app (``test_index_hash_rule_matches_the_app``), so a change on one side fails a test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
CATALOG = REPO / "backend" / "app" / "catalog"
SEED = REPO / "backend" / "app" / "seed" / "sheet_types.json"

# app.sheets.geometry.PAGE_SIZES, in hundredths of a millimetre.
PAGE_SIZES = {"A4": (21000, 29700), "Letter": (21590, 27940)}
GEOMETRY_LENGTHS = (
    "label_w",
    "label_h",
    "pitch_x",
    "pitch_y",
    "margin_top",
    "margin_left",
)
# The defaults of app.sheets.exchange.TypeDoc; a field holding its default is left out of the hash.
TYPE_DEFAULTS: dict[str, Any] = {
    "vendor": None,
    "product_codes": [],
    "description": None,
    "verified": False,
    "source_name": None,
    "source_url": None,
    "notes": None,
    "id_placement": "auto",
    "id_cell": None,
    "backing_design": None,
}
INDEX_FIELDS = (
    "key",
    "vendor",
    "product_codes",
    "page_size",
    "cols",
    "rows",
    "label_w_mm",
    "label_h_mm",
    "verified",
    "verified_on",
    "evidence",
    "source_name",
    "source_url",
    "revision",
    "geometry_hash",
    "fields_hash",
)
_EVIDENCE_MAX = 500
_VERIFIED_ON = re.compile(r"\bVerified on real sheets (\d{4}-\d{2}-\d{2})\b")


def _canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _hmm(value: Any, field: str) -> int:
    """Millimetres (at most two decimals) to hundredths, as ``app.sheets.geometry.mm_to_hmm``."""
    scaled = Decimal(str(value)) * 100
    if not scaled.is_finite() or scaled < 0 or scaled != scaled.to_integral_value():
        raise ValueError(f"{field}: not a length with at most two decimals")
    return int(scaled)


def geometry_hash_of(geometry: dict[str, Any]) -> str:
    """The geometry hash the type service stores for a version (D14), from the document's mm."""
    page_size = geometry["page_size"]
    page_w, page_h = PAGE_SIZES[page_size]  # normalise(): the page size from the table
    shape = geometry["shape"]
    radius = geometry.get("corner_radius")
    normalised: dict[str, Any] = {
        "page_size": page_size,
        "page_w": page_w,
        "page_h": page_h,
        "cols": geometry["cols"],
        "rows": geometry["rows"],
        "shape": shape,
        "corner_radius": None if shape == "round" or radius is None else _hmm(radius, "radius"),
    }
    for name in GEOMETRY_LENGTHS:
        normalised[name] = _hmm(geometry[name], name)
    return hashlib.sha256(_canonical(normalised).encode("utf-8")).hexdigest()


def fields_hash_of(type_obj: dict[str, Any]) -> str:
    """The fields hash of a document's ``type`` (ADR 0028): no key, no prefix, no defaults."""
    data = {
        name: value
        for name, value in type_obj.items()
        if name not in ("key", "prefix")
        and not (name in TYPE_DEFAULTS and value == TYPE_DEFAULTS[name])
    }
    return hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build_index(folder: Path) -> list[dict[str, Any]]:
    """The index entries of the catalog in ``folder``, sorted by key."""
    docs = {p.stem: _read_json(p) for p in sorted((folder / "sheet_types").glob("*.json"))}
    meta: dict[str, Any] = _read_json(folder / "meta.json")
    if set(docs) != set(meta):
        missing = sorted(set(docs) ^ set(meta))
        raise ValueError(f"meta.json and the documents disagree on: {', '.join(missing)}")
    entries = []
    for key, doc in docs.items():
        t, g, m = doc["type"], doc["version"]["geometry"], meta[key]
        if t["key"] != key:
            raise ValueError(f"sheet_types/{key}.json holds the key {t['key']}")
        if not isinstance(m.get("revision"), int) or m["revision"] < 1:
            raise ValueError(f"meta.json: {key} needs a revision of at least 1")
        entry = {
            "key": key,
            "vendor": t.get("vendor"),
            "product_codes": t.get("product_codes", []),
            "page_size": g["page_size"],
            "cols": g["cols"],
            "rows": g["rows"],
            "label_w_mm": g["label_w"],
            "label_h_mm": g["label_h"],
            "verified": t.get("verified", False),
            "verified_on": m.get("verified_on"),
            "evidence": m.get("evidence"),
            "source_name": t.get("source_name"),
            "source_url": t.get("source_url"),
            "revision": m["revision"],
            "geometry_hash": geometry_hash_of(g),
            "fields_hash": fields_hash_of(t),
        }
        entries.append({name: entry[name] for name in INDEX_FIELDS})
    return entries


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _evidence(notes: str | None) -> str | None:
    """The notes up to the last sentence that ends within the evidence bound."""
    if not notes:
        return None
    if len(notes) <= _EVIDENCE_MAX:
        return notes
    cut = notes[:_EVIDENCE_MAX]
    end = cut.rfind(". ")
    return cut[: end + 1] if end > 0 else cut.rstrip()


def _strip_mm(hmm: int) -> float:
    return hmm / 100  # app.sheets.geometry.hmm_to_mm


def convert_seed(seed_path: Path, folder: Path) -> list[str]:
    """Write the revision-1 catalog from the 1.7.x seed; return the keys without evidence.

    Revision 1 is what a 1.7.x installation holds after the seed: the seed's geometry (its
    corrections are already the shipped values) and, for the types the seed links to a backing
    design, that design embedded (format version 2).
    """
    types_dir = folder / "sheet_types"
    if (folder / "meta.json").exists() or any(types_dir.glob("*.json")):
        raise ValueError("the catalog is not empty; --from-seed is a one-off conversion")
    seed = _read_json(seed_path)
    designs = {d["key"]: d for d in seed.get("backing_designs", [])}
    links = seed.get("backing_links", {})
    types_dir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {}
    without_evidence: list[str] = []
    for doc in seed["types"]:
        key = doc["type"]["key"]
        doc = json.loads(json.dumps(doc))
        doc["exported_by_hub"] = "catalog"
        link = links.get(key)
        if link is not None:
            design = designs[link["design"]]
            doc["format_version"] = 2
            doc["type"]["backing_design"] = {
                "key": design["key"],
                "name": design["name"],
                "page_size": design["page_size"],
                "strips": [
                    {
                        "x_mm": _strip_mm(round(x * 100)),
                        "y_mm": _strip_mm(round(y * 100)),
                        "w_mm": _strip_mm(round(w * 100)),
                        "h_mm": _strip_mm(round(h * 100)),
                    }
                    for x, y, w, h in design["strips_mm"]
                ],
            }
        verified = bool(doc["type"].get("verified"))
        notes = doc["type"].get("notes")
        found = _VERIFIED_ON.search(notes or "") if verified else None
        evidence = _evidence(notes) if verified else None
        if evidence is None:
            without_evidence.append(key)
        meta[key] = {
            "revision": 1,
            "verified_on": found.group(1) if found else None,
            "evidence": evidence,
            "baseline_fields_hash": fields_hash_of(doc["type"]),
        }
        (types_dir / f"{key}.json").write_text(_dump(doc), encoding="utf-8")
    (folder / "meta.json").write_text(_dump(dict(sorted(meta.items()))), encoding="utf-8")
    return sorted(without_evidence)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail when index.json is stale")
    mode.add_argument("--from-seed", action="store_true", help="convert the 1.7.x seed (one-off)")
    parser.add_argument("--folder", type=Path, default=CATALOG, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    folder: Path = args.folder
    index_path = folder / "index.json"
    try:
        if args.from_seed:
            without = convert_seed(SEED, folder)
            print(f"keys without evidence ({len(without)}): {', '.join(without) or '-'}")
        index = _dump(build_index(folder))
    except (ValueError, KeyError) as exc:
        print(f"catalog_index: {exc}", file=sys.stderr)
        return 1
    if args.check:
        current = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
        if current != index:
            print(
                "catalog_index: index.json is stale; run python3 scripts/catalog_index.py",
                file=sys.stderr,
            )
            return 1
        return 0
    index_path.write_text(index, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
