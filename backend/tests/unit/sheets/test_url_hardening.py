# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""User info and mixed-script hosts in stored links (#204 note 2, 1.8.0)."""

from __future__ import annotations

import pytest
from app.integrations.links import check_web_url
from app.sheets.text import check_http_url, display_host

# A host that starts with a Cyrillic a (U+0430) followed by Latin letters: the classic homograph.
_CYRILLIC_A = "\u0430"
_MIXED_HOST = f"{_CYRILLIC_A}pple.example.invalid"
_MIXED_PUNY = _MIXED_HOST.encode("idna").decode("ascii")


@pytest.mark.parametrize(
    "url",
    [
        "https://user@example.invalid/a",
        "https://user:secret@example.invalid/a",
        "http://:secret@example.invalid/",
        "http://@example.invalid/",
    ],
)
def test_url_with_user_info_refused(url: str) -> None:
    with pytest.raises(ValueError, match="user info"):
        check_http_url(url)


def test_user_info_text_in_the_path_is_fine() -> None:
    url = "https://example.invalid/a@b"
    assert check_http_url(url) == url


def test_mixed_script_idn_refused() -> None:
    assert _MIXED_PUNY.startswith("xn--")
    for url in (f"https://{_MIXED_HOST}/x", f"https://{_MIXED_PUNY}/x"):
        with pytest.raises(ValueError, match="mix"):
            check_http_url(url)


def test_mixed_greek_and_latin_refused() -> None:
    host = "\u03b1bc.example.invalid"  # Greek alpha, then Latin letters
    with pytest.raises(ValueError, match="mix"):
        check_http_url(f"https://{host}/")


def test_single_script_idn_accepted() -> None:
    cyrillic = "https://\u043f\u0440\u0438\u043c\u0435\u0440.example.invalid/x"
    german = "https://b\u00fccher.example.invalid/x"
    greek = "https://\u03b1\u03b2\u03b3.example.invalid/x"
    for url in (cyrillic, german, greek):
        assert check_http_url(url) == url
        assert check_http_url(
            url.replace(url.split("/")[2], url.split("/")[2].encode("idna").decode("ascii"))
        )


def test_scripts_may_differ_between_labels() -> None:
    """Only a label that mixes scripts is a homograph; a Cyrillic label under a Latin domain is
    each label's own business."""
    url = "https://\u043f\u0440\u0438\u043c\u0435\u0440.example.invalid/"
    assert check_http_url(url) == url


def test_plain_ascii_host_untouched() -> None:
    assert check_http_url("http://192.0.2.7:8080/p?q=1") == "http://192.0.2.7:8080/p?q=1"


def test_broken_punycode_refused() -> None:
    with pytest.raises(ValueError, match="host"):
        check_http_url("https://xn--a-.example.invalid/")


def test_display_host_is_punycode() -> None:
    assert display_host("https://b\u00fccher.example.invalid/p") == "xn--bcher-kva.example.invalid"
    assert display_host("https://Example.Invalid:8443/p") == "example.invalid:8443"
    assert display_host("https://example.invalid/p") == "example.invalid"


def test_check_web_url_uses_the_shared_rules() -> None:
    assert check_web_url("https://example.invalid/base/") == "https://example.invalid/base"
    assert check_web_url("https://user@example.invalid") == ""
    assert check_web_url(f"https://{_MIXED_HOST}") == ""
    assert check_web_url("https://example.invalid/b?x=1") == ""  # web-url-only rule stays
    assert check_web_url("https://example.invalid/b#x") == ""


@pytest.mark.parametrize(
    "value",
    [f"https://{_MIXED_HOST}", f"https://{_MIXED_PUNY}", "https://u:p@example.invalid"],
)
def test_web_url_setting_refuses_a_look_alike_host(value: str) -> None:
    from app.settings_store import SETTINGS
    from app.settings_store.store import InvalidSettingValueError, _validate

    with pytest.raises(InvalidSettingValueError):
        _validate(SETTINGS["integrations.snipeit.web_url"], value)


@pytest.mark.parametrize(
    "url",
    [
        "https://exam​ple.invalid/",  # zero-width space in the host
        "https://example.invalid/a⁠b",  # word joiner in the path
        "https://example.invalid/﻿",
        "https://example.invalid/\U000e0041",
    ],
)
def test_format_characters_in_a_url_are_refused(url: str) -> None:
    with pytest.raises(ValueError, match=r"invisible|control"):
        check_http_url(url)


@pytest.mark.parametrize(
    "url",
    ["http://ex%0aample.invalid/", "http://ex%00ample.invalid/", "http://ex%7fample.invalid/"],
)
def test_percent_encoded_control_code_in_the_host_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="host"):
        check_http_url(url)


def test_percent_sign_in_the_path_is_fine() -> None:
    assert check_http_url("https://example.invalid/a%20b") == "https://example.invalid/a%20b"


def test_fullwidth_latin_mixed_with_cyrillic_is_refused() -> None:
    host = "ａа.example.invalid"  # fullwidth Latin a + Cyrillic a  # noqa: RUF001
    with pytest.raises(ValueError, match="mix"):
        check_http_url(f"https://{host}/")


@pytest.mark.parametrize("url", ["http://%D0%B5xample.invalid/", "http://ex%41mple.invalid/"])
def test_percent_in_the_host_is_refused(url: str) -> None:
    """A browser decodes ``%D0%B5`` to a Cyrillic letter; the mixed-script check must not see a
    different host than the browser does, so no host may carry a ``%`` at all."""
    with pytest.raises(ValueError, match="host"):
        check_http_url(url)
