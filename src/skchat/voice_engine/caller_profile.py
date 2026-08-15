"""Who is on the call, resolved from the signature-verified invite FQID.

Privilege on a voice call used to be decided by ``is_chef_identity()``, a
``startswith()`` match of the LiveKit participant identity against
``LUMINA_OPERATOR_PREFIXES`` (default ``("chef",)``). That identity is display
data supplied by whoever joined the room, not an authenticated claim, so the
check was wrong in both directions:

* It failed CLOSED, live, on 2026-08-13. Chef's browser authenticates as the
  AGENT, so his LiveKit identity is ``lumina@chef.skworld.io``, which does not
  start with ``chef``. ``AddressingGate.should_reply`` never reset the
  agent-turn streak for him (she stopped auto-replying to him at all), and
  ``ToolRegistry.dispatch``'s operator gate refused EVERY operator tool in a
  real 1:1, which she then relayed aloud as if it were her own boundary. The
  live workaround was a drop-in widening the prefix list to
  ``chef,lumina@chef``, which is the shim this module retires.
* The same shim fails OPEN for anyone who picks a display name starting with
  ``chef``. A stranger joining as ``chef-laptop`` inherited the operator's
  tools.

The trustworthy source is the invite. ``/call/incoming`` surfaces only
signature-verified invites addressed to this agent, cross-checked against the
signed envelope sender, and ``call_answerer`` carries that ``from_fqid``
through to the room session. So resolution here is an EXACT match of that FQID
against a directory, never a prefix, and never the display identity.

Anything unproven lands on :data:`LEAST_PRIVILEGE`. An unknown, malformed,
bare-short-name or absent caller is a guest, never an operator.

This module is deliberately identity only. The per-profile tool policy
(allow / confirm / deny), the confirmation machine, the action ledger and the
speakable-text gate are separate cards; see the assistive-voice-first design,
section 4.1.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from skchat.call_answerer import AGENT_IDENTITY_SUFFIX

from .tools import ONE_TO_ONE_MODES

log = logging.getLogger("skchat.voice_engine.caller_profile")


class CallerProfile(str, Enum):
    """Who the verified caller is, in privilege order (highest first)."""

    OPERATOR = "operator"
    COMPANION = "companion"
    GUEST = "guest"


#: What an unproven caller gets. Every failure path in this module returns it.
LEAST_PRIVILEGE = CallerProfile.GUEST

#: Operator FQIDs may be pinned explicitly (exact FQIDs, comma separated).
#: They are added to the pair derived from the agent's own identity, never a
#: prefix and never a pattern.
ENV_OPERATOR_FQIDS = "SKCHAT_OPERATOR_FQIDS"
#: Companion FQIDs (exact, comma separated). This is the seam the caller-policy
#: file replaces once the policy layer lands; keep it exact-match either way.
ENV_COMPANION_FQIDS = "SKCHAT_COMPANION_FQIDS"
#: This agent's own FQID, when capauth cannot be asked (tests, minimal hosts).
ENV_AGENT_FQID = "SKCHAT_AGENT_FQID"


def normalize_fqid(value: object) -> Optional[str]:
    """Canonical form of an FQID, or ``None`` when it is not one.

    Accepts the wire prefix (``capauth:lumina@chef.skworld.io``) and trims
    case and surrounding space, because those genuinely vary across the peer
    store. It rejects everything else, including a bare short name like
    ``chef``: a name with no domain cannot be matched against a directory
    without guessing, and guessing is how the prefix shim happened.

    A LiveKit participant suffix (``...#agent``) is rejected rather than
    stripped. Participant identities are minted from an FQID but they are not
    one, and an authorization input must not accept near-misses.
    """
    if not isinstance(value, str):
        return None
    ident = value.strip().lower()
    if ident.startswith("capauth:"):
        ident = ident[len("capauth:") :]
    if not ident or any(c.isspace() for c in ident) or "#" in ident:
        return None
    local, sep, domain = ident.partition("@")
    if not sep or not local or not domain or "@" in domain:
        return None
    return ident


def _split_env_fqids(raw: str) -> set[str]:
    out = set()
    for item in (raw or "").split(","):
        fqid = normalize_fqid(item)
        if fqid:
            out.add(fqid)
        elif item.strip():
            log.warning("ignoring %r: not a full FQID (local@domain)", item.strip())
    return out


@dataclass(frozen=True)
class CallerDirectory:
    """The exact FQIDs that map to something above :data:`LEAST_PRIVILEGE`.

    Built once per session by :meth:`load`. Empty is a valid, safe state: with
    no known operator every caller is a guest, which is the direction a
    security boundary should fail in.
    """

    operator_fqids: frozenset[str] = frozenset()
    companion_fqids: frozenset[str] = frozenset()

    @classmethod
    def build(
        cls,
        *,
        agent_fqid: str | None = None,
        operator_fqids: Iterable[str] = (),
        companion_fqids: Iterable[str] = (),
    ) -> "CallerDirectory":
        """Directory from an agent FQID plus any explicitly pinned identities.

        The operator set is DERIVED from the agent's own sovereign FQID,
        ``<agent>@<operator>.<realm>``, so it tracks cluster identity instead
        of a hand-maintained name list:

        * ``<operator>@<realm>``            the operator as a human (chef@skworld.io)
        * ``<operator>@<operator>.<realm>`` the same human in FQID three-tier form
        * the agent's own FQID              the self-addressed call, see below

        The self-addressed case is real and is the ONLY way the operator
        reaches her today: his browser drives the agent's own webui, so a call
        to "lumina" resolves both ends to ``lumina@chef.skworld.io``.
        ``/call/start``, ``/call/answer`` and ``/call/incoming`` are all
        operator-gated, so an invite from the agent to itself can only have
        been placed by the operator. (What that does NOT attest is who later
        joins the room; room membership is gated by the token mint, which is a
        separate boundary from this one.)
        """
        operators = {f for f in (normalize_fqid(x) for x in operator_fqids) if f}
        companions = {f for f in (normalize_fqid(x) for x in companion_fqids) if f}
        me = normalize_fqid(agent_fqid)
        if me:
            operators.add(me)
            domain = me.partition("@")[2]
            operator_label, _, realm = domain.partition(".")
            if operator_label and realm:
                operators.add(f"{operator_label}@{realm}")
                operators.add(f"{operator_label}@{domain}")
        # A caller cannot be two things. Operator wins, so a stale companion
        # entry can never quietly demote the operator.
        companions -= operators
        return cls(operator_fqids=frozenset(operators), companion_fqids=frozenset(companions))

    @classmethod
    def load(cls, agent_name: str | None = None, env: dict | None = None) -> "CallerDirectory":
        """Build from the running environment (capauth identity + env pins)."""
        environ = os.environ if env is None else env
        agent = agent_name or environ.get("SKAGENT") or "lumina"
        directory = cls.build(
            agent_fqid=_resolve_agent_fqid(agent, environ),
            operator_fqids=_split_env_fqids(environ.get(ENV_OPERATOR_FQIDS, "")),
            companion_fqids=_split_env_fqids(environ.get(ENV_COMPANION_FQIDS, "")),
        )
        if not directory.operator_fqids:
            log.warning(
                "no operator FQID could be resolved for agent %r; every caller will be a %s. "
                "Set %s or repair the capauth identity.",
                agent,
                LEAST_PRIVILEGE.value,
                ENV_OPERATOR_FQIDS,
            )
        return directory

    def profile_for(self, invite_fqid: object) -> CallerProfile:
        """Resolve one signature-verified invite FQID to a profile."""
        fqid = normalize_fqid(invite_fqid)
        if not fqid:
            return LEAST_PRIVILEGE
        if fqid in self.operator_fqids:
            return CallerProfile.OPERATOR
        if fqid in self.companion_fqids:
            return CallerProfile.COMPANION
        return LEAST_PRIVILEGE


def _resolve_agent_fqid(agent: str, environ) -> Optional[str]:
    """This agent's sovereign FQID, preferring the canonical capauth resolver."""
    pinned = environ.get(ENV_AGENT_FQID, "").strip()
    if pinned:
        return pinned
    try:
        from capauth.agent_identity import resolve_agent_identity  # noqa: PLC0415

        return resolve_agent_identity(agent).fqid
    except Exception as exc:  # noqa: BLE001 - a missing resolver must fail closed, not crash
        log.warning("capauth could not resolve an FQID for %r (%r)", agent, exc)
        return None


_DEFAULT_DIRECTORIES: dict[str, CallerDirectory] = {}


def default_directory(agent_name: str | None = None) -> CallerDirectory:
    """Process-wide directory for *agent_name*, built once and cached."""
    agent = agent_name or os.environ.get("SKAGENT") or "lumina"
    if agent not in _DEFAULT_DIRECTORIES:
        _DEFAULT_DIRECTORIES[agent] = CallerDirectory.load(agent)
    return _DEFAULT_DIRECTORIES[agent]


def reset_default_directory() -> None:
    """Drop the cache (tests, and any process that re-reads its identity)."""
    _DEFAULT_DIRECTORIES.clear()


def resolve_caller_profile(
    invite_fqid: object,
    *,
    agent_name: str | None = None,
    directory: CallerDirectory | None = None,
) -> CallerProfile:
    """Profile for a caller, from the signature-verified invite FQID ONLY.

    Pass the ``from_fqid`` of an invite that ``/call/incoming`` surfaced. Do
    not pass a LiveKit participant identity, a display name or a room name:
    none of those are authenticated, and treating them as if they were is the
    whole bug this replaces.
    """
    resolved = directory or default_directory(agent_name)
    profile = resolved.profile_for(invite_fqid)
    log.info("caller %r resolved as %s", invite_fqid, profile.value)
    return profile


def is_one_to_one(mode: str | None) -> bool:
    """True for the 1:1 register, under BOTH vocabularies (see ONE_TO_ONE_MODES)."""
    return (mode or "").strip().lower() in ONE_TO_ONE_MODES


def is_peer_agent(
    speaker_id: str,
    *,
    other_agents: Iterable[str] = (),
    agent_suffix: str = AGENT_IDENTITY_SUFFIX,
) -> bool:
    """True when this speaker is another AGENT rather than a human.

    Display-derived on purpose, and safe to be: it drives conversational
    loop-damping (how chatty she is in a roundtable), never privilege. Getting
    it wrong makes her a little quieter or a little chattier. Privilege comes
    from :func:`speaker_is_operator`, which is not display-derived.
    """
    ident = (speaker_id or "").strip().lower()
    if not ident:
        return False
    if agent_suffix and ident.endswith(agent_suffix.lower()):
        return True
    return any(ident == (o or "").strip().lower() for o in other_agents)


def speaker_is_operator(
    caller: CallerProfile,
    speaker_id: str = "",
    *,
    mode: str,
    other_agents: Iterable[str] = (),
) -> bool:
    """Does this utterance carry the operator's authority?

    Three things must hold, and the display identity is not one of them:

    1. The call's verified caller is the operator.
    2. The call is a 1:1 register. In a group room an utterance cannot be
       attributed to the verified caller, so nobody there speaks with his
       authority. This is the fail-open direction of the old shim closed: a
       second human joining an operator's call does not inherit his tools.
    3. The speaker is not a peer agent, so a roundtable bot cannot borrow the
       operator's hands.
    """
    if caller is not CallerProfile.OPERATOR:
        return False
    if not is_one_to_one(mode):
        return False
    return not is_peer_agent(speaker_id, other_agents=other_agents)


__all__ = [
    "AGENT_IDENTITY_SUFFIX",
    "CallerDirectory",
    "CallerProfile",
    "ENV_AGENT_FQID",
    "ENV_COMPANION_FQIDS",
    "ENV_OPERATOR_FQIDS",
    "LEAST_PRIVILEGE",
    "default_directory",
    "is_one_to_one",
    "is_peer_agent",
    "normalize_fqid",
    "reset_default_directory",
    "resolve_caller_profile",
    "speaker_is_operator",
]
