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

import contextlib
import copy
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import unquote, urlsplit

from app.integrations.urls import base_url
from app.schemas.grocy_label import is_grocycode
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
    a host, no user info, no query or fragment, no whitespace, at most
    :data:`MAX_WEB_URL_LENGTH` characters; every trailing slash is stripped, like the validator
    does. Task 6b of 1.8.0 replaces this local check with ``check_http_url`` once that is
    tightened.
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
    if "?" in text or "#" in text:  # a base address carries no query and no fragment
        return ""
    return text.rstrip("/")


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


def _host_forms(host: str) -> set[str]:
    """A host in every spelling that names it: lower case, no trailing dot, Unicode and IDNA."""
    plain = host.casefold().rstrip(".")
    forms = {plain}
    with contextlib.suppress(UnicodeError):
        forms.add(plain.encode("idna").decode("ascii"))
    with contextlib.suppress(UnicodeError):
        forms.add(plain.encode("ascii").decode("idna"))
    return {f for f in forms if f}


@dataclass(frozen=True)
class _Context:
    """Everything redaction reads from the settings, read once per job answer (ADR 0033).

    ``bases[name]`` is ``(internal base, web address)``; ``marker_bases`` and ``marker_hosts``
    betray an internal base (see :func:`_build_context`). Building it reads the settings, encodes
    IDNA and compiles patterns, so it is built once per :func:`redact_job_payload` call and
    reused for every code of the job, not once per code.
    """

    bases: dict[str, tuple[str, str]]
    marker_bases: tuple[str, ...]
    marker_hosts: tuple[re.Pattern[str], ...]


#: What may follow an internal host for it to count: a port, a path, query or fragment, a
#: query separator, punctuation that ends a URL in text, whitespace, or the end.
_HOST_END = r"(?=[:/?#\\&,;\"')\s]|$)"


def _build_context(settings: SettingsLike) -> _Context:
    """Read the bases once and derive what betrays them: the base itself, and its host in any
    spelling.

    The host counts wherever it follows ``//`` or ``@`` (user info), with or without a port, a
    trailing dot or the brackets of an IPv6 address. It is left out when the integration's web
    address uses the same host, and the base altogether when it is the web address itself: it
    is public then.
    """
    bases = {
        name: (_internal_base(name, settings), _web_base(name, settings)) for name in INTEGRATIONS
    }
    marker_bases: list[str] = []
    marker_hosts: list[re.Pattern[str]] = []
    for base, web in bases.values():
        if not base:
            continue
        if web and web.casefold() == base.casefold():
            continue  # the operator declared this base public: it is its own web address
        marker_bases.append(base.casefold())
        host = urlsplit(base).hostname or ""
        web_host = (urlsplit(web).hostname or "") if web else ""
        if not host or (web_host and _host_forms(web_host) & _host_forms(host)):
            continue
        for form in _host_forms(host):
            # An IPv6 host stands in brackets in a URL (``urlsplit`` drops them).
            marker_hosts.append(
                re.compile(r"(?://|@)\[?" + re.escape(form) + r"\]?\.?" + _HOST_END)
            )
    return _Context(bases, tuple(marker_bases), tuple(marker_hosts))


def _names_internal(text: str, ctx: _Context) -> bool:
    """Whether ``text`` (also percent-decoded, twice) names an internal base or host."""
    seen = {text}
    current = text
    for _ in range(2):
        current = unquote(current)
        seen.add(current)
    for variant in seen:
        folded = variant.casefold()
        if any(b in folded for b in ctx.marker_bases):
            return True
        if any(h.search(folded) for h in ctx.marker_hosts):
            return True
    return False


