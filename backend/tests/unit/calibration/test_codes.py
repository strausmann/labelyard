# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The ``LYC1`` codes of a calibration page (#240, design 7.2, ADR 0029).

The grammar is a durable printed format: these tests pin the field order, the alphanumeric set,
the QR version and error correction floors, and that a payload is never truncated.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
import qrcode
import qrcode.exceptions
import qrcode.util
from app.calibration import codes
from app.calibration.codes import (
    ALNUM,
    AUTO_DETECT_PATTERNS,
    CELL_QR_HMM,
    CELL_VERSION,
    MIN_MODULE_HMM,
    PAGE_MIN_VERSION,
    PAGE_QR_HMM,
    catalog_geometry_diff,
    cell_payload,
    cell_qr_version,
    page_payload,
    page_qr_version,
)
from app.catalog import loader
from app.catalog import service as cs
from app.page_render.transform import IDENTITY, Deviation
from app.sheets import type_service as ts
from app.sheets.errors import CalibrationQrTooLargeError
from app.sheets.geometry import Geometry
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# The worst case of design 7.2: every bound at its most negative, every optional field present.
WORST_DEV = Deviation(
    offset_x_hmm=-1000,
    offset_y_hmm=-1000,
    scale_x_ppm=950_000,
    scale_y_ppm=950_000,
    rotation_udeg=-2_000_000,
)
WORST_DIFF: dict[Any, int] = {"PX": 100000, "PY": 100000, "MT": 100000, "ML": 100000}
LONGEST_PAGE = (
    "LYC1:999999:0:AAAAAAAAAA:1000000:-1000:-1000:-50000:-50000:-2000000"
    ":K1:PX100000:PY100000:MT100000:ML100000"
)
LONGEST_CELL = "LYC1:999999:400"

K6121 = "avery-zweckform-6121"

G_CUSTOM = Geometry(
    "A4", 21000, 29700, 4570, 2120, 4, 12, 4850, 2120, 2150, 840, None, "rectangle"
)  # fmt: skip


def _encodes_at(payload: str, version: int) -> bool:
    qr = qrcode.QRCode(version=version, error_correction=codes.ECC, border=0)
    qr.add_data(qrcode.util.QRData(payload, mode=qrcode.util.MODE_ALPHA_NUM))
    try:
        qr.make(fit=False)
    except qrcode.exceptions.DataOverflowError:  # the payload does not fit this version
        return False
    return qr.modules_count == 17 + 4 * version


# ---------------------------------------------------------------------------- capacity


def test_longest_page_payload_encodes_at_5_M() -> None:  # noqa: N802
    payload = page_payload(
        run_no=999999,
        prefix="AAAAAAAAAA",
        version=1_000_000,
        dev=WORST_DEV,
        check=True,
        geometry_diff=WORST_DIFF,
    )
    assert payload == LONGEST_PAGE
    assert len(payload) == 106
    assert page_qr_version(payload) == PAGE_MIN_VERSION == 5
    assert codes.ECC == qrcode.constants.ERROR_CORRECT_M
    assert _encodes_at(payload, 5)
    modules = 17 + 4 * 5
    assert modules == 37
    assert PAGE_QR_HMM / modules >= MIN_MODULE_HMM  # 1000 hmm / 37 = 27 hmm >= 25 hmm


def test_longest_cell_payload_encodes_at_1_M() -> None:  # noqa: N802
    payload = cell_payload(999999, 400)
    assert payload == LONGEST_CELL
    assert cell_qr_version(payload) == CELL_VERSION == 1
    assert _encodes_at(payload, 1)
    # 21 modules plus a 4-module quiet zone each side inside the 8 mm cell code
    assert CELL_QR_HMM / (21 + 8) >= MIN_MODULE_HMM


def test_short_payloads_still_draw_at_the_floor_versions() -> None:
    page = page_payload(
        run_no=1, prefix="L4736", version=1, dev=IDENTITY, check=False, geometry_diff={}
    )
    assert page_qr_version(page) == 5
    assert cell_qr_version(cell_payload(1, 1)) == 1


def test_payloads_use_only_the_alphanumeric_set() -> None:
    assert frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ:-") == ALNUM
    for payload in (
        LONGEST_PAGE,
        page_payload(
            run_no=7, prefix="A7160", version=3, dev=WORST_DEV, check=False, geometry_diff={}
        ),
        cell_payload(7, 1),
    ):
        assert set(payload) <= ALNUM, payload


