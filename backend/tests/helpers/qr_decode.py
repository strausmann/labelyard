# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read the QR codes of a rendered label image (#247): what a phone would scan."""

from __future__ import annotations

import io

import zxingcpp
from PIL import Image


def decode_qr(png: bytes) -> list[str]:
    """The text of every QR code in ``png``, in the order zxing finds them.

    The label is drawn at printer resolution with a narrow quiet zone; a white border is added
    so the reader finds the finder patterns at the image edge too.
    """
    image = Image.open(io.BytesIO(png)).convert("L")
    framed = Image.new("L", (image.width + 40, image.height + 40), 255)
    framed.paste(image, (20, 20))
    results = zxingcpp.read_barcodes(framed, formats=zxingcpp.BarcodeFormat.QRCode)
    return [r.text for r in results]
