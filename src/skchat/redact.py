"""Log-safe redaction helpers for sovereign identifiers, emails, and PGP fingerprints.

Full fqids (``lumina@skworld.io``), email addresses, and PGP fingerprints leak
identity and key material when logged in the clear. These helpers are pure,
dependency-free, and never raise -- callers pass whatever they have (including
``None`` or garbage) straight from a log call site.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

#: Returned for input that can't be safely partially masked.
REDACTED_PLACEHOLDER = "<redacted>"

#: Number of trailing characters of a fingerprint left unmasked.
_FINGERPRINT_VISIBLE = 8

#: Matches an fqid or email anywhere in free-form text, e.g. in a log line.
_FQID_RE = re.compile(r"(?:capauth:)?[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

#: Matches an IPv4 address (with optional ``:port``) anywhere in free-form text.
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b")

#: Matches a hex run long enough to plausibly be a PGP key id/fingerprint
#: (short 8-char hex runs are common outside crypto contexts, so they're left alone).
_FINGERPRINT_RE = re.compile(r"\b[0-9A-Fa-f]{16,}\b")


def mask_fqid(value: Any) -> str:
    """Mask the local-part of an fqid, keeping the domain and optional ``capauth:`` prefix.

    ``lumina@skworld.io`` -> ``l****a@skworld.io``;
    ``capauth:lumina@skworld.io`` -> ``capauth:l****a@skworld.io``.
    """
    if not isinstance(value, str):
        return REDACTED_PLACEHOLDER
    value = value.strip()
    if not value:
        return REDACTED_PLACEHOLDER

    prefix = ""
    rest = value
    if rest.startswith("capauth:"):
        prefix, rest = "capauth:", rest[len("capauth:") :]

    local, sep, domain = rest.partition("@")
    if not sep or not local or not domain or "@" in domain:
        return REDACTED_PLACEHOLDER

    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]

    return f"{prefix}{masked_local}@{domain}"


def mask_email(value: Any) -> str:
    """Mask the local-part of an email address, keeping the domain.

    ``alice@x.com`` -> ``a***e@x.com``. Local-parts of length <= 2 are masked
    entirely, since partial masking would reveal most or all of the value.
    """
    if not isinstance(value, str):
        return REDACTED_PLACEHOLDER
    value = value.strip()
    if not value:
        return REDACTED_PLACEHOLDER

    local, sep, domain = value.partition("@")
    if not sep or not local or not domain or "@" in domain:
        return REDACTED_PLACEHOLDER

    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]

    return f"{masked_local}@{domain}"


def mask_fingerprint(value: Any) -> str:
    """Mask a PGP fingerprint, keeping only the last few characters visible.

    Fingerprints longer than :data:`_FINGERPRINT_VISIBLE` keep their trailing
    characters (e.g. ``...QRST7890``); shorter ones are masked entirely since
    partial masking would reveal most or all of the value.
    """
    if not isinstance(value, str):
        return REDACTED_PLACEHOLDER
    value = value.strip()
    if not value:
        return REDACTED_PLACEHOLDER

    if len(value) <= _FINGERPRINT_VISIBLE:
        return "*" * len(value)
    return "*" * (len(value) - _FINGERPRINT_VISIBLE) + value[-_FINGERPRINT_VISIBLE:]


def mask_ip(value: Any) -> str:
    """Mask the host portion of an IPv4 address or ``host:port`` pair.

    Only the last octet stays visible: ``192.168.0.41`` -> ``***.***.***.41``,
    ``192.168.0.41:8080`` -> ``***.***.***.41:8080``. Anything that isn't a
    dotted-quad IPv4 address (hostnames, IPv6, malformed octets) returns
    :data:`REDACTED_PLACEHOLDER`.
    """
    if not isinstance(value, str):
        return REDACTED_PLACEHOLDER
    value = value.strip()
    if not value:
        return REDACTED_PLACEHOLDER

    host, sep, port = value.partition(":")
    if sep and (not port or not port.isdigit()):
        return REDACTED_PLACEHOLDER

    octets = host.split(".")
    if len(octets) != 4 or not all(o.isdigit() and 0 <= int(o) <= 255 for o in octets):
        return REDACTED_PLACEHOLDER

    masked_host = f"***.***.***.{octets[-1]}"
    return f"{masked_host}:{port}" if sep else masked_host


def scrub(value: Any) -> str:
    """Mask emails, fqids, IPs, and PGP fingerprints found anywhere in free-form text.

    Unlike the ``mask_*`` helpers above, which require the *entire* input to be
    one identifier, ``scrub`` scans arbitrary text (e.g. a log message or a
    structured-log field value) and masks every identifier it finds in place,
    leaving the surrounding text untouched. Non-string input returns
    :data:`REDACTED_PLACEHOLDER`.
    """
    if not isinstance(value, str):
        return REDACTED_PLACEHOLDER
    result = _FQID_RE.sub(lambda m: mask_fqid(m.group(0)), value)
    result = _IP_RE.sub(lambda m: mask_ip(m.group(0)), result)
    result = _FINGERPRINT_RE.sub(lambda m: mask_fingerprint(m.group(0)), result)
    return result


def redact_dict(mapping: Any) -> dict:
    """Return a new dict with every string value passed through :func:`scrub`.

    Non-string values are copied unchanged; nested dicts are redacted
    recursively. ``None`` or non-mapping input returns ``{}`` and never
    raises; the input mapping is never mutated.
    """
    if not isinstance(mapping, Mapping):
        return {}

    result: dict[Any, Any] = {}
    for key, value in mapping.items():
        if isinstance(value, Mapping):
            result[key] = redact_dict(value)
        elif isinstance(value, str):
            result[key] = scrub(value)
        else:
            result[key] = value
    return result