def test_a_prefix_outside_the_pattern_is_refused() -> None:
    with pytest.raises(ValueError, match="prefix"):
        page_payload(
            run_no=1, prefix="l4736", version=1, dev=IDENTITY, check=False, geometry_diff={}
        )
    with pytest.raises(ValueError, match="geometry"):
        page_payload(
            run_no=1, prefix="L4736", version=1, dev=IDENTITY, check=False,
            geometry_diff={"XX": 1},  # type: ignore[dict-item]
        )  # fmt: skip
    with pytest.raises(ValueError):
        page_payload(
            run_no=0, prefix="L4736", version=1, dev=IDENTITY, check=False, geometry_diff={}
        )
    with pytest.raises(ValueError):
        cell_payload(1, 0)


# ---------------------------------------------------------------------------- the fields


def test_uncorrected_page_has_all_correction_fields_zero() -> None:
    payload = page_payload(
        run_no=14, prefix="L4736", version=2, dev=IDENTITY, check=False, geometry_diff={}
    )
    assert payload == "LYC1:14:0:L4736:2:0:0:0:0:0"


def test_correction_fields_are_in_their_order() -> None:
    dev = Deviation(
        offset_x_hmm=12, offset_y_hmm=-34, scale_x_ppm=1_000_560, scale_y_ppm=999_100,
        rotation_udeg=-7800,
    )  # fmt: skip
    payload = page_payload(
        run_no=3, prefix="L4736", version=1, dev=dev, check=False, geometry_diff={}
    )
    assert payload == "LYC1:3:0:L4736:1:12:-34:560:-900:-7800"


def test_check_page_carries_k1() -> None:
    payload = page_payload(
        run_no=14, prefix="L4736", version=2, dev=IDENTITY, check=True, geometry_diff={"MT": 2200}
    )
    assert payload == "LYC1:14:0:L4736:2:0:0:0:0:0:K1:MT2200"
    calibration = page_payload(
        run_no=14, prefix="L4736", version=2, dev=IDENTITY, check=False, geometry_diff={}
    )
    assert ":K" not in calibration


def test_geometry_fields_keep_the_grammar_order() -> None:
    payload = page_payload(
        run_no=1, prefix="L4736", version=1, dev=IDENTITY, check=False,
        geometry_diff={"ML": 4, "PY": 2, "MT": 3, "PX": 1},
    )  # fmt: skip
    assert payload.endswith(":PX1:PY2:MT3:ML4")


def test_cell_code_has_no_pass_suffix() -> None:
    assert cell_payload(14, 7) == "LYC1:14:7"
    assert cell_payload(14, 7).count(":") == 2


def test_prefix_with_trailing_digits_is_unambiguous() -> None:
    def code(prefix: str, version: int) -> str:
        return page_payload(
            run_no=1, prefix=prefix, version=version, dev=IDENTITY, check=False, geometry_diff={}
        )

    # Joined as one token both would read "A71601" (the L4736REV1 problem of design 7.2).
    a, b = code("A7160", 1), code("A71", 601)
    assert a != b
    assert a.split(":")[3:5] == ["A7160", "1"]
    assert b.split(":")[3:5] == ["A71", "601"]


# ---------------------------------------------------------------------------- never truncated


def test_run_past_capacity_grows_then_refuses_never_truncates() -> None:
    def long_page(run_no: int) -> str:
        return page_payload(
            run_no=run_no, prefix="AAAAAAAAAA", version=1_000_000, dev=WORST_DEV, check=True,
            geometry_diff=WORST_DIFF,
        )  # fmt: skip

    # 122 alphanumeric characters is the 5-M capacity; 17 more run digits make 123.
    big = long_page(10**22)
    assert len(big) == 123 and str(10**22) in big  # the payload is whole
    assert page_qr_version(long_page(10**21)) == 5  # 122 characters still fit 5-M
    # A larger symbol keeps 0.25 mm modules at version 6 (11 mm / 41 = 0.268 mm): it grows.
    assert page_qr_version(big, symbol_hmm=1100) == 6
    # At the drawn 10 mm, version 6 would have 0.244 mm modules: refused, never shortened.
    with pytest.raises(CalibrationQrTooLargeError) as raised:
        page_qr_version(big)
    err = raised.value
    assert (err.status, err.error_code) == (422, "calibration_qr_too_large")
    assert err.detail == {"length": 123, "version_needed": 6, "max_version": 5}
    # Version 7 at 11 mm (45 modules, 0.244 mm) is refused as well.
    huger = long_page(10**60)
    with pytest.raises(CalibrationQrTooLargeError):
        page_qr_version(huger, symbol_hmm=1100)
    # Past the largest QR version at all: still the same refusal, not a library error, and the
    # detail names no version that does not exist.
    with pytest.raises(CalibrationQrTooLargeError) as past_40:
        page_qr_version("LYC1:" + "1" * 5000)
    assert past_40.value.detail == {"length": 5005, "version_needed": None, "max_version": 5}


