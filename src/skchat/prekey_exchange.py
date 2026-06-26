"""Cross-node hybrid prekey exchange — pull a REMOTE agent's bundle (RFC-0001 P1).

The FOUNDATION of the federated prekey exchange. Today co-resident agents resolve
each other through the shared local prekey store (see
:func:`skchat.pq_prekeys.publish_self_prekey` / ``sync_fleet_prekeys``), so DMs
between agents on the *same* node can negotiate the Level-3 ``pqdr1`` ratchet.

A peer on **another node** (e.g. ``jarvis@<operator>.<realm>`` on .41) never lands
in our local store, so :class:`skchat.dm_manager.DmRatchetManager` can't resolve a
hybrid public key for it and every DM to it stays classical. This module lets a
node *pull* a remote agent's published bundle over federation and persist it
locally, so the existing resolver / :func:`pq_prekeys.peer_is_hybrid` light up.

Design / honesty:

* The hybrid KEM stays **X25519 + ML-KEM-768** (FIPS 203 ML-KEM) — secure if
  EITHER leg holds. This module only *transports + stores* a published bundle; it
  performs no key agreement of its own.
* The HTTP getter is **injected** (``http_get``) — no network call is hard-coded,
  so the fetch is unit-tested without a live daemon. Federation addressing
  (which remote daemon serves the peer) is likewise injectable
  (``inbox_resolver``), defaulting to :func:`skcomms.discovery.inbox_url_for`.
* A bundle that does **not** advertise the ``pqdr1`` capability is stored but
  stays classical — downgrade-safe (RFC-0001 downgrade protection). The dm_manager
  resolver already gates the ratchet on the capability, so a remote app / older
  agent is never silently locked into a format it can't speak.
* If the bundle carries an optional **sovereign** signature and a signer public
  key is supplied, it is verified via :mod:`skchat.prekey_sig`; a bad signature
  (prekey-substitution) is rejected — nothing is stored.

This module is intentionally NOT wired into the live daemon poll loop here; that
plus the live .41 verify is a separate task (coord ``bb2d06ef``).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional
from urllib.parse import urlsplit

from skchat import pq_prekeys as PQ

logger = logging.getLogger(__name__)

#: Type of the injected HTTP getter: ``(url) -> parsed-JSON | None``.
HttpGet = Callable[[str], Optional[object]]
#: Type of the injected federation resolver: ``(peer_fqid) -> base-url | None``.
InboxResolver = Callable[[str], Optional[str]]


def _default_inbox_resolver(peer_fqid: str) -> Optional[str]:
    """Resolve a peer's S2S inbox URL via skcomms federation addressing."""
    try:
        from skcomms.discovery import inbox_url_for

        return inbox_url_for(peer_fqid)
    except Exception:
        logger.debug("federation inbox resolution failed for %s", peer_fqid, exc_info=True)
        return None


def _prekey_url(inbox_url: str, short: str) -> Optional[str]:
    """Derive the remote daemon's ``/api/v1/prekey/<short>`` from its inbox URL.

    The peer's ``https-s2s`` inbox URL points at the same daemon host that serves
    the prekey endpoint; we keep its scheme + authority and swap the path.
    """
    parts = urlsplit(inbox_url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}/api/v1/prekey/{short}"


def is_ratchet_capable(bundle: Optional[dict]) -> bool:
    """Whether *bundle* is a hybrid bundle that advertises the ``pqdr1`` ratchet.

    Mirrors the gate in :meth:`DmRatchetManager.for_agent`'s resolver: hybrid
    suite + a non-empty hybrid public key + the ``pqdr1`` capability. A hybrid
    bundle WITHOUT the capability is usable classically but not for the ratchet.
    """
    return bool(
        bundle
        and bundle.get("suite") == PQ.HYBRID_SUITE
        and bundle.get("hybrid_public_hex")
        and bundle.get("ratchet") == PQ.RATCHET_CAP
    )


