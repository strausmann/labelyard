# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""#405 fix round 1, Q5: the configuration reference only says "also set in the web UI" for a
setting the web UI shows. Settings that live in the settings store but have no form field yet
say "settings API or import" instead (``API_ONLY`` in ``website/scripts/gen-config-reference.py``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PAGES = {
    "en": (
        REPO / "website/src/content/docs/reference/configuration.md",
        "Also set in the web UI",
        "settings API",
    ),
    "de": (
        REPO / "website/src/content/docs/de/reference/configuration.md",
        "Auch in der Weboberfläche einstellbar",
        "Einstellungs-API",
    ),
}
API_ONLY = ("LABELYARD_LOOKUP_BATCH_DEADLINE_S", "LABELYARD_RENDER_MAX_CONCURRENT")


def _section(text: str, name: str) -> str:
    m = re.search(rf"^### `{name}`\n(.*?)(?=^### |\Z)", text, re.M | re.S)
    assert m, f"{name} has no section"
    return m.group(1)


@pytest.mark.parametrize("lang", sorted(PAGES))
@pytest.mark.parametrize("name", API_ONLY)
def test_api_only_settings_do_not_claim_the_web_ui(lang: str, name: str) -> None:
    path, ui_word, api_word = PAGES[lang]
    section = _section(path.read_text(encoding="utf-8"), name)
    assert ui_word not in section, section
    assert api_word in section, section


@pytest.mark.parametrize("lang", sorted(PAGES))
def test_a_setting_the_ui_shows_keeps_its_claim(lang: str) -> None:
    """(guard) the exception does not swallow the normal case."""
    path, ui_word, _ = PAGES[lang]
    assert ui_word in _section(path.read_text(encoding="utf-8"), "LABELYARD_JOB_RETENTION_DAYS")
