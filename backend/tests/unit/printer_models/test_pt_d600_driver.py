# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The PT-D600 driver: the PT-P750W driver with its own model id and PJL signature.

The PT-D600 is USB only; it reaches the hub through a network bridge
(``examples/usb-bridge/``), which reports the model as ``MDL:PT-D600``.
"""

from __future__ import annotations

from typing import Any, cast

from app.printer_backends.mock_backend import MockPrinterBackend
from app.printer_models.base import PrinterModel
from app.printer_models.pt import PTD600Driver, PTP750WDriver
from app.printer_models.registry import ModelRegistry


def test_constants() -> None:
    assert PTD600Driver.model_id == "PT-D600"
    assert PTD600Driver.pjl_signatures == ["PT-D600"]
    assert PTD600Driver.snmp_model_oid_value_substr == "PT-D600"
    assert PTD600Driver.dpi == (180, 180)
    assert PTD600Driver.print_head_pins == 128


def test_it_is_the_p750w_driver_otherwise() -> None:
    assert issubclass(PTD600Driver, PTP750WDriver)
    driver = PTD600Driver(backend=cast(Any, MockPrinterBackend(host="192.0.2.10")))
    assert driver.describe_tape(24).label == "24mm TZe"


def test_registry_tells_the_d600_and_the_p750w_apart() -> None:
    ModelRegistry.register(cast("type[PrinterModel]", PTP750WDriver))
    ModelRegistry.register(cast("type[PrinterModel]", PTD600Driver))
    d600 = "MFG:Brother;CMD:PT-CBP;MDL:PT-D600;CLS:PRINTER;"
    p750w = "MFG:Brother;CMD:PJL;MDL:PT-P750W;CLS:PRINTER;"
    assert ModelRegistry.find_by_pjl(d600) is PTD600Driver
    assert ModelRegistry.find_by_pjl(p750w) is PTP750WDriver
    assert ModelRegistry.find_by_model_id("PT-D600") is PTD600Driver
