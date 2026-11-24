# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read the sheet-type catalog that ships in the image (ADR 0028, spec 6.1).

Every file here is read-only data of the image. A key comes from a request, so it is checked
against the key pattern and the index before it becomes a file name: no path is ever built from a
key the index does not list.

Two hashes identify an entry. The geometry hash is the type service's (D14). The fields hash
(``fields_hash``) covers the document's ``type`` without ``key`` and ``prefix`` and without the
fields that hold their default value, so a field added later with a default changes no hash. The
same rule is duplicated in ``scripts/catalog_index.py``, which must not import the app; a test pins
the two together.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.resources
import json
from datetime import date
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.models.backing_design import BackingDesign
from app.models.sheet import SheetType
from app.sheets.backing_design_service import to_doc
from app.sheets.errors import CatalogEntryNotFoundError
from app.sheets.exchange import ExportDoc, TypeDoc, parse_import
from app.sheets.geometry import hmm_to_mm
from app.sheets.naming import KEY_PATTERN
from app.sheets.preprint_service import zones_to_mm

CATALOG_ROOT = importlib.resources.files("app.catalog")
_HASH = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CatalogMeta(BaseModel):
    """What an exchange document cannot carry, per key (``meta.json``, spec S3, S14)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: Annotated[int, Field(strict=True, ge=1)]
    verified_on: date | None
    evidence: Annotated[str, Field(min_length=1, max_length=500)] | None
    # The fields hash of the revision-1 document: what a 1.7.x installation holds after the
    # seed's corrections. Written once, never changed by a later revision (S14).
    baseline_fields_hash: _HASH
    # Fields hashes of earlier shipped states that still count as unedited: an installation
    # seeded by an older release and then by 1.7.x holds them (before 1.6.0 three types shipped
    # with ``allow_qr`` off). Derived by running the old seed files through the 1.7.1 seeder.
    prior_fields_hashes: list[_HASH] = Field(default_factory=list)

    def counts_as_unedited(self, fields_hash: str) -> bool:
        """Whether an installed type with this fields hash holds a state the catalog shipped."""
        return fields_hash == self.baseline_fields_hash or fields_hash in self.prior_fields_hashes


class CatalogEntry(BaseModel):
    """One row of ``index.json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    vendor: str | None
    product_codes: list[str]
    page_size: Literal["A4", "Letter"]
    cols: int
    rows: int
    label_w_mm: float
    label_h_mm: float
    verified: bool
    verified_on: date | None
    evidence: str | None
    source_name: str | None
    source_url: str | None
    revision: int
    geometry_hash: _HASH
    fields_hash: _HASH


def _canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def fields_hash(type_doc: TypeDoc) -> str:
    """SHA-256 of the canonical JSON of the type fields, defaults left out (ADR 0028)."""
    data = type_doc.model_dump(mode="json", exclude_defaults=True, exclude={"key", "prefix"})
    return hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest()


def type_doc_of(t: SheetType, design: BackingDesign | None) -> TypeDoc | None:
    """The type fields of an installed type, its linked design always embedded.

    ``export_type`` leaves out a shipped type's default design (a version-1 document); a hash
    comparison with the catalog document, which embeds it, must not, so a shipped link never
    counts as an edit (S14). ``design`` is the design the type links to, or ``None``.

    ``None`` when the row holds a value an exchange document can no longer carry, such as a
    source URL with user info that 1.7.x accepted and #427 refuses. No catalog revision ships
    such a value, so callers treat the type as locally edited.
    """
    if design is not None and design.id != t.backing_design_id:
        raise ValueError("the design is not the one this type links to")
    if design is None and t.backing_design_id is not None:
        raise ValueError("the type links a design that was not passed")
    if t.preprint_preset_id is not None:
        # No catalog entry links a preprint preset yet (#246; Task 10c ships the first): a type
        # that links one differs from every shipped state and counts as edited.
        return None
    try:
        return _type_doc(t, design)
    except ValidationError:
        return None


def _type_doc(t: SheetType, design: BackingDesign | None) -> TypeDoc:
    return TypeDoc(
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
        id_placement=cast(Literal["auto", "margin", "back_side", "cell"], t.id_placement),
        id_cell=t.id_cell,
        backing_design=None if design is None else to_doc(design),
        # #246: the defaults leave the fields hash of a type that never set them unchanged.
        id_margin_band=cast(Literal["auto", "top", "bottom", "left", "right"], t.id_margin_band),
        id_margin_align=cast(Literal["start", "end"], t.id_margin_align),
        id_margin_offset_mm=hmm_to_mm(t.id_margin_offset_hmm or 0),
        preprint_zones=zones_to_mm(t.preprint_zones or []),
    )


def _read(*parts: str) -> str:
    return CATALOG_ROOT.joinpath(*parts).read_text(encoding="utf-8")


@functools.cache
def load_index() -> tuple[CatalogEntry, ...]:
    return tuple(CatalogEntry.model_validate(e) for e in json.loads(_read("index.json")))


@functools.cache
def _all_meta() -> dict[str, CatalogMeta]:
    raw: dict[str, Any] = json.loads(_read("meta.json"))
    return {key: CatalogMeta.model_validate(value) for key, value in raw.items()}


def clear_caches() -> None:
    """Forget the cached index and sidecar (tests that swap the catalog files)."""
    load_index.cache_clear()
    _all_meta.cache_clear()


def _known(key: str) -> str:
    if not KEY_PATTERN.fullmatch(key) or key not in {e.key for e in load_index()}:
        raise CatalogEntryNotFoundError()
    return key


def load_entry(key: str) -> ExportDoc:
    """The catalog document of ``key``, through the same checks as an import."""
    raw = CATALOG_ROOT.joinpath("sheet_types", f"{_known(key)}.json").read_bytes()
    return parse_import(raw)


def load_meta(key: str) -> CatalogMeta:
    meta = _all_meta().get(_known(key))
    if meta is None:
        raise CatalogEntryNotFoundError()
    return meta


def load_preset(key: str) -> dict[str, Any]:
    """A preprint preset as stored (its model arrives with the presets, Task 10a)."""
    if not KEY_PATTERN.fullmatch(key):
        raise CatalogEntryNotFoundError()
    path = CATALOG_ROOT.joinpath("presets", f"{key}.json")
    if not path.is_file():
        raise CatalogEntryNotFoundError()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CatalogEntryNotFoundError()
    return data
