"""skchat operator-facet probe: the explain / observe / act contract (R2.12).

This is the canonical operator contract for skchat, the module the
`skchat operator` CLI is built over and the exact shape Atlas's skchat adapter
(`skcapstone/src/skcapstone/operator_seat/skchat_adapter.py`) mirrors. One
operator, many apps: skchat conforms by exposing the same three verbs.

The observe probes are REAL and injectable (tests never touch a live skchat):

  * ``DaemonReady``   the daemon health endpoint (:9385 by default).
  * ``BridgeAlive``   the telegram bridge poll age, the silent-wedge detector:
    a poll older than 10 min while the daemon is up reads as wedged (the known
    ConnectTimeout hang signature).
  * ``OutboxBounded`` the pending depth of the UNIFIED skcomms PersistentOutbox
    retry store (coord eb659f61 / roadmap CR-5.3), read through the one
    canonical probe ``skcomms.operator_probe.queue_depth``. This is the single
    backlog metric: the skchat operator CLI (here) and Atlas's skchat adapter
    both consume it, so outbox depth has one source of truth. It replaced the
    legacy ``~/.skcomms/outbox`` transport-spool file count, which was NOT the
    consolidated retry store.
  * ``AuthEnforced``  the ``SKCHAT_DATAPLANE_AUTH`` state.
  * ``CallingReady``  the daemon's WebRTC signaling health (``webrtc_signaling``
    in the ``/health`` body): the calling backend reads DOWN only when the
    WebRTC transport is not wired, so a call cannot be placed. ``ok`` and the
    TURN-fallback ``degraded`` both read ready (spec 2.3, the deferred fifth
    condition, now that a calling-health signal lands).

Every probe fails SAFE (reports healthy) rather than raising a false alarm when
skchat is unreachable, matching the operator facet's fail-safe posture (spec
2.3, failure semantics #3).

The act verb maps the two reversible standard actions (restart-daemon,
restart-telegram-bridge) onto ``systemctl --user restart <unit>`` through an
injectable runner. ``purge-outbox`` is declared irreversible and refuses at the
act verb: it is human-approval-only and escalates as MAJOR by construction.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

#: The five operator conditions, matching the manifest's operator block and the
#: names Atlas's skchat_adapter observes. CallingReady is appended last so the
#: order stays stable for the drift-guard across both repos.
CONDITIONS = [
    "DaemonReady",
    "BridgeAlive",
    "OutboxBounded",
    "AuthEnforced",
    "CallingReady",
]

#: The kinds skchat exposes to the operator plane.
KINDS = ["daemon", "bridge", "outbox", "dataplane-auth", "calling"]

#: skchat conditions are health-type (they fire when status is False), so they
#: are NOT problem-when-true.
_ACTIONS = [
    {
        "name": "restart-daemon",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "restart the skchat receive daemon and verify DaemonReady",
        "kedb_refs": ["ke-skchat-daemon-down"],
    },
    {
        "name": "restart-telegram-bridge",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "restart the wedged telegram bridge (silent-wedge signature)",
        "kedb_refs": ["ke-telegram-wedge"],
    },
    {
        "name": "purge-outbox",
        "standard": False,
        "reversible": False,
        "blast_radius": "delete",
        "runbook": "drop stranded outbox messages (irreversible: escalates as MAJOR)",
        "kedb_refs": ["ke-outbox-flood"],
    },
]

_OUTBOX_LIMIT = 1000
#: The telegram silent-wedge threshold: a bridge whose last poll is older than
#: this while the daemon is up is wedged (the ConnectTimeout hang signature).
_BRIDGE_POLL_MAX_AGE_S = 600
_DAEMON_HEALTH_URL = "http://localhost:9385/health"
_UNIT_RESTART_DAEMON = "skchat-daemon.service"
#: The only WebRTC signaling-health value that means calling cannot be placed
#: (the transport is not wired). ``ok`` and the TURN-fallback ``degraded`` still
#: connect, so they read ready. See daemon.webrtc_signaling_health.
_CALLING_DOWN = "down"

#: The dataplane-auth flag, the canonical source-of-truth (see dataplane_auth.py).
_AUTH_FLAG = "SKCHAT_DATAPLANE_AUTH"
_AUTH_TRUTHY = {"1", "true", "yes", "on"}


def _b(value: bool) -> str:
    return "True" if value else "False"


def _agent() -> str:
    """The active agent, for the per-agent telegram bridge unit name."""
    return (
        os.environ.get("SKAGENT")
        or os.environ.get("SKCAPSTONE_AGENT")
        or os.environ.get("SKMEMORY_AGENT")
        or "lumina"
    )


# --- pure probe logic (unit-tested directly) ---------------------------------


def _bridge_alive(poll_age_s: Optional[float], daemon_up: bool) -> bool:
    """The silent-wedge rule: a bridge is wedged when the daemon is up but the
    last poll is older than the threshold. Unknown poll age fails SAFE (alive)."""
    if poll_age_s is None:
        return True
    return not (daemon_up and poll_age_s > _BRIDGE_POLL_MAX_AGE_S)


def _count_outbox(outbox_dir) -> int:
    """Count queued files under the outbox dir. A missing dir is zero (healthy)."""
    p = Path(outbox_dir)
    if not p.is_dir():
        return 0
    return sum(1 for f in p.iterdir() if f.is_file())


def _unified_outbox_depth() -> int:
    """Depth of the unified skcomms PersistentOutbox: the single backlog metric.

    Delegates to the one canonical probe ``skcomms.operator_probe.queue_depth``
    (coord eb659f61 / roadmap CR-5.3), so the skchat ``OutboxBounded`` condition
    and the skcomms ``QueueDrained`` condition read the SAME consolidated retry
    store (``retry_outbox_dir()/pending``, honoring ``SKCOMMS_OUTBOX_DIR``)
    instead of the legacy ``~/.skcomms/outbox`` transport spool. Fails SAFE
    (returns 0) when skcomms is not importable, so a probe failure never raises
    a false 'outbox flooded' alarm.
    """
    try:
        from skcomms.operator_probe import queue_depth

        return queue_depth()
    except Exception:
        return 0


def _calling_ready(webrtc_signaling) -> bool:
    """CallingReady rule: the calling backend is down ONLY when the daemon's
    WebRTC signaling health reads ``down`` (the transport is not wired). ``ok``,
    the TURN-fallback ``degraded``, and an unknown/absent value (None) all fail
    SAFE to ready (True), so a missing signal never raises a false 'calling down'."""
    if webrtc_signaling is None:
        return True
    return str(webrtc_signaling).strip().lower() != _CALLING_DOWN


# --- real signal readers (each fails safe = healthy) -------------------------


def _probe_daemon_health() -> tuple:
    """Read the daemon health endpoint. Returns (daemon_ready, auth_enforced,
    calling_ready).

    The live daemon reports ``{"status": "ok" | "stopping", ...,
    "webrtc_signaling": "ok"|"degraded"|"down"}`` and does not carry the auth
    flag, so we accept either an ``ok`` boolean or a ``status`` of ``ok`` and
    leave auth to the env probe. ``calling_ready`` is derived from
    ``webrtc_signaling`` (absent on an older daemon -> ready). Fails SAFE: an
    unreachable daemon reports (ready, auth-unknown, calling-ready) so a probe
    failure never raises a false 'daemon down' / 'auth off' / 'calling down'.
    """
    try:
        import json
        import urllib.request

        url = os.environ.get("SKCHAT_DAEMON_HEALTH", _DAEMON_HEALTH_URL)
        with urllib.request.urlopen(url, timeout=8) as r:  # noqa: S310
            body = json.loads(r.read())
        if isinstance(body, dict):
            if "ok" in body:
                ready = bool(body.get("ok"))
            else:
                ready = str(body.get("status", "ok")).lower() == "ok"
            auth = body.get("dataplane_auth")
            calling = _calling_ready(body.get("webrtc_signaling"))
        else:
            ready, auth, calling = True, None, True
        return ready, (bool(auth) if auth is not None else None), calling
    except Exception:
        return True, None, True


def _probe_bridge_poll_age() -> Optional[float]:
    """Age in seconds of the telegram bridge's last-poll heartbeat, or None when
    no heartbeat file is found (fails safe: unknown age reads as alive)."""
    try:
        import time

        candidate = os.environ.get("SKCHAT_BRIDGE_HEARTBEAT")
        if not candidate:
            candidate = str(
                Path.home()
                / ".skcapstone"
                / "agents"
                / _agent()
                / "skwhisper"
                / "telegram_poll.ts"
            )
        p = Path(candidate)
        if not p.is_file():
            return None
        return max(0.0, time.time() - p.stat().st_mtime)
    except Exception:
        return None


def _probe_auth_enforced() -> Optional[bool]:
    """The dataplane-auth state from the env, when the daemon did not report it.

    None means the flag is unset (unknown), which the default probe fails SAFE
    to enforced so it never cries a false 'auth off'.
    """
    val = os.environ.get(_AUTH_FLAG)
    if val is None:
        return None
    return val.strip().lower() in _AUTH_TRUTHY


def _default_probe() -> dict:
    """Best-effort skchat health from real signals. Fails SAFE (healthy) when
    skchat is unreachable, so an inability to probe never raises a false alarm."""
    daemon_ready, auth_from_daemon, calling_ready = _probe_daemon_health()
    poll_age = _probe_bridge_poll_age()
    auth = auth_from_daemon
    if auth is None:
        auth = _probe_auth_enforced()
    return {
        "daemon_ready": daemon_ready,
        "bridge_alive": _bridge_alive(poll_age, daemon_ready),
        "outbox_depth": _unified_outbox_depth(),
        "outbox_limit": _OUTBOX_LIMIT,
        # Unknown auth fails safe to enforced (True): never cry a false 'auth off'.
        "auth_enforced": True if auth is None else bool(auth),
        # Unknown calling health fails safe to ready (True).
        "calling_ready": calling_ready,
    }


# --- contract verbs ----------------------------------------------------------


def explain() -> dict:
    """skchat's self-description in the operator-contract shape."""
    return {
        "kinds": list(KINDS),
        "conditions": list(CONDITIONS),
        "actions": [dict(a) for a in _ACTIONS],
    }


