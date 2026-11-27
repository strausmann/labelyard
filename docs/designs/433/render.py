#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Render the QR vs Data Matrix comparison mockup for issue #433.

Both symbols are real: generated with zxing-cpp, then decoded once to prove they
are valid. Module sizes are computed from the actual symbol dimensions, not guessed.

Usage (in a throwaway venv):
    pip install zxing-cpp pillow numpy
    python docs/designs/433/render.py [output.png]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import zxingcpp
from PIL import Image, ImageDraw, ImageFont

PAYLOAD = "https://inv.example.invalid/t/K7D2QX"
LABEL_TEXT = "Shelf A · Box 3"

PX_PER_MM = 12  # true proportions: 1 mm = 12 px
SS = 4  # supersampling factor for smooth edges
WIDTH = 1200
MARGIN = 40
COL_MM = 45  # label length shown per column
COL_W = COL_MM * PX_PER_MM
COL_X = (MARGIN, WIDTH - MARGIN - COL_W)

# (tape width mm, printable code-area height mm) -- Brother TZe approximations
TAPES = ((6, 3.5), (9, 6.5), (12, 9.0))
MIN_MODULE_MM = 0.25  # below this a phone camera or handheld scanner struggles
QUIET = {"QR": 4, "DM": 1}  # quiet zone in modules, per symbology spec

BG = (238, 240, 243)
INK = (20, 24, 28)
MUTED = (88, 98, 110)
TAPE_EDGE = (160, 168, 178)
GUIDE = (196, 204, 214)
OK = (15, 118, 110)
BAD = (180, 35, 24)

FONT_DIR = Path("/usr/share/fonts/truetype/liberation")
REG = FONT_DIR / "LiberationSans-Regular.ttf"
BOLD = FONT_DIR / "LiberationSans-Bold.ttf"


def font(path: Path, px: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), round(px * SS))


def make_symbols() -> dict[str, np.ndarray]:
    """Return {'QR': matrix, 'DM': matrix} as boolean arrays (True = dark module)."""
    qr = zxingcpp.create_barcode(PAYLOAD, zxingcpp.BarcodeFormat.QRCode, ec_level="M")
    dm = zxingcpp.create_barcode(PAYLOAD, zxingcpp.BarcodeFormat.DataMatrix, force_square=True)
    out = {}
    for key, bc in (("QR", qr), ("DM", dm)):
        img = np.asarray(bc.to_image(scale=1, add_quiet_zones=False))
        out[key] = img < 128
    return out


def verify(symbols: dict[str, np.ndarray]) -> dict[str, str]:
    """Decode a crisp rendering of each symbol; raise if it does not round-trip."""
    result = {}
    for key, fmt in (("QR", zxingcpp.BarcodeFormat.QRCode), ("DM", zxingcpp.BarcodeFormat.DataMatrix)):
        m = symbols[key]
        q = QUIET[key]
        px = 10
        n = m.shape[0]
        canvas = np.full(((n + 2 * q) * px,) * 2, 255, np.uint8)
        for y, x in zip(*np.nonzero(m)):
            canvas[(y + q) * px:(y + q + 1) * px, (x + q) * px:(x + q + 1) * px] = 0
        found = zxingcpp.read_barcodes(canvas, formats=fmt)
        if len(found) != 1 or found[0].text != PAYLOAD:
            raise SystemExit(f"{key} did not decode back to the payload: {found}")
        result[key] = f"{found[0].format.name} decoded OK ({n}x{n} modules)"
    return result


def draw_symbol(d: ImageDraw.ImageDraw, m: np.ndarray, key: str, x: float, y: float, side: float) -> float:
    """Draw symbol (with quiet zone) inside a square of `side` px at x, y. Returns module px."""
    n = m.shape[0]
    q = QUIET[key]
    mod = side / (n + 2 * q)
    for r, c in zip(*np.nonzero(m)):
        x0 = x + (c + q) * mod
        y0 = y + (r + q) * mod
        d.rectangle(
            [round(x0 * SS), round(y0 * SS), round((x0 + mod) * SS) - 1, round((y0 + mod) * SS) - 1],
            fill=INK,
        )
    return mod


def wrap(d: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, max_w: float) -> list[str]:
    lines, cur = [], ""
    for word in text.split(" "):
        trial = f"{cur} {word}".strip()
        if d.textlength(trial, font=f) <= max_w * SS:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + [cur]


