# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Guard: the image runs uvicorn with the pure-Python h11 parser and a bounded header event (#405).

h11 is chosen explicitly because its ``--h11-max-incomplete-event-size`` bounds the bytes of one
incomplete request-line or header block; ``--http auto`` would pick httptools (installed with
``uvicorn[standard]``), which takes no such setting. A reverse proxy in front stays recommended.
Measured with uvicorn: the bound applies to what stays buffered unfinished across reads, so a
header block that arrives complete in one read is not cut at 16 KiB.

Limit: this reads the text of the ``CMD`` in the Dockerfile; it does not start uvicorn.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


def _cmd() -> str:
    text = DOCKERFILE.read_text(encoding="utf-8")
    start = text.rindex("\nCMD ")
    return re.sub(r"\\\s*\n\s*", " ", text[start:])


def test_entrypoint_runs_h11_with_a_16_kib_event_bound() -> None:
    cmd = _cmd()
    assert re.search(r"--http h11(?=\s)", cmd), cmd
    assert re.search(r"--h11-max-incomplete-event-size 16384(?=\s|\")", cmd), cmd