def test_a_cell_code_past_capacity_prints_the_number_only() -> None:
    # 1-M holds 20 alphanumeric characters; at 8 mm version 2 has 0.242 mm modules.
    assert cell_qr_version(cell_payload(10**10, 400)) == 1  # 20 characters
    assert cell_qr_version(cell_payload(10**11, 400)) is None
    assert cell_qr_version("LYC1:" + "1" * 5000) is None


# ---------------------------------------------------------------------------- auto-detect guard


def _catalog() -> Iterator[tuple[str, int]]:
    for entry in loader.load_index():
        doc = loader.load_entry(entry.key)
        g = doc.version.geometry
        yield doc.type.prefix, g.cols * g.rows


def test_no_payload_matches_an_auto_detect_pattern() -> None:
    corrections = [
        IDENTITY,
        WORST_DEV,
        Deviation(1000, 1000, 1_050_000, 1_050_000, 2_000_000),
        Deviation(5, -5, 1_000_001, 999_999, 1),
    ]
    diffs: list[dict[Any, int]] = [{}, WORST_DIFF, {"PX": 4850}, {"ML": 0}]
    checked = 0
    catalog = list(_catalog())
    assert len(catalog) > 30
    for prefix, cells in catalog:
        for run_no in (1, 999999):
            for dev in corrections:
                for check in (False, True):
                    for diff in diffs:
                        payload = page_payload(
                            run_no=run_no, prefix=prefix, version=1, dev=dev, check=check,
                            geometry_diff=diff,
                        )  # fmt: skip
                        _assert_not_detected(payload)
                        checked += 1
            for cell in range(1, cells + 1):
                _assert_not_detected(cell_payload(run_no, cell))
                checked += 1
    assert checked > 1000


def _assert_not_detected(payload: str) -> None:
    hits = [p.pattern for p in AUTO_DETECT_PATTERNS if p.search(payload)]
    assert not hits, (payload, hits)


@pytest.mark.parametrize(
    "bad",
    [
        "A@B", "HTTP://X", "MAILTO:A", "mailto:a", "TEL:1", "SMS:1", "WIFI:S", "GEO:1",
        "BEGIN:VCARD", "+491234",
    ],
)  # fmt: skip
def test_the_auto_detect_patterns_catch_what_a_scanner_acts_on(bad: str) -> None:
    assert any(p.search(bad) for p in AUTO_DETECT_PATTERNS), bad


@pytest.mark.parametrize("prefix", ["TEL", "ATEL", "GEO", "SMS", "WIFI", "BEGIN", "MAILTO"])
def test_a_custom_prefix_like_a_scheme_does_not_change_detection(prefix: str) -> None:
    # A scanner app picks the content type from the start of the content; every payload starts
    # with LYC1:, so a custom prefix inside it is never read as tel:, geo:, WIFI: and the like.
    payloads = [
        page_payload(
            run_no=run_no, prefix=prefix, version=1, dev=dev, check=check, geometry_diff=diff
        )
        for run_no in (1, 999999)
        for dev in (IDENTITY, WORST_DEV)
        for check in (False, True)
        for diff in ({}, WORST_DIFF)
    ]
    payloads += [cell_payload(1, 1), cell_payload(999999, 400)]
    for payload in payloads:
        assert payload.startswith("LYC1:"), payload
        hits = [p.pattern for p in AUTO_DETECT_PATTERNS if re.match(p.pattern, payload, re.I)]
        assert not hits, (payload, hits)


def test_a_leading_plus_only_counts_at_the_start() -> None:
    assert not any(p.search("LYC1:+1") for p in AUTO_DETECT_PATTERNS)


# ---------------------------------------------------------------------------- geometry reference


