# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The sheet-type catalog in the image (ADR 0028, #335, spec 6.1).

The catalog is a folder of exchange documents, a ``meta.json`` sidecar and a generated
``index.json``. These tests pin that the three agree, that every document passes the write path
an import takes, and that the two hashes are the ones the rest of the hub computes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from app.catalog import loader
from app.models.backing_design import BackingDesign
from app.models.sheet import SheetType
from app.seed.sheet_seed import load_seed
from app.sheets import type_service as ts
from app.sheets.backing_design_service import strips_to_mm
from app.sheets.errors import CatalogEntryNotFoundError
from app.sheets.exchange import (
    BackingDesignDoc,
    ExportDoc,
    TypeDoc,
    geometry_from_doc,
    parse_import,
)
from app.sheets.geometry import geometry_hash

REPO = Path(__file__).resolve().parents[4]
CATALOG = REPO / "backend" / "app" / "catalog"
SCRIPT = REPO / "scripts" / "catalog_index.py"
ULTRAGRIP_TYPES = {"avery-zweckform-6121", "avery-zweckform-3490"}


def _script() -> ModuleType:
    """``scripts/catalog_index.py`` (stdlib only, it imports nothing from ``app``)."""
    spec = importlib.util.spec_from_file_location("catalog_index", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["catalog_index"] = module
    spec.loader.exec_module(module)
    return module


def _doc_files() -> dict[str, Path]:
    return {p.stem: p for p in sorted((CATALOG / "sheet_types").glob("*.json"))}


def _docs() -> dict[str, ExportDoc]:
    return {key: parse_import(path.read_bytes()) for key, path in _doc_files().items()}


def _meta() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((CATALOG / "meta.json").read_text(encoding="utf-8"))
    return data


def test_catalog_has_the_39_seed_types() -> None:
    seed_keys = {doc.type.key for doc in load_seed().docs}
    assert len(seed_keys) == 39
    assert set(_doc_files()) == seed_keys


def test_every_document_parses_as_an_export_doc() -> None:
    """Each document passes the import's write path: strict schema, ``validate_geometry`` with
    ``on_write`` (the corner radius bound), the placement check, and text cleaning that changes
    nothing (``sanitise_text``, ``check_http_url``)."""
    for key, path in _doc_files().items():
        doc = parse_import(path.read_bytes())
        assert doc.type.key == key, f"{path.name} holds the key {doc.type.key}"
        t = doc.type
        fields = ts.TypeFields(
            key=t.key,
            prefix=t.prefix,
            vendor=t.vendor,
            product_codes=list(t.product_codes),
            description=t.description,
            allow_qr=t.allow_qr,
            verified=t.verified,
            source_name=t.source_name,
            source_url=t.source_url,
            notes=t.notes,
        )
        assert ts._clean_fields(fields) == fields, f"{key}: the write path would change its text"
        assert loader.load_entry(key) == doc


def test_every_entry_has_revision_and_evidence_source() -> None:
    raw = _meta()
    assert set(raw) == set(_doc_files())
    docs = _docs()
    for key, value in raw.items():
        meta = loader.CatalogMeta.model_validate(value)
        assert meta == loader.load_meta(key)
        assert meta.revision >= 1
        if docs[key].type.verified:
            assert meta.evidence, f"{key} is verified but names no evidence"
        else:
            assert meta.verified_on is None, f"{key} has a verification date but is not verified"


def test_catalog_meta_refuses_unknown_fields_and_revision_zero() -> None:
    good = {"revision": 1, "verified_on": None, "evidence": None, "baseline_fields_hash": "0" * 64}
    loader.CatalogMeta.model_validate(good)
    for bad in ({**good, "extra": 1}, {**good, "revision": 0}, {**good, "evidence": "x" * 501}):
        with pytest.raises(ValueError):
            loader.CatalogMeta.model_validate(bad)


def test_index_matches_the_documents() -> None:
    shipped = json.loads((CATALOG / "index.json").read_text(encoding="utf-8"))
    assert shipped == _script().build_index(CATALOG)
    by_key = {e.key: e for e in loader.load_index()}
    docs, meta = _docs(), _meta()
    assert set(by_key) == set(docs)
    for key, doc in docs.items():
        e, g = by_key[key], doc.version.geometry
        assert (e.vendor, e.product_codes, e.verified) == (
            doc.type.vendor,
            list(doc.type.product_codes),
            doc.type.verified,
        )
        assert (e.page_size, e.cols, e.rows, e.label_w_mm, e.label_h_mm) == (
            g.page_size,
            g.cols,
            g.rows,
            g.label_w,
            g.label_h,
        )
        assert (e.source_name, e.source_url) == (doc.type.source_name, doc.type.source_url)
        assert e.revision == meta[key]["revision"]
        assert e.evidence == meta[key]["evidence"]
        assert e.fields_hash == loader.fields_hash(doc.type)


def test_geometry_hash_matches_the_type_service_hash() -> None:
    """The index's geometry hash is the one the type service stores for the version (D14)."""
    by_key = {e.key: e for e in loader.load_index()}
    for key, doc in _docs().items():
        assert by_key[key].geometry_hash == geometry_hash(geometry_from_doc(doc.version.geometry))


def test_index_hash_rule_matches_the_app() -> None:
    """The script duplicates the fields-hash rule; both give the same hash."""
    script = _script()
    for path in _doc_files().values():
        raw = json.loads(path.read_text(encoding="utf-8"))
        doc = ExportDoc.model_validate(raw)
        assert script.fields_hash_of(raw["type"]) == loader.fields_hash(doc.type)
        assert script.geometry_hash_of(raw["version"]["geometry"]) == geometry_hash(
            geometry_from_doc(doc.version.geometry)
        )
    # Defaults written out explicitly, and a round label with a radius, hash like their omission.
    explicit = {
        "key": "x-1",
        "prefix": "XA",
        "allow_qr": True,
        "vendor": None,
        "product_codes": [],
        "verified": False,
        "id_placement": "auto",
        "id_cell": None,
        "backing_design": None,
    }
    assert script.fields_hash_of(explicit) == loader.fields_hash(TypeDoc.model_validate(explicit))
    assert script.fields_hash_of(explicit) == script.fields_hash_of(
        {"key": "y-2", "prefix": "YB", "allow_qr": True}
    )


def test_adding_a_defaulted_type_field_keeps_the_hash() -> None:
    """Task 10a adds defaulted fields to ``TypeDoc``; no shipped hash may change through that."""

    class LaterTypeDoc(TypeDoc):
        id_margin_band: str | None = None
        id_margin_offset: float = 0.0

    for doc in _docs().values():
        data = doc.type.model_dump(mode="json")
        assert loader.fields_hash(LaterTypeDoc.model_validate(data)) == loader.fields_hash(doc.type)


def test_6121_and_3490_documents_embed_the_ultragrip_design() -> None:
    seed = load_seed()
    designs = {d.key: d for d in seed.backing_designs}
    links = dict(seed.backing_links)
    assert set(links) == ULTRAGRIP_TYPES
    for key, doc in _docs().items():
        if key not in links:
            assert doc.type.backing_design is None
            continue
        shipped = designs[links[key].design]
        assert doc.format_version == 2
        assert doc.type.backing_design == BackingDesignDoc(
            key=shipped.key,
            name=shipped.name,
            page_size=shipped.page_size,
            strips=strips_to_mm(shipped.strips_hmm()),
        )
        assert doc.type.backing_design.key == "avery-zweckform-ultragrip"


def _seeded_row(seed_doc: ExportDoc, design: BackingDesign | None) -> SheetType:
    """The row the 1.7.x seeder writes for a type it inserted (and linked)."""
    t = seed_doc.type
    return SheetType(
        key=t.key,
        prefix=t.prefix,
        vendor=t.vendor,
        product_codes=list(t.product_codes),
        description=t.description,
        allow_qr=t.allow_qr,
        verified=t.verified,
        origin="seed",
        source_name=t.source_name,
        source_url=t.source_url,
        notes=t.notes,
        backing_design_id=None if design is None else design.id,
    )


def _seed_design_rows() -> dict[str, BackingDesign]:
    seed = load_seed()
    return {
        d.key: BackingDesign(
            key=d.key, name=d.name, page_size=d.page_size, strips=d.strips_hmm(), origin="seed"
        )
        for d in seed.backing_designs
    }


def test_baseline_hash_is_the_revision_1_document_hash() -> None:
    """S14: the baseline is what a 1.7.x installation holds after the seed, link included."""
    seed = load_seed()
    designs = _seed_design_rows()
    links = dict(seed.backing_links)
    docs, meta = _docs(), _meta()
    for seed_doc in seed.docs:
        key = seed_doc.type.key
        assert meta[key]["revision"] == 1
        assert meta[key]["baseline_fields_hash"] == loader.fields_hash(docs[key].type)
        design = designs[links[key].design] if key in links else None
        row = _seeded_row(seed_doc, design)
        installed = loader.fields_hash(loader.type_doc_of(row, design))
        assert installed == meta[key]["baseline_fields_hash"], (
            f"{key}: a 1.7.x row counts as edited"
        )


def test_type_doc_of_embeds_a_linked_design_and_refuses_a_foreign_one() -> None:
    designs = _seed_design_rows()
    design = designs["avery-zweckform-ultragrip"]
    seed_doc = next(d for d in load_seed().docs if d.type.key == "avery-zweckform-6121")
    row = _seeded_row(seed_doc, design)
    with_design = loader.type_doc_of(row, design)
    assert with_design.backing_design is not None
    assert with_design.backing_design.key == design.key
    other = BackingDesign(key="other", name="Other", page_size="A4", strips=[], origin="custom")
    with pytest.raises(ValueError):
        loader.type_doc_of(row, other)
    row.backing_design_id = None
    assert loader.type_doc_of(row, None).backing_design is None
    assert loader.fields_hash(loader.type_doc_of(row, None)) != loader.fields_hash(with_design)


def test_no_catalog_key_is_reserved() -> None:
    from app.main import create_app
    from app.sheets.naming import RESERVED_TYPE_KEYS

    create_app()  # adds the static segments of every registered route
    assert {"facets", "import"} <= RESERVED_TYPE_KEYS
    assert set(_doc_files()) & RESERVED_TYPE_KEYS == set()


def test_unknown_or_hostile_keys_are_not_found() -> None:
    for key in ("no-such-type", "../meta", "avery-7160/../../x", ""):
        with pytest.raises(CatalogEntryNotFoundError):
            loader.load_entry(key)
        with pytest.raises(CatalogEntryNotFoundError):
            loader.load_meta(key)
        with pytest.raises(CatalogEntryNotFoundError):
            loader.load_preset(key)
    with pytest.raises(CatalogEntryNotFoundError):
        loader.load_preset("avery-7160")  # no preset is shipped yet (Task 10a adds the first)
