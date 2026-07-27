"""Log-safe redaction helpers for sovereign identifiers, emails, tokens, and fingerprints.

Full fqids (``lumina@skworld.io``), email addresses, secret API tokens, and PGP
fingerprints leak identity and key material when logged in the clear. These
helpers are pure, dependency-free, and never raise -- callers pass whatever
they have (including ``None`` or garbage) straight from a log call site.
"""

from __future__ import annotations

import re
from typing import Any

#: Returned for input that can't be safely partially masked.
REDACTED_PLACEHOLDER = "<redacted>"

#: Number of trailing characters of a fingerprint left unmasked.
_FINGERPRINT_VISIBLE = 8

#: Recognized secret-token prefixes (Anthropic, npm, GitHub, AWS), checked in order.
_TOKEN_PREFIXES = ("sk-ant-", "npm_", "ghp_", "gho_", "ghs_", "AKIA")

#: Minimum length for a standalone hex/base64 run to be treated as a token.
_MIN_ENTROPY_RUN = 24

#: Leading characters kept visible for an unlabeled high-entropy run.
_TOKEN_VISIBLE_PREFIX = 6

_HEX_RUN_RE = re.compile(r"^[0-9a-fA-F]{%d,}$" % _MIN_ENTROPY_RUN)
_B64_RUN_RE = re.compile(r"^[A-Za-z0-9+/_-]{%d,}=*$" % _MIN_ENTROPY_RUN)


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


def _looks_high_entropy(value: str) -> bool:
    """Heuristic for an unlabeled secret run vs. an ordinary word or phrase.

    A pure hex run is always treated as high-entropy. A base64-shaped run only
    counts if it mixes upper, lower, and digit characters the way a random
    token does -- a plain lowercase word of the same length is left alone.
    """
    if _HEX_RUN_RE.match(value):
        return True
    if _B64_RUN_RE.match(value):
        return (
            any(c.isdigit() for c in value)
            and any(c.isupper() for c in value)
            and any(c.islower() for c in value)
        )
    return False


def mask_token(value: Any) -> str:
    """Redact a secret API key/token, keeping a short identifying prefix.

    Recognizes common shapes -- ``sk-ant-*`` (Anthropic), ``npm_*``, GitHub's
    ``ghp_``/``gho_``/``ghs_`` families, AWS ``AKIA*`` access key IDs, and
    standalone high-entropy hex/base64 runs of :data:`_MIN_ENTROPY_RUN`+ chars
    -- and replaces the secret portion, e.g. ``sk-ant-<redacted>``. Plain text
    that doesn't match any of these shapes is returned unchanged.
    """
    if not isinstance(value, str):
        return REDACTED_PLACEHOLDER
    stripped = value.strip()
    if not stripped:
        return REDACTED_PLACEHOLDER

    for prefix in _TOKEN_PREFIXES:
        if stripped.startswith(prefix):
            return f"{prefix}{REDACTED_PLACEHOLDER}"

    if _looks_high_entropy(stripped):
        return f"{stripped[:_TOKEN_VISIBLE_PREFIX]}{REDACTED_PLACEHOLDER}"

    return value