def render(symbols: dict[str, np.ndarray], out: Path) -> list[dict]:
    rows_info: list[dict] = []
    row_gap, head_h, cap_h = 34, 30, 58
    top = 262
    total_h = top + sum(head_h + t * PX_PER_MM + cap_h + row_gap for t, _ in TAPES) + 90
    img = Image.new("RGB", (WIDTH * SS, total_h * SS), BG)
    d = ImageDraw.Draw(img)

    def text(xy, s, f, fill=INK):
        d.text((xy[0] * SS, xy[1] * SS), s, font=f, fill=fill)

    text((MARGIN, 34), "QR vs Data Matrix on narrow tape", font(BOLD, 36))
    text((MARGIN, 90), "Same payload; Data Matrix needs fewer modules, so each module is larger on the same tape.",
         font(REG, 18), MUTED)

    heads = ("QR code", "Data Matrix (ECC 200)")
    for x, h in zip(COL_X, heads):
        text((x, 150), h, font(BOLD, 22))

    # mm ruler above each column
    ry = 214
    f_tick = font(REG, 16)
    for x in COL_X:
        d.line([(x * SS, ry * SS), ((x + COL_W) * SS, ry * SS)], fill=MUTED, width=SS)
        for mm in range(COL_MM + 1):
            tx = x + mm * PX_PER_MM
            tl = 14 if mm % 5 == 0 else 7
            d.line([(tx * SS, ry * SS), (tx * SS, (ry - tl) * SS)], fill=MUTED, width=SS)
            if mm % 10 == 0:
                s = f"{mm} mm" if mm == 0 else str(mm)
                text((tx + 4, ry - 36), s, f_tick, MUTED)

    y = top
    f_row = font(BOLD, 19)
    f_cap = font(BOLD, 17)
    f_ver = font(REG, 17)
    for tape_mm, code_mm in TAPES:
        tape_h = tape_mm * PX_PER_MM
        code_px = code_mm * PX_PER_MM
        text((MARGIN, y), f"{tape_mm} mm tape · code area {code_mm:g} mm high", f_row)
        ty = y + head_h
        info = {"tape_mm": tape_mm, "code_mm": code_mm}
        for key, x in (("QR", COL_X[0]), ("DM", COL_X[1])):
            d.rectangle([x * SS, ty * SS, (x + COL_W) * SS, (ty + tape_h) * SS],
                        fill=(255, 255, 255), outline=TAPE_EDGE, width=2 * SS)
            # printable area guides
            pad = (tape_h - code_px) / 2
            for gy in (ty + pad, ty + pad + code_px):
                d.line([((x + 2) * SS, gy * SS), ((x + COL_W - 2) * SS, gy * SS)], fill=GUIDE, width=SS)
            cx = x + 16
            mod_px = draw_symbol(d, symbols[key], key, cx, ty + pad, code_px)
            n = symbols[key].shape[0]
            mod_mm = code_mm / (n + 2 * QUIET[key])
            ok = mod_mm >= MIN_MODULE_MM
            # label text, sized relative to tape height
            fs = tape_h * 0.28
            f_lab = font(BOLD, fs)
            text((cx + code_px + 16, ty + tape_h / 2 - fs * 0.62), LABEL_TEXT, f_lab)
            name = "QR" if key == "QR" else "Data Matrix"
            text((x, ty + tape_h + 10), f"{name} · {n} × {n} modules", f_cap)
            verdict = "scannable" if ok else "too small to scan"
            text((x, ty + tape_h + 33), f"{mod_mm:.2f} mm per module · {verdict}", f_ver, OK if ok else BAD)
            info[key] = {"modules": n, "module_mm": mod_mm, "ok": ok}
        rows_info.append(info)
        y = ty + tape_h + cap_h + row_gap

    f_note = font(REG, 16)
    note = (f"Payload {PAYLOAD}. QR: error correction M, quiet zone 4 modules. Data Matrix: ECC 200, square, "
            f"quiet zone 1 module. Module size = code area ÷ (modules + quiet zone). Scannable means "
            f"at least {MIN_MODULE_MM:.2f} mm per module. Drawn at 1 mm = {PX_PER_MM} px.")
    for i, line in enumerate(wrap(d, note, f_note, WIDTH - 2 * MARGIN)):
        text((MARGIN, y - 4 + i * 23), line, f_note, MUTED)

    final = img.resize((WIDTH, total_h), Image.LANCZOS)
    final.save(out, optimize=True)
    return rows_info


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("datamatrix-vs-qr.png")
    symbols = make_symbols()
    for key, status in verify(symbols).items():
        print(f"{key}: {status}")
    for info in render(symbols, out):
        for key in ("QR", "DM"):
            s = info[key]
            print(f"{info['tape_mm']} mm {key}: {s['modules']}x{s['modules']}, "
                  f"{s['module_mm']:.3f} mm/module, {'scannable' if s['ok'] else 'too small'}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