@pytest.fixture(autouse=True)
def _fresh_catalog_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    loader.clear_caches()
    yield
    monkeypatch.undo()
    loader.clear_caches()


@pytest_asyncio.fixture
async def session(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as s:
        yield s


def _offer_newer_geometry(monkeypatch: pytest.MonkeyPatch, key: str, **geometry: Any) -> None:
    """The image now ships catalog revision 2 of ``key`` with other geometry (an update)."""
    meta = loader.load_meta(key).model_copy(update={"revision": 2})
    metas = {**loader._all_meta(), key: meta}
    doc = loader.load_entry(key)
    newer = doc.model_copy(
        update={
            "version": doc.version.model_copy(
                update={"geometry": doc.version.geometry.model_copy(update=geometry)}
            )
        }
    )
    real = loader.load_entry
    monkeypatch.setattr(loader, "_all_meta", lambda: metas)
    monkeypatch.setattr(loader, "load_entry", lambda k: newer if k == key else real(k))


async def test_custom_type_has_no_geometry_fields(session: AsyncSession) -> None:
    _, v1 = await ts.create_type(
        session,
        ts.TypeFields(
            key="acme-custom", prefix="ACME", vendor="Acme", product_codes=[],
            description=None, allow_qr=None,
        ),
        G_CUSTOM,
        actor="admin",
    )  # fmt: skip
    assert await catalog_geometry_diff(session, v1) == {}
    edited = dataclasses.replace(G_CUSTOM, pitch_x=4900)
    v2 = await ts.add_version(session, "acme-custom", edited, actor="admin", change_note="wider")
    assert await catalog_geometry_diff(session, v2) == {}
    # v1 again, now that a newer version exists: still no reference, so still no fields.
    assert await catalog_geometry_diff(session, v1) == {}


async def test_type_installed_under_a_new_key_has_no_geometry_fields(
    session: AsyncSession,
) -> None:
    t = (
        await cs.install(session, K6121, as_new_key="my-6121", prefix="MY6121", actor="admin")
    ).type
    v = await ts.current_version(session, t.id)
    assert await catalog_geometry_diff(session, v) == {}


async def test_type_with_pending_update_prints_no_geometry_fields(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = (await cs.install(session, K6121, as_new_key=None, prefix=None, actor="admin")).type
    _offer_newer_geometry(monkeypatch, K6121, pitch_x=99.0, margin_top=3.0)
    v = await ts.current_version(session, t.id)
    # Revision 1 installed and unedited; the image's revision 2 is not the reference (S17).
    assert await catalog_geometry_diff(session, v) == {}


async def test_locally_edited_pitch_prints_px(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = (await cs.install(session, K6121, as_new_key=None, prefix=None, actor="admin")).type
    v1 = await ts.current_version(session, t.id)
    _offer_newer_geometry(monkeypatch, K6121, margin_top=3.0)  # the image moved on as well
    g = ts.version_geometry(v1)
    v2 = await ts.add_version(
        session, K6121, dataclasses.replace(g, pitch_x=g.pitch_x + 10), actor="admin",
        change_note="measured",
    )  # fmt: skip
    # Only the field that differs from the catalog-written version in the database.
    assert await catalog_geometry_diff(session, v2) == {"PX": v1.pitch_x + 10}
    # Printing the catalog version itself carries none.
    assert await catalog_geometry_diff(session, v1) == {}


async def test_the_reference_is_the_newest_catalog_written_version(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = (await cs.install(session, K6121, as_new_key=None, prefix=None, actor="admin")).type
    v1 = await ts.current_version(session, t.id)
    g1 = ts.version_geometry(v1)
    # The catalog's revision 2 moves the top margin; the update writes a catalog version.
    _offer_newer_geometry(monkeypatch, K6121, margin_top=g1.margin_top / 100 + 1.0)
    await cs.apply_update(session, K6121, actor="admin")
    v2 = await ts.current_version(session, t.id)
    assert v2.version == v1.version + 1 and v2.margin_top == v1.margin_top + 100
    # Then a local edit of the pitch only.
    g2 = ts.version_geometry(v2)
    v3 = await ts.add_version(
        session, K6121, dataclasses.replace(g2, pitch_x=g2.pitch_x + 10), actor="admin",
        change_note="measured",
    )  # fmt: skip
    # Against revision 2 only the pitch differs; against revision 1 MT would appear too.
    assert await catalog_geometry_diff(session, v3) == {"PX": v2.pitch_x + 10}
