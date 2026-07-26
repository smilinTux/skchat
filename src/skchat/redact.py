"""Log-safe redaction helpers for sovereign identifiers and PGP fingerprints.

Full fqids (``lumina@skworld.io``) and PGP fingerprints leak identity and key
material when logged in the clear. These helpers are pure, dependency-free,
and never raise -- callers pass whatever they have (including ``None`` or
garbage) straight from a log call site.
"""

from __future__ import annotations

from typing import Any

#: Returned for input that can't be safely partially masked.
REDACTED_PLACEHOLDER = "<redacted>"

#: Number of trailing characters of a fingerprint left unmasked.
_FINGERPRINT_VISIBLE = 8


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
