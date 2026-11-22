# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The public web address of an integration: links, printed QR payloads, redaction (ADR 0033).

``integrations.<name>.url`` is the address the hub uses to reach an app; it is often internal
(a container name, a LAN host). ``integrations.<name>.web_url`` is the address people open and
scan (#247). This module is the one place that moves a URL from the first onto the second:

- :func:`web_link_for` is the "Open in …" link of a lookup: the rebased URL, or ``None``
  without a web address;
- :func:`qr_payload_for` is the code every print path prints for a looked-up item: rebased once
  a web address is set, unchanged otherwise;
- :func:`redact_for` / :func:`redact_job_payload` keep internal base addresses out of every
  answer to a caller that is not an admin.

The print paths are services without a request, so the runtime settings are bound once at
startup (:func:`bind_settings`, like ``lookup_gate.bind_batch_deadline``); routes pass
``request.app.state.settings_store`` directly.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from app.integrations.urls import base_url
from app.settings_store.registry import INTEGRATIONS, SETTINGS

if TYPE_CHECKING:
    from app.auth.dependencies import AuthContext
    from app.schemas.label_data import LabelData

#: The longest web address accepted (the settings validator's bound for every URL setting).
MAX_WEB_URL_LENGTH = 2048

#: What may follow the internal base in a URL that is rebased: the base is a whole prefix only.
_BOUNDARY = ("/", "?", "#")


class SettingsLike(Protocol):
    """What this module reads: ``get(key)`` of the settings store."""

    def get(self, key: str) -> Any: ...


_bound: SettingsLike | None = None


def bind_settings(settings: SettingsLike | None) -> SettingsLike | None:
    """Use ``settings`` for the print paths (the store, at startup); returns the one it replaces."""
    global _bound
    previous, _bound = _bound, settings
    return previous


def bound_settings() -> SettingsLike | None:
    """The settings bound at startup, or ``None``: no web address, payloads stay as they are."""
    return _bound


def web_url_key(name: str) -> str:
    return f"integrations.{name}.web_url"


def check_web_url(raw: str) -> str:
    """``raw`` when it is usable as a web address, else ``""``.

    The settings validator refuses a bad value on write; an environment value bypasses it
    (``LABELYARD_<NAME>_WEB_URL``), so the same rules are applied again here: http or https,
    a host, no user info, no whitespace, at most :data:`MAX_WEB_URL_LENGTH` characters. Task 6b
    of 1.8.0 replaces this local check with ``check_http_url`` once that is tightened.
    """
    text = raw.strip()
    if not text or len(text) > MAX_WEB_URL_LENGTH or any(c.isspace() for c in text):
        return ""
    try:
        parts = urlsplit(text)
        _ = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    if parts.username is not None or parts.password is not None:
        return ""
    return text


def _strip_one_slash(url: str) -> str:
    return url[:-1] if url.endswith("/") else url


def _remainder(url: str, internal_base: str) -> str | None:
    """What follows ``internal_base`` in ``url`` (path, query, fragment), or ``None``."""
    base = _strip_one_slash(internal_base)
    if not base or not url.startswith(base):
        return None
    rest = url[len(base) :]
    if rest and not rest.startswith(_BOUNDARY):
        return None  # ``http://h:80`` is no prefix of ``http://h:8080/x``
    return rest


def rebase(url: str, internal_base: str, web_url: str) -> str | None:
    """``url`` moved from ``internal_base`` onto ``web_url``, or ``None`` when it is outside it.

    One trailing slash is stripped from both bases. The internal base matches only when it is
    followed by ``/``, ``?``, ``#`` or the end of ``url``; path, query and fragment are kept.
    """
    rest = _remainder(url, internal_base)
    web = _strip_one_slash(web_url)
    if rest is None or not web:
        return None
    return web + rest


def _internal_base(name: str, settings: SettingsLike) -> str:
    try:
        return base_url(str(settings.get(f"integrations.{name}.url") or ""))
    except ValueError:
        return ""  # an invalid environment URL disables the integration (live_config)


def _web_base(name: str, settings: SettingsLike) -> str:
    key = web_url_key(name)
    if key not in SETTINGS:  # an integration without looked-up items has no web address
        return ""
    return check_web_url(str(settings.get(key) or ""))


def web_link_for(app: str, internal_url: str | None, settings: SettingsLike | None) -> str | None:
    """The link shown to people for ``internal_url``: rebased onto ``web_url``, else ``None``."""
    if internal_url is None or settings is None or app not in INTEGRATIONS:
        return None
    return rebase(internal_url, _internal_base(app, settings), _web_base(app, settings))


def qr_payload_for(
    app: str, item_id: str, label: LabelData, settings: SettingsLike | None
) -> str | None:
    """The QR payload printed for the item ``item_id`` of ``app`` (ADR 0033).

    The plugin built ``label.qr_payload`` from the internal base; once ``web_url`` is set and
    the payload lies under the internal base, the rebased payload is printed. Otherwise the
    payload is returned unchanged, so nothing printed changes until the operator sets one.
    ``item_id`` names the item for the reader; the payload already carries it.
    """
    del item_id
    payload = label.qr_payload
    if payload is None:
        return None
    rebased = web_link_for(app, payload, settings)
    return payload if rebased is None else rebased


def printed_label(
    app: str, item_id: str, label: LabelData, settings: SettingsLike | None
) -> LabelData:
    """``label`` with the payload :func:`qr_payload_for` decides; every print path uses it."""
    payload = qr_payload_for(app, item_id, label, settings)
    if payload == label.qr_payload:
        return label
    return label.model_copy(update={"qr_payload": payload})


def _markers(settings: SettingsLike) -> list[str]:
    """Case-folded strings that betray an internal base: the base itself and its ``//host``.

    The host is left out when the integration's web address uses the same host, and the base
    altogether when it is the web address itself: it is public then.
    """
    found: list[str] = []
    for name in INTEGRATIONS:
        base = _internal_base(name, settings)
        if not base:
            continue
        web = _web_base(name, settings)
        if web and _strip_one_slash(web).casefold() == base.casefold():
            continue  # the operator declared this base public: it is its own web address
        found.append(base.casefold())
        netloc = urlsplit(base).netloc.casefold()
        if netloc and (not web or urlsplit(web).netloc.casefold() != netloc):
            found.append("//" + netloc)
    return found


def redact_for(
    auth: AuthContext, app: str | None, payload: str | None, settings: SettingsLike | None
) -> str | None:
    """``payload`` as a caller with ``auth`` may see it (ADR 0033).

    Admins see it unchanged. For anyone else a payload under an internal base address (of
    ``app`` first, then of any other integration: a manual label can carry any of them) is
    answered in its web form, or ``None`` when that integration has no web address; a payload
    that still names an internal base anywhere (another case, inside a query) is ``None``. A
    payload without an internal address (a manual URL, a Grocy code) stays.
    """
    if auth.scope == "admin" or payload is None:
        return payload
    if settings is None:
        return None  # nothing to compare against: fail closed
    names = [app] if app in INTEGRATIONS else []
    names += [n for n in INTEGRATIONS if n != app]
    candidate: str | None = payload
    checked = payload  # the part that must name no internal base (a public prefix is exempt)
    for name in names:
        web = _strip_one_slash(_web_base(name, settings))
        public_rest = _remainder(payload, web) if web else None
        if public_rest is not None:
            # Already public (a printed payload): never rebased again, even when the web
            # address lies under the internal base (``https://h`` and ``https://h/inv``).
            checked = public_rest
            break
        rest = _remainder(payload, _internal_base(name, settings))
        if rest is not None:
            candidate = web + rest if web else None
            checked = rest
            break
    if candidate is None:
        return None
    folded = checked.casefold()
    if any(marker in folded for marker in _markers(settings)):
        return None
    return candidate


def may_print(auth: AuthContext) -> bool:
    """Whether ``auth`` could print an item and so read its code from the paper anyway.

    A key or session with ``print`` or ``admin`` scope; an SSO user, who is granted every scope
    a route asks for (``require_scope``); the Basic bypass unless it is limited to ``read``.
    """
    if auth.source == "pangolin-sso":
        return True
    if auth.source == "pangolin-bypass":
        from app.config import get_settings

        return not get_settings().pangolin_bypass_scope_downgrade
    return auth.scope in ("print", "admin")


def qr_stand_in(payload: str) -> str:
    """A code of the same byte length as ``payload`` and nothing in it (ADR 0033).

    Lowercase letters are encoded in byte mode like an address, so the stand-in needs the same
    QR version: a preview keeps the size and the fit of the printed code.
    """
    return "x" * len(payload.encode("utf-8"))


def preview_qr_for(
    auth: AuthContext, app: str, payload: str | None, settings: SettingsLike | None
) -> tuple[str | None, bool]:
    """The code a preview draws for the printed ``payload`` of an item, and whether it is withheld.

    Every preview that draws an integration item's QR uses this (the tape preview and the sheet
    preview, ADR 0033). A caller that may print sees the printed code: it could print the item
    and read the paper. Anyone else gets :func:`redact_for` (the web form), or, while there is
    none, :func:`qr_stand_in` with ``True`` so the answer can say so.
    """
    if payload is None or may_print(auth):
        return payload, False
    shown = redact_for(auth, app, payload, settings)
    if shown is None:
        return qr_stand_in(payload), True
    return shown, False


def _redact_label(auth: AuthContext, label: Any, settings: SettingsLike | None) -> None:
    """Redact ``label["qr_payload"]`` and every ``label["items"][i]["qr_payload"]`` in place."""
    if not isinstance(label, dict):
        return
    app = label.get("source_app")
    app = app if isinstance(app, str) else None
    if "qr_payload" in label:
        value = label["qr_payload"]
        text = value if isinstance(value, str) else None
        label["qr_payload"] = redact_for(auth, app, text, settings)
    items = label.get("items")
    if isinstance(items, list):
        for row in items:
            if isinstance(row, dict) and "qr_payload" in row:
                value = row["qr_payload"]
                text = value if isinstance(value, str) else None
                row["qr_payload"] = redact_for(auth, app, text, settings)


def redact_job_payload(
    auth: AuthContext, payload: dict[str, Any], settings: SettingsLike | None
) -> dict[str, Any]:
    """A copy of a job ``payload`` with every ``qr_payload`` passed through :func:`redact_for`.

    Walks the tape ``label_data``, ``detail.cells[].label_data``, ``detail.id_cell.label_data``
    and the ``items[]`` rows of each. The stored payload is never changed. A new place for label
    data in a job payload is added here, so every answer that carries a job stays covered.
    """
    out = copy.deepcopy(payload)
    if auth.scope == "admin":
        return out
    _redact_label(auth, out.get("label_data"), settings)
    detail = out.get("detail")
    if isinstance(detail, dict):
        cells = detail.get("cells")
        if isinstance(cells, list):
            for cell in cells:
                if isinstance(cell, dict):
                    _redact_label(auth, cell.get("label_data"), settings)
        id_cell = detail.get("id_cell")
        if isinstance(id_cell, dict):
            _redact_label(auth, id_cell.get("label_data"), settings)
    return out