def observe(probe: Optional[Callable[[], dict]] = None) -> dict:
    """Read-only skchat health snapshot in the operator-contract shape.

    ``probe`` is injectable so tests are hermetic; the default reads real
    signals and fails safe.
    """
    st = (probe or _default_probe)()
    depth = int(st.get("outbox_depth", 0))
    limit = int(st.get("outbox_limit", _OUTBOX_LIMIT))
    return {
        "conditions": [
            {
                "type": "DaemonReady",
                "status": _b(bool(st.get("daemon_ready", True))),
                "object": "skchat-daemon",
            },
            {
                "type": "BridgeAlive",
                "status": _b(bool(st.get("bridge_alive", True))),
                "object": "telegram-bridge",
            },
            {"type": "OutboxBounded", "status": _b(depth <= limit), "object": "outbox"},
            {
                "type": "AuthEnforced",
                "status": _b(bool(st.get("auth_enforced", True))),
                "object": "dataplane-auth",
            },
            {
                "type": "CallingReady",
                "status": _b(bool(st.get("calling_ready", True))),
                "object": "calling",
            },
        ]
    }


def _action_meta(action: str) -> Optional[dict]:
    for a in _ACTIONS:
        if a["name"] == action:
            return a
    return None


def _unit_for(action: str, agent: Optional[str] = None) -> Optional[str]:
    """The systemd unit a reversible standard action restarts."""
    if action == "restart-daemon":
        return _UNIT_RESTART_DAEMON
    if action == "restart-telegram-bridge":
        return f"skchat-telegram-{agent or _agent()}.service"
    return None


