# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Invisible characters in free text (#204 note 1, 1.8.0)."""

from __future__ import annotations

import unicodedata

import pytest
from app.sheets.text import sanitise_text


@pytest.mark.parametrize("char", ["\u200b", "\u2060", "\ufeff"])
def test_zero_width_characters_removed(char: str) -> None:
    assert sanitise_text(f"flour{char}1 kg", max_len=120) == "flour1 kg"


def test_zero_width_space_removed() -> None:
    assert sanitise_text("a\u200bb", max_len=20) == "ab"


def test_word_joiner_removed() -> None:
    assert sanitise_text("a\u2060b", max_len=20) == "ab"


def test_bom_removed() -> None:
    assert sanitise_text("\ufeffname", max_len=20) == "name"


@pytest.mark.parametrize("code_point", [0xE0000, 0xE0001, 0xE0020, 0xE0041, 0xE007F])
def test_tag_characters_removed(code_point: int) -> None:
    assert sanitise_text(f"a{chr(code_point)}b", max_len=20) == "ab"


def test_tag_characters_removed_before_composition() -> None:
    """A tag between a letter and its combining mark must not keep them apart: strip first,
    compose (NFC) last."""
    result = sanitise_text("e\U000e0041\u0301", max_len=20)
    assert result == "\u00e9"  # one composed character, not "e" + combining acute
    assert unicodedata.is_normalized("NFC", result)


def test_tag_sequence_flag_loses_its_tags() -> None:
    flag = "\U0001f3f4" + "".join(chr(0xE0000 + c) for c in b"gbeng") + "\U000e007f"
    assert sanitise_text(flag, max_len=20) == "\U0001f3f4"


@pytest.mark.parametrize("sep", ["\u2028", "\u2029"])
def test_line_separator_becomes_newline_or_space(sep: str) -> None:
    assert sanitise_text(f"a{sep}b", max_len=20, multiline=True) == "a\nb"
    assert sanitise_text(f"a{sep}b", max_len=20) == "a b"


def test_nfc_applied() -> None:
    assert sanitise_text("e\u0301", max_len=20) == "\u00e9"


def test_length_is_counted_after_normalisation() -> None:
    assert sanitise_text("e\u0301" * 5, max_len=5) == "\u00e9" * 5


def test_zwj_and_soft_hyphen_kept() -> None:
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert sanitise_text(family, max_len=20) == family
    assert sanitise_text("a\u00adb", max_len=20) == "a\u00adb"


def test_required_field_empty_after_cleaning_is_refused() -> None:
    with pytest.raises(ValueError, match=r"^empty$"):
        sanitise_text("\u200b\u2060 \ufeff", max_len=20, required=True)


def test_optional_field_empty_after_cleaning_stays_empty() -> None:
    assert sanitise_text("\u200b\u2060", max_len=20) == ""


def test_clean_text_maps_a_required_empty_field_to_422() -> None:
    from app.sheets.errors import InvalidFieldError
    from app.sheets.type_service import clean_text

    with pytest.raises(InvalidFieldError) as ei:
        clean_text("name", "\u200b\u2060", 20, required=True)
    assert ei.value.detail == {"field": "name", "reason": "must not be empty"}
    assert clean_text("name", "\u200b", 20) == ""


def test_invisible_characters_do_not_split_a_whitespace_run() -> None:
    """A zero-width character between two parts of one run must not leave two spaces."""
    assert sanitise_text("a \n​ b", max_len=20) == "a b"
    assert sanitise_text("a\t⁠\n﻿b", max_len=20) == "a b"
    assert sanitise_text("a \n​ b", max_len=20, multiline=True) == "a \n b"