def _longest_prefix(payload: str, names: list[str], ctx: _Context) -> tuple[str, str, str] | None:
    """``(kind, name, rest)`` of the longest base ``payload`` lies under (``web`` or ``internal``).

    The longer prefix wins: an internal base under the web host (``https://h/api`` below
    ``https://h``) is internal, a web address under the internal base is public.
    """
    best: tuple[int, str, str, str] | None = None
    for name in names:
        internal, web = ctx.bases.get(name, ("", ""))
        for kind, base in (("web", web), ("internal", internal)):
            rest = _remainder(payload, base) if base else None
            if rest is not None:
                length = len(_strip_one_slash(base))
                if best is None or length > best[0]:
                    best = (length, kind, name, rest)
    return None if best is None else best[1:]


def _lookup_apps() -> list[str]:
    """The integrations whose items are looked up and printed: those with a web address."""
    return [n for n in INTEGRATIONS if web_url_key(n) in SETTINGS]


def redact_for(
    auth: AuthContext, app: str | None, payload: str | None, settings: SettingsLike | None
) -> str | None:
    """``payload`` as a caller with ``auth`` may see it (ADR 0033).

    Admins see it unchanged. For anyone else:

    - a label of a looked-up integration (``app`` Snipe-IT, Grocy, Spoolman) is trusted only
      under that integration's **current** addresses: under its web address it stays, under its
      base it is answered in web form (``None`` without one); anything else, such as a job
      printed under an earlier base or an integration that was cleared since, is ``None``. A
      Grocy code (``grcy:…``) is not an address and stays;
    - any other label (``manual``, ``hub``, …) is rebased the same way when it lies under some
      integration's base (the longer prefix wins), and otherwise stays.

    In both cases a result that still names an internal base or host anywhere after its public
    prefix (another case, user info, a percent-encoded query, IDNA) is ``None``. One code: the
    settings are read for this call; a job answer uses :func:`redact_job_payload`, which reads
    them once for all its codes.
    """
    if auth.scope == "admin" or payload is None:
        return payload
    if settings is None:
        return None  # nothing to compare against: fail closed
    return _redact(app, payload, _build_context(settings))


def _redact(app: str | None, payload: str, ctx: _Context) -> str | None:
    """:func:`redact_for` for a caller that is not an admin, with the settings already read."""
    strict = app in _lookup_apps()
    if strict and app == "grocy" and is_grocycode(payload):
        return payload
    names = [app] if strict and app is not None else list(INTEGRATIONS)
    match = _longest_prefix(payload, names, ctx)
    if match is None:
        if strict:
            return None  # not under any current address of its own integration
        candidate, checked = payload, payload
    else:
        kind, name, rest = match
        if kind == "web":
            candidate, checked = payload, rest  # already public: never rebased again
        else:
            web = ctx.bases[name][1]
            if not web:
                return None
            candidate, checked = web + rest, rest
    if _names_internal(checked, ctx):
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


def _redact_label(label: Any, ctx: _Context | None) -> None:
    """Redact ``label["qr_payload"]`` and every ``label["items"][i]["qr_payload"]`` in place.

    ``ctx`` ``None`` (no settings) answers every code as ``None``: fail closed.
    """
    if not isinstance(label, dict):
        return
    app = label.get("source_app")
    app = app if isinstance(app, str) else None

    def one(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        return None if ctx is None else _redact(app, value, ctx)

    if "qr_payload" in label:
        label["qr_payload"] = one(label["qr_payload"])
    items = label.get("items")
    if isinstance(items, list):
        for row in items:
            if isinstance(row, dict) and "qr_payload" in row:
                row["qr_payload"] = one(row["qr_payload"])


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
    ctx = None if settings is None else _build_context(settings)  # once for every code
    _redact_label(out.get("label_data"), ctx)
    detail = out.get("detail")
    if isinstance(detail, dict):
        cells = detail.get("cells")
        if isinstance(cells, list):
            for cell in cells:
                if isinstance(cell, dict):
                    _redact_label(cell.get("label_data"), ctx)
        id_cell = detail.get("id_cell")
        if isinstance(id_cell, dict):
            _redact_label(id_cell.get("label_data"), ctx)
    return out
