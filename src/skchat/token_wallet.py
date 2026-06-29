"""TokenWallet — the SENDER side of skcomms gate-4 per-contact delivery tokens.

The recipient-side machinery lives in skcomms (``consent_pipeline.ConsentPipeline``):
on accepting a first-contact request the recipient *mints* a per-contact capability
token (``on_accept`` → ``TokenStore.issue``) and mails it back to the requester. This
module is the requester/sender counterpart: a small persisted stash of the tokens we
have RECEIVED, keyed by the granting peer's fqid, so that when WE later DM that peer
the token can ride along in the message metadata (``consent_token``). The recipient's
gate-4 then recomputes + constant-time-compares it and fast-paths DELIVER instead of
re-quarantining a now-known contact.

Design properties (mirrors ``skcomms.consent_tokens``):

* **Per-contact distinct tokens** — the wallet is a flat ``peer_fqid → token`` map; a
  token issued by one contact never authenticates another.
* **Per-agent isolation** — each agent's wallet lives under its own
  ``<home>/consent/<agent>/token_wallet.json`` (``home`` = ``SKCHAT_HOME`` or
  ``~/.skchat``, the same tree the DM-ratchet store uses), so one agent's tokens are
  invisible to another.
* **Additive + opt-in** — nothing here changes live behaviour. The transport only
  consults the wallet when ``SKCOMMS_CONSENT_MODE`` is set (consent OFF by default).

The ACCEPT/contact-grant message is a plain :class:`~skchat.models.ChatMessage` whose
metadata carries the grant markers (``consent_accept`` / ``consent_token``); the
recipient builds it with :func:`build_accept_message` and the sender harvests the
token with :func:`extract_accept_token` on receive.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from .models import ChatMessage, ContentType

logger = logging.getLogger("skchat.token_wallet")

#: Metadata key flagging a message as a contact-grant / ACCEPT (bool ``True``).
CONSENT_ACCEPT_KEY = "consent_accept"
#: Metadata key carrying the per-contact capability token (hex string).
CONSENT_TOKEN_KEY = "consent_token"


def _skchat_home() -> Path:
    """Resolve the skchat home tree (``SKCHAT_HOME`` or ``~/.skchat``).

    Identical resolution to the DM-ratchet store in :mod:`skchat.transport`, so the
    wallet co-locates with the rest of an agent's per-home state.
    """
    return Path(os.environ.get("SKCHAT_HOME") or os.path.expanduser("~/.skchat"))


def _normalise(peer_fqid: str) -> str:
    """Canonicalise a peer key so ``capauth:`` URIs and bare fqids collide.

    A recipient might be addressed as ``capauth:alice@operator.realm`` on one call
    and ``alice@operator.realm`` on another; both must resolve to the same wallet
    slot. Only the ``capauth:`` scheme is stripped — everything else is preserved.
    """
    key = (peer_fqid or "").strip()
    if key.startswith("capauth:"):
        key = key.split(":", 1)[1]
    return key


class TokenWallet:
    """Per-agent persisted stash of per-contact delivery tokens (sender side).

    Args:
        agent: Short agent name (e.g. ``"lumina"``) — the persistence + isolation
            key. Any ``@domain`` suffix is stripped so it matches the consent stores.
        home: Optional explicit home tree (testing); defaults to
            ``SKCHAT_HOME`` / ``~/.skchat``.
    """

    def __init__(self, agent: str, *, home: Optional[Path] = None) -> None:
        self.agent = (agent or "lumina").split("@")[0]
        base = Path(home) if home is not None else _skchat_home()
        self._dir = base / "consent" / self.agent
        self._path = self._dir / "token_wallet.json"

    # -- persistence ------------------------------------------------------

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("TokenWallet read failed (%s); treating as empty", exc)
        return {}

    def _save(self, mapping: dict[str, str]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(mapping, sort_keys=True), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # the token is a delivery credential — lock it down
        except OSError:
            pass
        tmp.rename(self._path)

    # -- public API -------------------------------------------------------

    def store_token(self, peer_fqid: str, token: str) -> None:
        """Persist *token* as the delivery credential for *peer_fqid* (upsert)."""
        mapping = self._load()
        mapping[_normalise(peer_fqid)] = token
        self._save(mapping)

    def get_token(self, peer_fqid: str) -> Optional[str]:
        """Return the stored token for *peer_fqid*, or ``None`` if we hold none."""
        return self._load().get(_normalise(peer_fqid))

    def drop(self, peer_fqid: str) -> None:
        """Forget the token for *peer_fqid* (no-op if absent)."""
        mapping = self._load()
        if mapping.pop(_normalise(peer_fqid), None) is not None:
            self._save(mapping)


# ── ACCEPT / contact-grant message shape ────────────────────────────────────


def build_accept_message(sender: str, recipient: str, token: str, **kwargs: object) -> ChatMessage:
    """Build the contact-grant message the recipient mails back to a requester.

    The *recipient* of the original first-contact request is the ``sender`` here
    (it is granting the contact + the token); the original requester is the
    ``recipient``. The minted per-contact ``token`` rides in the metadata so the
    requester's :class:`TokenWallet` can harvest it via :func:`extract_accept_token`.
    """
    return ChatMessage(
        sender=sender,
        recipient=recipient,
        content="✓ contact request accepted",
        content_type=ContentType.SYSTEM.value,
        metadata={CONSENT_ACCEPT_KEY: True, CONSENT_TOKEN_KEY: token},
        **kwargs,
    )


def extract_accept_token(message: ChatMessage) -> Optional[tuple[str, str]]:
    """Return ``(grantor_fqid, token)`` if *message* is a token-bearing ACCEPT.

    ``grantor_fqid`` is the message ``sender`` — the contact who accepted us and is
    therefore the wallet key under which the token must be stored (it is the peer we
    will attach the token to on future outbound DMs). Returns ``None`` for any
    message that is not a contact-grant or that carries no token.
    """
    meta = message.metadata or {}
    if not meta.get(CONSENT_ACCEPT_KEY):
        return None
    token = meta.get(CONSENT_TOKEN_KEY)
    if not token:
        return None
    return message.sender, str(token)
