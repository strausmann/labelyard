# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The public web address of an integration (#247, ADR 0033): rebase, links, payloads, redaction.

All addresses are invented ``example.invalid`` hosts.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from app.auth.dependencies import AuthContext
from app.integrations.links import (
    qr_payload_for,
    rebase,
    redact_for,
    redact_job_payload,
    web_link_for,
)
from app.schemas.label_data import LabelData
from app.settings_store import SETTINGS, InvalidSettingValueError
from app.settings_store.store import _validate

SNIPE = "http://snipe.internal.example.invalid"
SNIPE_WEB = "https://inventory.example.invalid/snipe"
GROCY = "http://grocy.internal.example.invalid:9283"


class _Settings:
    """The two settings ``links`` reads per integration, nothing else."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get(self, key: str) -> Any:
        return self._values.get(key, "")


def _settings(**web: str) -> _Settings:
    values: dict[str, Any] = {
        "integrations.snipeit.url": SNIPE,
        "integrations.grocy.url": GROCY,
    }
    for name, url in web.items():
        values[f"integrations.{name}.web_url"] = url
    return _Settings(values)


def _auth(scope: str) -> AuthContext:
    return AuthContext(source="api-key", scope=scope, api_key_id=uuid4(), ip="127.0.0.1")  # type: ignore[arg-type]


def _label(payload: str | None) -> LabelData:
    return LabelData(source_app="snipeit", title="Laptop", primary_id="A-1", qr_payload=payload)


# -- rebase -----------------------------------------------------------------------------


def test_rebase_onto_web_url() -> None:
    assert (
        rebase(f"{SNIPE}/hardware/42?x=1#top", SNIPE, "https://inventory.example.invalid")
        == "https://inventory.example.invalid/hardware/42?x=1#top"
    )


def test_web_url_with_path_prefix_keeps_the_prefix() -> None:
    assert (
        rebase(f"{SNIPE}/hardware/42", SNIPE, "https://inventory.example.invalid/snipe/")
        == "https://inventory.example.invalid/snipe/hardware/42"
    )


def test_trailing_slash_gives_no_double_slash() -> None:
    assert rebase(f"{SNIPE}/hardware/42", f"{SNIPE}/", f"{SNIPE_WEB}/") == (
        f"{SNIPE_WEB}/hardware/42"
    )
    assert rebase(SNIPE, f"{SNIPE}/", f"{SNIPE_WEB}/") == SNIPE_WEB


def test_base_prefix_needs_a_boundary() -> None:
    assert (
        rebase("http://h.example.invalid:8080/x", "http://h.example.invalid:80", SNIPE_WEB) is None
    )
    assert rebase(f"{SNIPE}evil/x", SNIPE, SNIPE_WEB) is None
    # The boundaries that do match: path, query, fragment and the end.
    assert rebase(f"{SNIPE}?q=1", SNIPE, SNIPE_WEB) == f"{SNIPE_WEB}?q=1"
    assert rebase(f"{SNIPE}#f", SNIPE, SNIPE_WEB) == f"{SNIPE_WEB}#f"


def test_payload_outside_the_internal_base_is_not_rewritten() -> None:
    assert rebase("https://elsewhere.example.invalid/hardware/42", SNIPE, SNIPE_WEB) is None
    settings = _settings(snipeit=SNIPE_WEB)
    other = "https://elsewhere.example.invalid/hardware/42"
    assert qr_payload_for("snipeit", "A-1", _label(other), settings) == other
    assert web_link_for("snipeit", other, settings) is None


def test_no_web_url_leaves_the_payload_unchanged() -> None:
    settings = _settings()
    payload = f"{SNIPE}/hardware/42"
    assert qr_payload_for("snipeit", "A-1", _label(payload), settings) == payload
    assert web_link_for("snipeit", payload, settings) is None


def test_web_url_set_rebases_the_printed_payload_and_the_link() -> None:
    settings = _settings(snipeit=SNIPE_WEB)
    payload = f"{SNIPE}/hardware/42"
    assert qr_payload_for("snipeit", "A-1", _label(payload), settings) == (
        f"{SNIPE_WEB}/hardware/42"
    )
    assert web_link_for("snipeit", payload, settings) == f"{SNIPE_WEB}/hardware/42"


def test_web_url_with_user_info_is_refused() -> None:
    with pytest.raises(InvalidSettingValueError):
        _validate(
            SETTINGS["integrations.snipeit.web_url"], "https://user:pw@inventory.example.invalid"
        )
    # An environment value bypasses the validator: it is not used at all.
    settings = _settings(snipeit="https://user:pw@inventory.example.invalid")
    payload = f"{SNIPE}/hardware/42"
    assert web_link_for("snipeit", payload, settings) is None
    assert qr_payload_for("snipeit", "A-1", _label(payload), settings) == payload


@pytest.mark.parametrize(
    "bad",
    ["ftp://inventory.example.invalid", "inventory.example.invalid", "https://" + "a" * 2050],
)
def test_web_url_must_be_a_short_http_url(bad: str) -> None:
    with pytest.raises(InvalidSettingValueError):
        _validate(SETTINGS["integrations.snipeit.web_url"], bad)
    assert web_link_for("snipeit", f"{SNIPE}/hardware/1", _settings(snipeit=bad)) is None


# -- redaction --------------------------------------------------------------------------


def test_redact_for_keeps_the_payload_for_admins() -> None:
    payload = f"{SNIPE}/hardware/42"
    assert redact_for(_auth("admin"), "snipeit", payload, _settings()) == payload


@pytest.mark.parametrize("scope", ["read", "print"])
def test_redact_for_non_admins(scope: str) -> None:
    payload = f"{SNIPE}/hardware/42"
    assert redact_for(_auth(scope), "snipeit", payload, _settings()) is None
    assert redact_for(_auth(scope), "snipeit", payload, _settings(snipeit=SNIPE_WEB)) == (
        f"{SNIPE_WEB}/hardware/42"
    )
    assert redact_for(_auth(scope), "snipeit", None, _settings()) is None


def test_redact_for_catches_an_internal_base_under_another_app_or_case() -> None:
    reader = _auth("read")
    # A manual label that carries Grocy's internal address.
    assert redact_for(reader, "manual", f"{GROCY}/product/7", _settings()) is None
    # Not at the start, other case: still the internal address.
    assert redact_for(reader, "manual", f"see {SNIPE.upper()}/x", _settings()) is None
    # Rebased, but the query still names the internal base.
    leaky = f"{SNIPE}/hardware/1?back={SNIPE}/"
    assert redact_for(reader, "snipeit", leaky, _settings(snipeit=SNIPE_WEB)) is None


def test_redact_for_keeps_payloads_without_an_internal_address() -> None:
    reader = _auth("read")
    assert redact_for(reader, "grocy", "grcy:p:7", _settings()) == "grcy:p:7"
    assert redact_for(reader, "manual", "https://example.invalid/x", _settings()) == (
        "https://example.invalid/x"
    )


def _job_payload() -> dict[str, Any]:
    return {
        "label_data": {
            "source_app": "manual",
            "qr_payload": f"{SNIPE}/locations/3",
            "items": [
                {"item": "A", "qr_payload": f"{SNIPE}/hardware/1"},
                {"item": "B", "qr_payload": None},
            ],
        },
        "detail": {
            "cells": [
                {
                    "cell": 1,
                    "label_data": {"source_app": "grocy", "qr_payload": f"{GROCY}/product/7"},
                }
            ],
            "id_cell": {
                "cell": 2,
                "label_data": {"source_app": "snipeit", "qr_payload": f"{SNIPE}/hardware/9"},
            },
        },
    }


def test_redact_job_payload_walks_every_nested_payload() -> None:
    original = _job_payload()
    out = redact_job_payload(_auth("read"), original, _settings(snipeit=SNIPE_WEB))
    assert out["label_data"]["qr_payload"] == f"{SNIPE_WEB}/locations/3"
    assert out["label_data"]["items"][0]["qr_payload"] == f"{SNIPE_WEB}/hardware/1"
    assert out["label_data"]["items"][1]["qr_payload"] is None
    assert out["detail"]["cells"][0]["label_data"]["qr_payload"] is None  # no Grocy web_url
    assert out["detail"]["id_cell"]["label_data"]["qr_payload"] == f"{SNIPE_WEB}/hardware/9"
    assert "internal" not in repr(out)
    # A copy: the stored payload is untouched.
    assert original == _job_payload()


def test_redact_job_payload_is_unchanged_for_admins() -> None:
    assert redact_job_payload(_auth("admin"), _job_payload(), _settings()) == _job_payload()


def test_redact_job_payload_tolerates_odd_shapes() -> None:
    odd: dict[str, Any] = {
        "label_data": "not a dict",
        "detail": {"cells": "nope", "id_cell": None},
    }
    assert redact_job_payload(_auth("read"), odd, _settings()) == odd
    assert redact_job_payload(_auth("read"), {}, _settings()) == {}


def test_every_integration_name_is_safe_against_the_real_store() -> None:
    """Paperless has a base address but no web address: the store has no such key."""
    from app.settings_store import SettingsStore

    store = SettingsStore()
    reader = _auth("read")
    assert redact_for(reader, "paperless", "https://example.invalid/x", store) == (
        "https://example.invalid/x"
    )
    assert web_link_for("paperless", "https://example.invalid/x", store) is None


def test_preview_qr_for_follows_who_may_print() -> None:
    from app.integrations.links import preview_qr_for

    payload = f"{SNIPE}/hardware/42"
    assert preview_qr_for(_auth("print"), "snipeit", payload, _settings()) == (payload, False)
    assert preview_qr_for(_auth("admin"), "snipeit", payload, _settings()) == (payload, False)
    sso = AuthContext(source="pangolin-sso", scope="read", api_key_id=None, ip="127.0.0.1")
    assert preview_qr_for(sso, "snipeit", payload, _settings()) == (payload, False)
    shown, withheld = preview_qr_for(_auth("read"), "snipeit", payload, _settings())
    assert (shown, withheld) == ("x" * len(payload), True)
    web = preview_qr_for(_auth("read"), "snipeit", payload, _settings(snipeit=SNIPE_WEB))
    assert web == (f"{SNIPE_WEB}/hardware/42", False)
    assert preview_qr_for(_auth("read"), "snipeit", None, _settings()) == (None, False)


def test_a_base_that_is_its_own_web_address_is_public() -> None:
    """Codex P2: ``url`` and ``web_url`` the same address: that address is not internal."""
    public = "https://inventory.example.invalid"
    settings = _Settings(
        {"integrations.snipeit.url": public, "integrations.snipeit.web_url": f"{public}/"}
    )
    payload = f"{public}/hardware/42"
    assert web_link_for("snipeit", payload, settings) == payload
    assert redact_for(_auth("read"), "snipeit", payload, settings) == payload
    # Another integration's internal base still counts.
    leaky = _Settings({**settings._values, "integrations.grocy.url": GROCY})
    assert redact_for(_auth("read"), "manual", f"{GROCY}/product/1", leaky) is None


def test_a_web_address_under_the_internal_base_is_not_rebased_twice() -> None:
    """Codex P2: ``web_url`` nested under ``url``: a printed (public) payload stays as it is."""
    base = "https://example.invalid"
    web = f"{base}/inventory"
    settings = _Settings({"integrations.snipeit.url": base, "integrations.snipeit.web_url": web})
    printed = qr_payload_for("snipeit", "A-1", _label(f"{base}/hardware/42"), settings)
    assert printed == f"{web}/hardware/42"
    assert redact_for(_auth("read"), "snipeit", printed, settings) == printed
    # A payload under the internal base but not under the web address is still rebased once.
    assert redact_for(_auth("read"), "snipeit", f"{base}/hardware/42", settings) == printed
    # The public prefix does not excuse an internal address after it.
    assert (
        redact_for(
            _auth("read"),
            "snipeit",
            f"{web}/x?next={GROCY}",
            _Settings({**settings._values, "integrations.grocy.url": GROCY}),
        )
        is None
    )


# -- fix round 1 ------------------------------------------------------------------------


def test_stored_payload_after_a_base_change_is_null_for_non_admins() -> None:
    """A job printed under an old base names no current base: not trusted, answered null."""
    old = "http://old-snipe.internal.example.invalid/hardware/42"
    assert redact_for(_auth("read"), "snipeit", old, _settings()) is None
    assert redact_for(_auth("read"), "snipeit", old, _settings(snipeit=SNIPE_WEB)) is None
    # An older web form after the web address moved is not current either.
    moved = "https://old-inventory.example.invalid/hardware/42"
    assert redact_for(_auth("read"), "snipeit", moved, _settings(snipeit=SNIPE_WEB)) is None
    assert redact_for(_auth("admin"), "snipeit", old, _settings()) == old


def test_stored_payload_of_a_cleared_integration_is_null() -> None:
    cleared = _Settings({"integrations.grocy.url": GROCY})  # Snipe-IT has no base any more
    payload = f"{SNIPE}/hardware/42"
    assert redact_for(_auth("read"), "snipeit", payload, cleared) is None


def test_grocy_codes_and_manual_labels_keep_their_payload() -> None:
    reader = _auth("read")
    assert redact_for(reader, "grocy", "grcy:p:7", _settings()) == "grcy:p:7"
    assert redact_for(reader, "manual", "https://example.invalid/m", _settings()) == (
        "https://example.invalid/m"
    )
    assert redact_for(reader, "hub", "https://hub.example.invalid/s/AT1-0001", _settings()) == (
        "https://hub.example.invalid/s/AT1-0001"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "https://pub.example.invalid/?a=1",
        "https://pub.example.invalid/#f",
        "https://pub.example.invalid?",
    ],
)
def test_web_url_with_query_or_fragment_is_refused(bad: str) -> None:
    with pytest.raises(InvalidSettingValueError):
        _validate(SETTINGS["integrations.snipeit.web_url"], bad)
    assert web_link_for("snipeit", f"{SNIPE}/hardware/1", _settings(snipeit=bad)) is None


def test_environment_web_url_loses_every_trailing_slash() -> None:
    settings = _settings(snipeit="https://pub.example.invalid//")
    assert web_link_for("snipeit", f"{SNIPE}/hardware/1", settings) == (
        "https://pub.example.invalid/hardware/1"
    )


@pytest.mark.parametrize(
    "payload",
    [
        "http://x@snipe.internal.example.invalid/h",
        "http://snipe%2Einternal%2Eexample%2Einvalid/h",
        "https://ok.example.invalid/?u=http%3A%2F%2Fsnipe.internal.example.invalid%2Fh",
        "http://SNIPE.internal.example.invalid./h",
        "http://snipe.internal.example.invalid:8443/h",
    ],
)
def test_internal_host_is_found_in_every_spelling(payload: str) -> None:
    assert redact_for(_auth("read"), "manual", payload, _settings()) is None


def test_unicode_and_punycode_hosts_are_the_same_host() -> None:
    uni = _Settings({"integrations.snipeit.url": "http://bücher.internal.example.invalid"})
    puny = _Settings({"integrations.snipeit.url": "http://xn--bcher-kva.internal.example.invalid"})
    reader = _auth("read")
    assert (
        redact_for(reader, "manual", "http://xn--bcher-kva.internal.example.invalid/x", uni) is None
    )
    assert redact_for(reader, "manual", "http://bücher.internal.example.invalid/x", puny) is None


def test_the_longer_prefix_wins() -> None:
    """The internal base lies under the web host: a payload under it is internal, not public."""
    settings = _Settings(
        {
            "integrations.snipeit.url": "https://inv.example.invalid/internal-api",
            "integrations.snipeit.web_url": "https://inv.example.invalid",
        }
    )
    payload = "https://inv.example.invalid/internal-api/hardware/1"
    assert redact_for(_auth("read"), "snipeit", payload, settings) == (
        "https://inv.example.invalid/hardware/1"
    )


def test_a_bracketed_ipv6_host_is_found() -> None:
    """Codex P2: an IPv6 base host (``[fd00::1]``) is found on another port, too."""
    settings = _Settings({"integrations.snipeit.url": "http://[fd00::1]:8080"})
    reader = _auth("read")
    assert redact_for(reader, "manual", "http://[fd00::1]:9090/hardware/1", settings) is None
    assert redact_for(reader, "manual", "http://u@[FD00::1]/x", settings) is None


# -- fix round 2 ------------------------------------------------------------------------


def test_redacting_a_job_builds_the_markers_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A job with 400 cells and listing rows: the settings are read into markers once per job,
    not once per code (a read key's job list must not stall the event loop)."""
    from app.integrations import links

    calls = 0
    original = links._build_context

    def counting(settings: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(settings)

    monkeypatch.setattr(links, "_build_context", counting)
    cell = {"label_data": {"source_app": "snipeit", "qr_payload": f"{SNIPE}/hardware/1"}}
    payload = {
        "label_data": {
            "source_app": "manual",
            "qr_payload": f"{SNIPE}/locations/1",
            "items": [{"item": str(i), "qr_payload": f"{GROCY}/product/{i}"} for i in range(50)],
        },
        "detail": {"cells": [dict(cell, cell=i) for i in range(400)], "id_cell": cell},
    }
    out = redact_job_payload(_auth("read"), payload, _settings(snipeit=SNIPE_WEB))
    assert out["detail"]["cells"][399]["label_data"]["qr_payload"] == f"{SNIPE_WEB}/hardware/1"
    assert calls == 1
    calls = 0
    redact_for(_auth("read"), "snipeit", f"{SNIPE}/hardware/1", _settings())
    assert calls == 1  # the single-code API builds its own context


@pytest.mark.parametrize("after", ["&y=1", ",", ";", '"', "'", ")", " next"])
def test_internal_host_followed_by_a_separator_is_found(after: str) -> None:
    payload = f"https://ok.example.invalid/?u=https://snipe.internal.example.invalid{after}"
    assert redact_for(_auth("read"), "manual", payload, _settings()) is None
