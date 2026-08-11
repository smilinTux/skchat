"""Prekey store + Lumina's own hybrid keypair (PQC-MIGRATION Q5, app-side wiring).

This is the daemon/webui half of Q5: it lets the Flutter app (chef) and the
daemon (Lumina) exchange **PQXDH-style hybrid-KEM prekeys** so DMs go hybrid
post-quantum end-to-end.

Two responsibilities:

1. **Peer prekey store.** The app publishes its device prekey bundle via
   ``POST /api/v1/prekey``; the daemon persists it here (keyed by short name) and
   serves it back via ``GET /api/v1/prekey/{peer}``. Lumina's send path looks up
   the operator's stored bundle to seal her reply.

2. **Lumina's own hybrid keypair.** Generated once (via :mod:`skcomms.pqkem`),
   persisted 0600, and exposed both as a published bundle (so the app can seal
   to her) and as the private key (so she can open hybrid DMs addressed to her).

Storage: ``~/.skchat/pqc/`` -
   * ``peers/<short>.json``   - published peer bundles (JSON)
   * ``lumina_hybrid.key``    - Lumina's 2432-byte hybrid private key (hex, 0600)
   * ``lumina_hybrid.pub``    - Lumina's 1216-byte hybrid public key (hex)

Honesty: if liboqs is unavailable, Lumina simply publishes no hybrid prekey
(``available() is False``) and every conversation stays classical - the same
negotiated-downgrade contract the rest of Q3 uses. Never a silent failure.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HYBRID_SUITE = "x25519-mlkem768"
CLASSICAL_SUITE = "x25519-pgp-wrap-v1"

#: DM ratchet wire-format capability this build advertises (RFC-0001 P1).
RATCHET_CAP = "pqdr1"

#: Multi-device fanout (Phase 1): the maximum number of concurrent device slots
#: a single peer may hold. Retirement is by explicit revoke only (no auto-TTL
#: prune); publishing an 11th DISTINCT device rejects rather than evicting.
SLOT_CAP = 10


class SlotCapExceeded(Exception):
    """Raised when publishing a NEW device slot would exceed :data:`SLOT_CAP`."""


class InsecureKeyPermissionsError(Exception):
    """Raised when the root hybrid private key is group/world-readable.

    Only raised under :data:`STRICT_KEY_PERMS_ENV` - see :func:`_check_key_perms`.
    """


#: Env flag (P0.5 / SEAM 7): when truthy, the **app-path** prekey intake
#: (``store_app_prekey_bundle`` behind ``POST /api/v1/prekey``) fails closed -
#: only a bundle carrying a signature that verifies under the claimed identity's
#: key is stored. Default OFF so the live app (which publishes UNSIGNED bundles
#: today) is not locked out; behaviour is UNCHANGED when unset.
REQUIRE_SIGNED_PREKEYS_ENV = "SKCHAT_REQUIRE_SIGNED_PREKEYS"

#: Env flag (arch review section 3 / root-key protection): when truthy, loading
#: the plaintext root hybrid private key refuses a file that is group- or
#: world-readable (``mode & 0o077``) instead of silently reading it. Default OFF
#: so the historical plaintext-0600 file keeps loading unchanged for operators
#: who haven't opted in yet.
STRICT_KEY_PERMS_ENV = "SKCHAT_STRICT_KEY_PERMS"

#: Env-selected private-key backend for :func:`ensure_agent_keypair`. ``"keyring"``
#: tries the OS keyring (via the optional ``keyring`` package) first and falls
#: back to the plaintext file when no sealed entry is present - see
#: :func:`_load_sealed_private`. Default (unset/anything else) is the historical
#: plaintext-file-only behaviour.
KEY_BACKEND_ENV = "SKCHAT_KEY_BACKEND"

#: Service name the sealed backend stores/looks up the root hybrid private key
#: under, keyed by ``<agent>_hybrid`` (mirrors the plaintext filename stem).
_KEYRING_SERVICE = "skchat-pqc-hybrid"

_TRUTHY = {"1", "true", "yes", "on"}


def prekey_verify_mode() -> str:
    """Return the app-path prekey verification mode.

    One of ``'off'`` (default), ``'shadow'``, or ``'enforce'``. Mirrors
    :func:`skchat.dataplane_auth.authz_pdp_mode` so both rollouts stage the same
    way. Read at call time so an operator can move a live daemon between modes
    without a reimport. Anything unrecognized reads as ``'off'``.

    Back-compat: every value in :data:`_TRUTHY` (the historical "flag on" set)
    reads as ``'enforce'``, so no existing reader changes behaviour.
    """
    raw = os.environ.get(REQUIRE_SIGNED_PREKEYS_ENV, "").strip().lower()
    if raw == "shadow":
        return "shadow"
    return "enforce" if raw in _TRUTHY else "off"


def require_signed_prekeys() -> bool:
    """Whether unsigned/invalid app-path prekey bundles must be rejected.

    True only in ``'enforce'``. Shadow verifies and reports but never rejects.
    """
    return prekey_verify_mode() == "enforce"


def _pqc_dir() -> Path:
    home = Path(os.environ.get("SKCHAT_HOME", Path.home() / ".skchat"))
    d = home / "pqc"
    (d / "peers").mkdir(parents=True, exist_ok=True)
    return d


def _short(uri: str) -> str:
    s = uri[len("capauth:") :] if uri.startswith("capauth:") else uri
    return s.split("@")[0]


def _safe_agent(agent: str) -> str:
    """Reduce an agent name to a filesystem-safe token for keypair paths.

    Keypair/session filenames are built as ``<agent>_hybrid.key`` etc. under the
    pqc dir, so an agent name derived from an untrusted identity (e.g. a skseal
    ``context['sender']`` of ``capauth:../x@y`` → ``../x``) must never carry a path
    separator or traversal component, or the key would be written outside the pqc
    dir. Keep only ``[A-Za-z0-9._-]`` and strip leading dots, so ``..``/``/`` can
    never escape. Raises on an empty result rather than silently defaulting.
    """
    import re

    token = re.sub(r"[^A-Za-z0-9._-]", "", (agent or "").split("@")[0])
    token = token.lstrip(".")
    if not token:
        raise ValueError(f"unsafe/empty agent name: {agent!r}")
    return token


def _current_agent() -> str:
    """The local resident agent short name (SKAGENT, fallbacks, default lumina)."""
    return (
        os.environ.get("SKAGENT")
        or os.environ.get("SKCAPSTONE_AGENT")
        or os.environ.get("SKMEMORY_AGENT")
        or "lumina"
    ).split("@")[0]


# --------------------------------------------------------------------------- #
# Peer prekey store
# --------------------------------------------------------------------------- #


def _safe_slot_id(key_id: str) -> str:
    """Filesystem-safe token for a slot filename.

    A ``key_id`` is 16-hex today, but it arrives from a published (untrusted)
    bundle, so reduce it to ``[A-Za-z0-9._-]`` and strip leading dots the same way
    :func:`_safe_agent` guards keypair paths - a slot file can never escape the
    peer directory via ``..``/``/``. Empty results fall back to ``_default`` so a
    classical (no-key_id) bundle still gets a stable single slot.
    """
    token = re.sub(r"[^A-Za-z0-9._-]", "", (key_id or "")).lstrip(".")
    return token or "_default"


def _peer_dir(peer: str) -> Path:
    """The per-peer slot directory ``peers/<short>/`` (created on demand).

    Raises:
        ValueError: if *peer* normalises to an empty short name.

    Card c5bbb20d: ``_short("@x")`` is ``""``, and ``peers/`` joined with ``""``
    is ``peers/`` itself. A slot written there lands at ``peers/<key_id>.json``,
    which is exactly the legacy flat-file path :func:`load_peer_bundles` folds in
    for back-compat. A publisher choosing ``owner="@x"`` and
    ``key_id="<victim>"`` could therefore plant a bundle that
    ``load_peer_bundle("<victim>")`` serves as the victim's newest slot: prekey
    substitution against another identity. The fold-in's key_id dedup does not
    help, because a real device key_id is 16 hex chars and never collides with a
    short name.

    So this is the chokepoint: every slot write resolves its directory here, and
    an empty short name is refused rather than silently collapsing to the root.
    """
    short = _short(peer).strip()
    if not short:
        raise ValueError(
            f"refusing prekey slot for an owner that normalises to an empty short name: {peer!r}"
        )
    d = _pqc_dir() / "peers" / short
    d.mkdir(parents=True, exist_ok=True)
    return d


def _normalise_bundle(bundle: dict) -> dict:
    """Keep only the contract fields (plus ``last_published`` for slot ordering).

    Normalisation is a pure function of its input (no wall-clock stamping), so
    storing the same bundle twice yields byte-identical slots. A bundle that
    omits ``last_published`` keeps ``None`` and simply sorts last in
    :func:`load_peer_bundles`.
    """
    return {
        "suite": bundle.get("suite", CLASSICAL_SUITE),
        "hybrid_public_hex": bundle.get("hybrid_public_hex", "") or "",
        "signature": bundle.get("signature"),
        "key_id": bundle.get("key_id"),
        "device_id": bundle.get("device_id"),
        # Capability advert: which DM ratchet wire format this client speaks.
        # Absent for clients without the pqdr1 codec (app / older agents) so the
        # sender stays classical for them (RFC-0001 downgrade protection).
        "ratchet": bundle.get("ratchet"),
        # Multi-device fanout advert: "pqdm2" means this device speaks the
        # multi-recipient envelope, so the sender fans out to all device slots
        # (Task 9). Distinct from `ratchet`; absent -> sender stays pqdm1 for it.
        "codec": bundle.get("codec"),
        # When this slot was (re)published; drives newest-first slot ordering.
        "last_published": bundle.get("last_published"),
    }


def store_peer_bundle(peer: str, bundle: dict) -> None:
    """Upsert a published peer prekey slot, keyed by ``bundle["key_id"]``.

    Multi-device fanout: each of a peer's devices is one slot file at
    ``peers/<short>/<key_id>.json``. Republishing the same ``key_id`` overwrites
    that slot in place; a NEW ``key_id`` adds a slot, and once the peer already
    holds :data:`SLOT_CAP` distinct devices a further NEW device raises
    :class:`SlotCapExceeded` (retirement is explicit revoke only). A bundle
    without a ``key_id`` (classical fallback) collapses to a single ``_default``
    slot, preserving the pre-multislot single-bundle behaviour.
    """
    slot_id = _safe_slot_id(bundle.get("key_id"))
    d = _peer_dir(peer)
    path = d / f"{slot_id}.json"
    if not path.exists():
        # A NEW distinct device - enforce the slot cap before creating it.
        existing = len([p for p in d.glob("*.json") if p.is_file()])
        if existing >= SLOT_CAP:
            raise SlotCapExceeded(
                f"{_short(peer)} already holds {existing} device slots "
                f"(cap {SLOT_CAP}); revoke one before adding {slot_id}"
            )
    safe = _normalise_bundle(bundle)
    # Atomic write: temp file in the same directory, then os.replace() onto the
    # target so a crash mid-write never leaves a torn slot file (mirrors the
    # operator_auth / history write pattern).
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(safe, indent=2))
    os.replace(tmp, path)


def store_app_prekey_bundle(
    peer: str,
    bundle: dict,
    *,
    signer_public_armor: Optional[str] = None,
    signer_source: str = "none",
) -> bool:
    """Intake a prekey bundle published over the **app path** (``POST /api/v1/prekey``).

    Behaviour is chosen by :func:`prekey_verify_mode`:

    * ``'off'`` (default) - stored as-is, nothing verified, nothing logged.
      Byte-identical to the historical unflagged path.
    * ``'shadow'`` - the signature IS verified and the outcome logged, but the
      bundle is stored either way. This is the soak mode: it answers "who breaks
      if I enforce" without breaking anyone.
    * ``'enforce'`` - stored only if the signature verifies. A null/missing
      signature, a missing signer key, or a failed verification (prekey
      substitution / wrong identity) rejects the bundle and stores nothing,
      closing the handshake MITM gap.

    Args:
        peer: The publishing peer (short name or URI); keys the stored bundle.
        bundle: The published prekey bundle dict.
        signer_public_armor: ASCII-armored PGP public key of the claimed identity.
            Used in ``shadow`` and ``enforce``.
        signer_source: Label for WHICH source resolved that key
            (``daemon-attest`` / ``peer-store`` / ``none``), for the audit line.

    Returns:
        ``True`` if the bundle was stored, ``False`` if it was rejected.
    """
    mode = prekey_verify_mode()
    if mode == "off":
        store_peer_bundle(peer, bundle)
        return True

    reason = _prekey_verify_reason(bundle, signer_public_armor)
    _log_prekey_verify(mode, peer, bundle, signer_source, reason)

    if mode == "enforce" and reason is not None:
        return False
    store_peer_bundle(peer, bundle)
    return True


def _log_prekey_verify(
    mode: str, peer: str, bundle: dict, signer_source: str, reason: Optional[str]
) -> None:
    """Emit the one-line audit record for a verified intake.

    Stable ``prekey-verify`` prefix so a soak is greppable straight out of
    journalctl. Carries only the TRUNCATED key_id; never a public key, never a
    signature.

    Level choice is deliberate, not uniform:

    * ``REJECT`` is always WARNING, in every mode.
    * ``ACCEPT`` is WARNING in ``shadow`` but INFO in ``enforce``. This is NOT
      an inconsistency to "clean up". The webui process that serves this route
      runs ``uvicorn.run(log_level="warning")``, which configures only the
      ``uvicorn*`` loggers; nothing configures the root logger, so root stays
      at its default level (WARNING, via ``logging.lastResort``) with no
      handlers attached. An INFO record from this module is therefore silently
      DROPPED in production. The whole point of shadow mode is rollout step 4:
      "every distinct publishing device should appear with result=ACCEPT" -
      if ACCEPT stayed at INFO, a soak that logged nothing would be
      indistinguishable from a soak that ran clean, and the flip-to-enforce
      decision would be unverifiable. So shadow escalates ACCEPT to WARNING to
      make the soak actually visible. Once in ``enforce`` the signature check
      is load-bearing (rejects really block the publish), so ACCEPT reverts to
      INFO for steady-state noise reduction.
    """
    kid = str(bundle.get("key_id") or "?")[:8]
    line = "prekey-verify mode=%s owner=%s kid=%s signer=%s result=%s" % (
        mode,
        _short(peer),
        kid,
        signer_source,
        "ACCEPT" if reason is None else "REJECT",
    )
    if reason is not None:
        logger.warning("%s reason=%s", line, reason)
    elif mode == "shadow":
        logger.warning(line)
    else:
        logger.info(line)


def _prekey_verify_reason(bundle: dict, signer_public_armor: Optional[str]) -> Optional[str]:
    """``None`` if the bundle's signature verifies, else a short reason code.

    Reason codes are stable strings meant for the audit line and for triage:
    ``unsigned`` (no signature on the bundle), ``no-signer-key`` (no key resolved
    for the claimed owner), ``bad-signature`` (present but does not verify:
    prekey substitution or wrong identity).
    """
    if not bundle.get("signature"):
        return "unsigned"
    if not signer_public_armor:
        return "no-signer-key"
    from . import prekey_sig

    if not prekey_sig.verify_prekey_bundle(bundle, signer_public_armor):
        return "bad-signature"
    return None


def _slot_sort_key(bundle: dict) -> float:
    """Newest-first ordering key: higher ``last_published`` sorts first."""
    lp = bundle.get("last_published")
    try:
        return float(lp)
    except (TypeError, ValueError):
        return 0.0


def load_peer_bundles(peer: str) -> list[dict]:
    """Return every current device slot for *peer*, newest first.

    Reads all ``peers/<short>/<key_id>.json`` slots (plus a legacy flat
    ``peers/<short>.json`` if one predates the migration), skipping any slot that
    fails to parse (corrupt files are logged and quarantined out of the result,
    never crash the load). Empty list when the peer has published nothing.
    """
    peers_root = _pqc_dir() / "peers"
    slots: list[dict] = []
    seen: set[str] = set()

    d = peers_root / _short(peer)
    if d.is_dir():
        for path in sorted(d.glob("*.json")):
            if not path.is_file():
                continue
            try:
                bundle = json.loads(path.read_text())
            except Exception:
                logger.warning("corrupt peer prekey slot %s", path, exc_info=True)
                continue
            slots.append(bundle)
            kid = bundle.get("key_id")
            if kid:
                seen.add(kid)

    # Back-compat: a pre-multislot deployment stored one flat file. Fold it in as
    # a slot unless a directory slot with the same key_id already supersedes it.
    legacy = peers_root / f"{_short(peer)}.json"
    if legacy.is_file():
        try:
            bundle = json.loads(legacy.read_text())
            if bundle.get("key_id") not in seen:
                slots.append(bundle)
        except Exception:
            logger.warning("corrupt legacy peer prekey %s", legacy, exc_info=True)

    slots.sort(key=_slot_sort_key, reverse=True)
    return slots


def load_peer_bundle(peer: str) -> Optional[dict]:
    """Return the NEWEST stored peer slot (or None if the peer never published).

    Back-compat shim over :func:`load_peer_bundles` for callers that still expect
    a single bundle (the newest device slot).
    """
    slots = load_peer_bundles(peer)
    return slots[0] if slots else None


def remove_peer_bundle(peer: str, key_id: str) -> bool:
    """Retire a single device slot by ``key_id``. True if a slot was removed."""
    path = _peer_dir(peer) / f"{_safe_slot_id(key_id)}.json"
    if not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        logger.warning("failed to remove peer prekey slot %s", path, exc_info=True)
        return False


def peer_is_hybrid(peer: str) -> bool:
    b = load_peer_bundle(peer)
    return bool(b and b.get("suite") == HYBRID_SUITE and b.get("hybrid_public_hex"))


# --------------------------------------------------------------------------- #
# Lumina's own hybrid keypair
# --------------------------------------------------------------------------- #


def available() -> bool:
    """Whether the PQ backend (liboqs via skcomms.pqkem) is reachable."""
    try:
        from skcomms import pqkem

        return pqkem.is_available()
    except Exception:
        return False


def _strict_key_perms() -> bool:
    """Whether :data:`STRICT_KEY_PERMS_ENV` is set (truthy)."""
    return os.environ.get(STRICT_KEY_PERMS_ENV, "").strip().lower() in _TRUTHY


def _check_key_perms(path: Path) -> None:
    """Refuse ``path`` if it is group/world-readable and strict perms are on.

    No-op unless :func:`_strict_key_perms` is true, so the historical
    plaintext-0600 file keeps loading unchanged when the flag is unset. When the
    flag is on, any bit in ``mode & 0o077`` (group or world read/write/execute)
    raises :class:`InsecureKeyPermissionsError` rather than reading the key.
    """
    if not _strict_key_perms():
        return
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        raise InsecureKeyPermissionsError(
            f"refusing to load {path}: mode {oct(mode)} is group/world-readable "
            f"(chmod 0600 it, or unset {STRICT_KEY_PERMS_ENV} to override)"
        )


def _key_backend() -> str:
    """The configured private-key backend: ``'keyring'`` or ``'plaintext'``."""
    raw = os.environ.get(KEY_BACKEND_ENV, "").strip().lower()
    return "keyring" if raw == "keyring" else "plaintext"


def _load_sealed_private(agent: str) -> Optional[bytes]:
    """Best-effort load of ``agent``'s hybrid private key from the OS keyring.

    Returns ``None`` (never raises) when the keyring backend isn't selected via
    :data:`KEY_BACKEND_ENV`, the optional ``keyring`` package isn't installed, or
    no sealed entry exists for this agent - callers fall back to the plaintext
    file in that case, so this path is strictly additive and never breaks the
    existing plaintext-0600 load.
    """
    if _key_backend() != "keyring":
        return None
    try:
        import keyring
    except ImportError:
        return None
    try:
        hex_priv = keyring.get_password(_KEYRING_SERVICE, f"{agent}_hybrid")
    except Exception:
        logger.warning("keyring lookup failed for %s hybrid key", agent, exc_info=True)
        return None
    if not hex_priv:
        return None
    try:
        return bytes.fromhex(hex_priv)
    except ValueError:
        logger.warning("corrupt sealed %s hybrid key in keyring", agent)
        return None


def ensure_agent_keypair(agent: Optional[str] = None) -> Optional[tuple[bytes, bytes]]:
    """Load-or-generate the resident agent's hybrid keypair.

    PQC cut-over: every resident agent (not just Lumina) publishes a hybrid
    prekey on startup so DMs to it negotiate hybrid by default. The key is
    persisted 0600 at ``~/.skchat/pqc/<agent>_hybrid.key`` / ``.pub``.

    Lumina keeps her historical ``lumina_hybrid.*`` filenames (so existing
    on-disk keys and the published bundle stay byte-identical); other agents use
    ``<agent>_hybrid.*``.

    Private-key load order: (1) the sealed/keyring backend when
    :data:`KEY_BACKEND_ENV` selects it and an entry is present (see
    :func:`_load_sealed_private`); (2) the plaintext ``.key`` file, refused under
    :data:`STRICT_KEY_PERMS_ENV` if it is group/world-readable (see
    :func:`_check_key_perms`); (3) generate a fresh keypair.

    Returns ``(public, private)`` or ``None`` if no PQ backend is available
    (honest classical fallback - never a silent failure).
    """
    agent = _safe_agent(agent or _current_agent())
    if not available():
        return None
    d = _pqc_dir()
    priv_path = d / f"{agent}_hybrid.key"
    pub_path = d / f"{agent}_hybrid.pub"

    sealed_priv = _load_sealed_private(agent)
    if sealed_priv is not None and pub_path.exists():
        try:
            return (bytes.fromhex(pub_path.read_text().strip()), sealed_priv)
        except Exception:
            logger.warning("corrupt %s hybrid pub key - regenerating", agent, exc_info=True)

    if priv_path.exists() and pub_path.exists():
        _check_key_perms(priv_path)
        try:
            return (
                bytes.fromhex(pub_path.read_text().strip()),
                bytes.fromhex(priv_path.read_text().strip()),
            )
        except Exception:
            logger.warning("corrupt %s hybrid key - regenerating", agent, exc_info=True)
    try:
        from skcomms import pqkem

        kp = pqkem.hybrid_keypair()
    except Exception:
        logger.exception("%s hybrid keypair generation failed", agent)
        return None
    pub_path.write_text(kp.public_key.hex())
    priv_path.write_text(kp.private_key.hex())
    try:
        os.chmod(priv_path, 0o600)
    except OSError:
        pass
    return (kp.public_key, kp.private_key)


def agent_private(agent: Optional[str] = None) -> Optional[bytes]:
    kp = ensure_agent_keypair(agent)
    return kp[1] if kp else None


def agent_public(agent: Optional[str] = None) -> Optional[bytes]:
    kp = ensure_agent_keypair(agent)
    return kp[0] if kp else None


def agent_bundle(agent: Optional[str] = None) -> dict:
    """The resident agent's published prekey bundle (hybrid if available)."""
    agent = _safe_agent(agent or _current_agent())
    kp = ensure_agent_keypair(agent)
    if not kp:
        return {"suite": CLASSICAL_SUITE, "hybrid_public_hex": ""}
    pub, _ = kp
    return {
        "suite": HYBRID_SUITE,
        "hybrid_public_hex": pub.hex(),
        # Signature stays classical (Phase 2 / Q7).
        "signature": None,
        "key_id": pub.hex()[:16],
        "device_id": f"{agent}-daemon",
        # This build speaks the pqdr1 DM ratchet - advertise it so capable peers
        # negotiate Level-3 and incapable ones (app/older clients) stay classical.
        "ratchet": RATCHET_CAP,
    }


