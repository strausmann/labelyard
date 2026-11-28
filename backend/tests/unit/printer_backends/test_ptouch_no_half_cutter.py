# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""PT models without a half cutter (PT-D600): the backend says so and never sends a half cut.

The PT-D600 has the PT-P750W's print head (128 pins, 180 dpi) and raster commands, so it prints
through ``ptouch.PTP750W``, but it has a full cutter only. A half cut sent to it switches the auto
cut off, so a batch would come out as one uncut strip.
"""

from __future__ import annotations

from typing import Any

import ptouch
import pytest
from app.models.tape import TapeSpec
from app.printer_backends.ptouch_backend import _PTOUCH_PRINTER_CLASSES, PTouchBackend
from app.printer_backends.snmp_helper import PreflightStatus
from app.services.status_block import MediaType
from PIL import Image


@pytest.fixture
def tape_24() -> TapeSpec:
    return TapeSpec(
        width_mm=24,
        media_type=MediaType.LAMINATED,
        print_area_pins=128,
        print_area_dots=128,
        bytes_per_raster=16,
        min_length_mm=4.4,
        max_length_mm=1000,
        cutter_min_length_mm=24.5,
    )


@pytest.fixture
def idle_24(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_preflight(*_a: Any, **_kw: Any) -> PreflightStatus:
        return PreflightStatus(hr_printer_status="idle", loaded_tape_mm=24, error_flags=[])

    monkeypatch.setattr("app.printer_backends.ptouch_backend.query_preflight", fake_preflight)


def test_d600_prints_through_the_p750w_class() -> None:
    assert _PTOUCH_PRINTER_CLASSES["PT-D600"] is ptouch.PTP750W
    assert PTouchBackend(host="192.0.2.10", model_id="PT-D600").dpi == 180


def test_d600_has_no_half_cutter() -> None:
    assert PTouchBackend(host="192.0.2.10", model_id="PT-D600").half_cut_supported is False


def test_other_pt_models_keep_their_half_cutter() -> None:
    assert PTouchBackend(host="192.0.2.10", model_id="PT-P750W").half_cut_supported is True
    assert PTouchBackend.half_cut_supported is True  # the class default is unchanged


async def test_print_image_on_d600_cuts_fully(
    monkeypatch: pytest.MonkeyPatch, idle_24: None, tape_24: TapeSpec
) -> None:
    captured: dict[str, Any] = {}

    def fake_print(*_a: Any, auto_cut: bool, half_cut: bool = False, **_kw: Any) -> None:
        captured.update(auto_cut=auto_cut, half_cut=half_cut)

    monkeypatch.setattr("app.printer_backends.ptouch_backend._ptouch_print", fake_print)
    backend = PTouchBackend(host="192.0.2.10", model_id="PT-D600")
    await backend.print_image(Image.new("1", (200, 128)), tape_24, auto_cut=True, half_cut=True)
    assert captured == {"auto_cut": True, "half_cut": False}


async def test_print_images_on_d600_cuts_fully(
    monkeypatch: pytest.MonkeyPatch, idle_24: None, tape_24: TapeSpec
) -> None:
    captured: dict[str, Any] = {}

    def fake_print_multi(
        *_a: Any, auto_cut: bool, half_cut: bool, high_resolution: bool, **_kw: Any
    ) -> None:
        captured.update(auto_cut=auto_cut, half_cut=half_cut)

    monkeypatch.setattr("app.printer_backends.ptouch_backend._ptouch_print_multi", fake_print_multi)
    backend = PTouchBackend(host="192.0.2.10", model_id="PT-D600")
    images = [Image.new("1", (200, 128)) for _ in range(2)]
    await backend.print_images(images, tape_24, half_cut=True)
    assert captured == {"auto_cut": True, "half_cut": False}


async def test_print_images_on_p750w_keeps_the_half_cut(
    monkeypatch: pytest.MonkeyPatch, idle_24: None, tape_24: TapeSpec
) -> None:
    captured: dict[str, Any] = {}

    def fake_print_multi(*_a: Any, half_cut: bool, **_kw: Any) -> None:
        captured["half_cut"] = half_cut

    monkeypatch.setattr("app.printer_backends.ptouch_backend._ptouch_print_multi", fake_print_multi)
    backend = PTouchBackend(host="192.0.2.10", model_id="PT-P750W")
    await backend.print_images([Image.new("1", (200, 128))], tape_24, half_cut=True)
    assert captured["half_cut"] is True
