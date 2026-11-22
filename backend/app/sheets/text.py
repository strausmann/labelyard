# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Plain-text cleaning for the free text of the sheet archive (purposes, notes, names).

Line breaks (CRLF, CR, LF) become ``\\n``. A ``multiline`` field (notes, descriptions, change
notes) keeps ``\\n``; every other field turns a run of whitespace that holds a line break or a
tab into one space, so words are never glued together. A tab is a space in both kinds.

Removes every other control character (Unicode category ``Cc``), the bidi controls that can
reorder what a person reads (U+061C, U+200E, U+200F, U+202A-U+202E, U+2066-U+2069) and the
invisible format characters that make two different texts look the same: the zero-width space
U+200B, the word joiner U+2060, the byte order mark U+FEFF and the tag characters
U+E0000-U+E007F (#204). The line and paragraph separators U+2028 and U+2029 are line breaks.
Other format characters (``Cf``) stay on purpose: the zero-width joiner U+200D holds emoji
sequences together, the soft hyphen is harmless.

The order is fixed: remove first, trim, **NFC last**. A tag character between a letter and its
combining mark would otherwise keep the two apart for the normaliser and leave them decomposed
once the tag is gone. The result is trimmed; longer than ``max_len`` after cleaning is a
``ValueError`` (the schema reports 422), and so is an empty result of a ``required`` field
(``ValueError("empty")``).
"""

from __future__ import annotations

import contextlib
import re
import unicodedata
from urllib.parse import urlparse, urlsplit

_BIDI_CONTROLS = frozenset({0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)})
#: Invisible format characters removed on write: zero-width space, word joiner, BOM, tags.
_INVISIBLE = frozenset({0x200B, 0x2060, 0xFEFF, *range(0xE0000, 0xE0080)})
_DROPPED = _BIDI_CONTROLS | _INVISIBLE
_LINE_SEPARATORS = str.maketrans({"\u2028": "\n", "\u2029": "\n"})


_WHITESPACE_RUN = re.compile(r"[ \t\n]+")  # one quantifier, no overlap: linear
_INPUT_FACTOR = 4  # input longer than this many times ``max_len`` is refused unprocessed


def _collapse_breaks(match: re.Match[str]) -> str:
    run = match.group()
    return " " if "\t" in run or "\n" in run else run


def _keep(ch: str) -> bool:
    return ch == "\n" or (ord(ch) not in _DROPPED and unicodedata.category(ch) != "Cc")


def sanitise_text(
    value: str, *, max_len: int, multiline: bool = False, required: bool = False
) -> str:
    """Clean ``value``; ``multiline`` keeps line breaks, otherwise they become a space.

    ``required``: a field that is empty after cleaning raises ``ValueError("empty")``.
    """
    if len(value) > _INPUT_FACTOR * max_len:  # before any work on the text
        raise ValueError(f"longer than {max_len} characters")
    text = value.translate(_LINE_SEPARATORS).replace("\r\n", "\n").replace("\r", "\n")
    # Invisible characters go before whitespace runs are collapsed: one between two spaces must
    # not leave the run in two pieces and two spaces behind.
    text = "".join(ch for ch in text if ord(ch) not in _DROPPED)
    text = text.replace("\t", " ") if multiline else _WHITESPACE_RUN.sub(_collapse_breaks, text)
    cleaned = "".join(ch for ch in text if _keep(ch)).strip()
    cleaned = unicodedata.normalize("NFC", cleaned)  # last: after every removal
    if len(cleaned) > max_len:
        raise ValueError(f"longer than {max_len} characters")
    if required and not cleaned:
        raise ValueError("empty")
    return cleaned


_SCRIPTS = ("LATIN", "CYRILLIC", "GREEK")


def _label_scripts(label: str) -> set[str]:
    """The scripts among Latin, Cyrillic and Greek that the letters of ``label`` belong to."""
    found: set[str] = set()
    for ch in label:
        if not ch.isalpha():
            continue
        words = unicodedata.name(ch, "").split()  # "FULLWIDTH LATIN SMALL LETTER A" counts
        for script in _SCRIPTS:
            if script in words:
                found.add(script)
    return found


def _decoded_labels(host: str) -> list[str]:
    """The labels of ``host`` as written in Unicode: punycode (``xn--``) labels are decoded.

    A label that is not valid IDNA is a ``ValueError``.
    """
    labels = []
    for label in host.rstrip(".").split("."):
        if label.startswith("xn--"):
            try:
                label = label.encode("ascii").decode("idna")
            except UnicodeError:
                raise ValueError("the host is not a valid internationalised name") from None
        labels.append(label)
    return labels


def _check_host(host: str) -> None:
    """Refuse a host with a label that mixes Latin, Cyrillic or Greek letters (homograph)."""
    for label in _decoded_labels(host):
        if len(_label_scripts(label)) > 1:
            raise ValueError("a host label must not mix Latin, Cyrillic and Greek letters")


def check_http_url(value: str) -> str:
    """Only http(s) URLs without control characters, user info or a homograph host.

    Shared by import, the API and the service. The user info (``user:secret@host``) is refused:
    it would store a credential and show a host that is not the one the link opens. A host label
    that mixes Latin, Cyrillic or Greek letters (after IDNA decoding) is refused as a look-alike.
    """
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("control characters are not allowed")
    if any(unicodedata.category(ch) == "Cf" for ch in value):
        raise ValueError("invisible format characters are not allowed")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("only http and https URLs are allowed")
    if "@" in parsed.netloc:
        raise ValueError("user info (user:password@) is not allowed in a URL")
    try:
        host = parsed.hostname
    except ValueError:
        raise ValueError("the host is not valid") from None
    if not host:
        raise ValueError("the URL has no host")
    # A host never needs a percent escape. A browser decodes it (``%D0%B5`` is a Cyrillic letter,
    # ``%0a`` a line break), so the checks below would judge another host than the one opened.
    if "%" in host:
        raise ValueError("the host is not valid: no percent escapes")
    _check_host(host)
    return value


def display_host(url: str) -> str:
    """The host of ``url`` as the browser shows it next to a link: lower case, punycode, with the
    port when there is one. The empty string when the URL has no host."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if not host:
        return ""
    with contextlib.suppress(UnicodeError):  # keep the text; the UI shows what it is given
        host = host.encode("idna").decode("ascii")
    if ":" in host:  # an IPv6 literal
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    return f"{host}:{port}" if port is not None else host


_LOG_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def log_value(value: object) -> str:
    """Return ``value`` as one log-safe line: CR, LF and every other control character removed
    (C0, DEL, C1, line and paragraph separator), so it cannot forge or hide a log entry."""
    return _LOG_CONTROL.sub("", str(value).replace("\r", "").replace("\n", ""))