def publish_self_prekey(agent: Optional[str] = None) -> dict:
    """Generate (if needed) + return the resident agent's prekey bundle.

    Startup hook: a daemon calls this once on boot so the agent's hybrid prekey
    exists and is serveable (via ``GET /api/v1/prekey/<agent>``). Returns the
    published bundle (``suite``/``hybrid_public_hex``/…). When liboqs is absent
    the bundle is classical-only - honest, never raised.
    """
    agent = _safe_agent(agent or _current_agent())
    bundle = agent_bundle(agent)
    # Register the agent in the SHARED peer store so co-resident agents resolve it
    # via load_peer_bundle() (RFC-0001 P1: the local-fleet prekey "exchange").
    store_peer_bundle(agent, bundle)
    if bundle.get("suite") == HYBRID_SUITE:
        logger.info("PQC: published hybrid prekey for resident agent %s", agent)
    else:
        logger.info(
            "PQC: no hybrid backend - agent %s publishes a classical prekey "
            "(DMs to it stay classical until liboqs is available)",
            agent,
        )
    return bundle


def sync_fleet_prekeys() -> dict[str, str]:
    """Publish every co-resident agent's prekey into the shared peer store.

    Scans the PQ dir for ``<agent>_hybrid.pub`` keypairs and registers each one
    (idempotent) so all co-resident agents can resolve each other and DMs negotiate
    the Level-3 ratchet. Returns ``{agent: suite}``.
    """
    published: dict[str, str] = {}
    for pub in sorted(_pqc_dir().glob("*_hybrid.pub")):
        agent = pub.name[: -len("_hybrid.pub")]
        if not agent:
            continue
        bundle = publish_self_prekey(agent)
        published[agent] = bundle.get("suite", CLASSICAL_SUITE)
    return published