def _unwrap(raw: object) -> Optional[dict]:
    """Extract the bundle dict from a getter response, or None if malformed.

    The daemon endpoint returns ``{"prekey": {...bundle...}}``; a getter may also
    hand back the bundle directly. Empty / non-dict / contentless payloads are
    rejected (returns None — caller stores nothing).
    """
    if not isinstance(raw, dict) or not raw:
        return None
    bundle = raw["prekey"] if "prekey" in raw else raw
    if not isinstance(bundle, dict) or not bundle:
        return None
    # A well-formed bundle always names its suite; without it there is nothing to
    # negotiate (the classical-fallback endpoint still returns an explicit suite).
    if "suite" not in bundle:
        return None
    return bundle


def fetch_peer_prekey(
    peer_fqid: str,
    *,
    http_get: HttpGet,
    inbox_resolver: InboxResolver = _default_inbox_resolver,
    signer_pubkey: Optional[str] = None,
) -> Optional[dict]:
    """Pull a remote peer's published prekey bundle and store it locally.

    GETs the remote daemon's ``/api/v1/prekey/<short>`` (via the injected
    ``http_get``), validates the response, optionally verifies a sovereign
    signature, and persists it through :func:`pq_prekeys.store_peer_bundle` so
    :func:`pq_prekeys.peer_is_hybrid` and the dm_manager resolver resolve it.

    Args:
        peer_fqid: The remote peer handle (``<agent>@<operator>.<realm>`` or
            ``capauth:<agent>@...`` / bare name).
        http_get: Injected getter ``(url) -> parsed-JSON | None``. No real network
            call is hard-coded — the caller supplies the transport.
        inbox_resolver: Injected federation addressing
            ``(peer_fqid) -> base-url | None``. Defaults to
            :func:`skcomms.discovery.inbox_url_for`.
        signer_pubkey: Optional ASCII-armored PGP public key of the claimed
            identity. If the bundle carries a ``signature`` and this is supplied,
            the signature is verified via :func:`skchat.prekey_sig.verify_prekey_bundle`;
            a bad signature rejects the bundle (nothing stored).

    Returns:
        The stored, normalised bundle dict, or ``None`` if the peer is unroutable,
        the response is malformed/empty, or the sovereign signature fails to
        verify. A bundle without the ``pqdr1`` capability is still stored (and
        returned) but stays classical — :func:`is_ratchet_capable` is ``False``.
    """
    short = PQ._short(peer_fqid)

    inbox_url = inbox_resolver(peer_fqid)
    if not inbox_url:
        logger.info("prekey_exchange: no federation route to %s — staying classical", peer_fqid)
        return None
    url = _prekey_url(inbox_url, short)
    if not url:
        logger.warning("prekey_exchange: bad inbox URL %r for %s", inbox_url, peer_fqid)
        return None

    try:
        raw = http_get(url)
    except Exception:
        logger.warning("prekey_exchange: GET %s failed", url, exc_info=True)
        return None

    bundle = _unwrap(raw)
    if bundle is None:
        logger.info("prekey_exchange: malformed/empty prekey for %s — nothing stored", peer_fqid)
        return None

    # Optional SOVEREIGN (attributable) mode: if the bundle is signed AND a signer
    # key is known, verify the prekey binds to the claimed identity. A tampered
    # hybrid_public_hex (prekey substitution) or wrong identity is rejected.
    if bundle.get("signature") and signer_pubkey:
        try:
            from skchat import prekey_sig

            if not prekey_sig.verify_prekey_bundle(bundle, signer_pubkey):
                logger.warning(
                    "prekey_exchange: sovereign signature for %s failed — rejecting", peer_fqid
                )
                return None
        except Exception:
            logger.warning("prekey_exchange: signature verify error for %s", peer_fqid, exc_info=True)
            return None

    # Persist (store_peer_bundle normalises + preserves the capability + signature).
    PQ.store_peer_bundle(short, bundle)
    stored = PQ.load_peer_bundle(short)
    if is_ratchet_capable(stored):
        logger.info("prekey_exchange: stored pqdr1 prekey for remote peer %s", short)
    else:
        logger.info(
            "prekey_exchange: stored classical prekey for remote peer %s "
            "(no pqdr1 capability — DMs stay classical)",
            short,
        )
    return stored