def _default_runner(cmd) -> dict:
    """Run a systemd command, capturing the result. Never invoked under test."""
    import subprocess

    proc = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def act(
    action: str,
    *,
    runner: Optional[Callable[[list], dict]] = None,
    agent: Optional[str] = None,
    unit: Optional[str] = None,
) -> dict:
    """Perform a reversible standard skchat action, or refuse.

    ``restart-daemon`` and ``restart-telegram-bridge`` (standard, reversible,
    low blast) run ``systemctl --user restart <unit>`` through the injected
    ``runner`` (defaults to a real subprocess). ``purge-outbox`` is declared
    irreversible and is NOT performed here: it is human-approval-only and
    escalates as MAJOR by construction. An unknown action is refused.
    """
    meta = _action_meta(action)
    if meta is None:
        raise ValueError(f"unknown skchat operator action {action!r}")
    if not meta.get("standard"):
        # purge-outbox and any future non-standard action: refuse at the act verb.
        return {
            "action": action,
            "performed": False,
            "escalate": "MAJOR",
            "reason": (
                "irreversible: human-approval-only, escalates as MAJOR by "
                "construction (policy.classify_change) and never actuates here"
            ),
        }
    target_unit = unit or _unit_for(action, agent)
    if target_unit is None:  # pragma: no cover - standard actions always map
        raise ValueError(f"no systemd unit mapping for skchat action {action!r}")
    cmd = ["systemctl", "--user", "restart", target_unit]
    result = (runner or _default_runner)(cmd)
    return {
        "action": action,
        "performed": True,
        "unit": target_unit,
        "command": cmd,
        "result": result,
    }


__all__ = [
    "CONDITIONS",
    "KINDS",
    "explain",
    "observe",
    "act",
]