# --------------------------------------------------------------------------- #
# Lumina back-compat aliases (the daemon_proxy + webui call these by name).
# --------------------------------------------------------------------------- #


def ensure_lumina_keypair() -> Optional[tuple[bytes, bytes]]:
    """Back-compat alias for :func:`ensure_agent_keypair` pinned to ``lumina``."""
    return ensure_agent_keypair("lumina")


def lumina_private() -> Optional[bytes]:
    return agent_private("lumina")


def lumina_bundle() -> dict:
    """Lumina's published prekey bundle (hybrid if available, else classical)."""
    return agent_bundle("lumina")


# --------------------------------------------------------------------------- #
# Group-create helper: collect hybrid prekeys for a set of members.
# --------------------------------------------------------------------------- #


def hybrid_pub_hex_for(identity_uri: str, *, self_agent: Optional[str] = None) -> str:
    """Best-effort hybrid public-key hex for ``identity_uri`` (or "").

    Resolution order:
      1. If the short name is the resident agent itself → its own public key.
      2. The published peer bundle in ``~/.skchat/pqc/peers/<short>.json``.
    Returns "" when no hybrid key is known (the member then falls back to the
    classical wrap and is flagged in the group self-report - never locked out).
    """
    short = _short(identity_uri)
    me = (self_agent or _current_agent()).split("@")[0]
    if short == me:
        pub = agent_public(me)
        return pub.hex() if pub else ""
    bundle = load_peer_bundle(short)
    if bundle and bundle.get("suite") == HYBRID_SUITE and bundle.get("hybrid_public_hex"):
        return str(bundle["hybrid_public_hex"])
    return ""


def collect_member_hybrid_keys(
    identities: list[str], *, self_agent: Optional[str] = None
) -> dict[str, str]:
    """Map every ``identity_uri -> hex(hybrid pub)`` that we can resolve.

    Members with no known hybrid key are omitted (they fall back classically).
    Used by the group-create paths so a new group is hybrid-from-epoch-1 for the
    members that have prekeys, without locking out classical-only peers.
    """
    out: dict[str, str] = {}
    for uri in identities:
        pub_hex = hybrid_pub_hex_for(uri, self_agent=self_agent)
        if pub_hex:
            out[uri] = pub_hex
    return out
