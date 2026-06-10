"""skchat ⇄ skcapstone — optional integration adapter (ADR backbone).

skchat runs fully standalone.  When the ``skcapstone`` package is installed
(and the operator has not forced standalone mode with ``SK_STANDALONE=1``),
this adapter routes alerts through skcapstone's shared **sk-alert** bus and
registers skchat's outbox-flush sweep with the fleet **skscheduler**, so the
whole sk* mesh sees one alert stream and one scheduler.  When skcapstone is
absent, every call degrades to skchat's native behaviour (structured logging /
``notify-send`` desktop notifications + the systemd-managed daemon's own
polling loop).

This is the *default-on-by-presence* pattern specified by
``skcapstone/docs/ADR-optional-integration-backbone.md`` — nothing here is a
hard dependency; ``skcapstone`` lives in the optional ``[skcapstone]`` extra.
The canonical reference implementation is ``skmemory/skmemory/integration.py``;
this adapter follows it exactly so skchat matches the established convention
rather than inventing a new one.

It is the *single* skcapstone backbone seam for skchat.  The existing
``identity_bridge`` (delegates identity to ``capauth.resolve_agent_identity``)
and ``memory_bridge`` (forwards chat threads to the skcapstone MCP
``session_capture`` tool) keep their own narrow responsibilities — this adapter
does NOT duplicate them; it owns the ADR's alert / schedule / discovery
backbone and routes everything else through the stable ``skcapstone.sdk``
facade.

Public API:
    is_present()                          -> bool
    alert(event, payload, level)          -> bool   (True iff sent via sk-alert)
    ensure_schedule(interval_minutes)     -> bool   (True iff registered with skscheduler)
    unregister_schedule()                 -> bool
    register_self(pid_file)               -> bool
    capabilities()                        -> dict   (feature/mode detection)

Topic convention: ``skchat.<severity>`` (severity ∈ info|warn|error|critical).
The semantic *event* name is carried in the payload ``event`` field — not the
topic suffix — so ``skcapstone alerts``' ``*.error``/``*.critical``/``*.warn``
wildcards match by severity while detail is preserved.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("skchat.integration")

#: This service's name — used as the alert topic prefix and registry key.
SERVICE = "skchat"

#: Fleet-scheduler job name for the reliable-outbox flush sweep.
OUTBOX_JOB = "skchat_outbox_flush"

#: Default health endpoint exposed by the skchat daemon (see CLAUDE.md).
DEFAULT_HEALTH_URL = "http://127.0.0.1:9385/health"

#: Default pid-file written by the systemd-managed daemon.
DEFAULT_PID_FILE = str(Path("~/.skchat/daemon.pid").expanduser())

# Optional import — never a hard dependency.
try:
    from skcapstone import sdk as _sdk
except Exception:  # ImportError, or a broken partial install
    _sdk = None  # type: ignore[assignment]

#: severity → logging method name (native fallback)
_LOG_METHOD = {
    "info": "info",
    "warn": "warning",
    "error": "error",
    "critical": "critical",
}
_NOTIFY_LEVELS = frozenset({"warn", "error", "critical"})

#: Backbone features this adapter offers when skcapstone is present.
_INTEGRATED_FEATURES = ("sk-alert", "skscheduler", "discovery")


def is_present() -> bool:
    """Return whether skcapstone integration should be used from this process.

    ``True`` only when the package imported, the operator has not set
    ``SK_STANDALONE``, and the SDK reports itself available.  Any failure is
    treated as "not present" so callers transparently use their native path.
    """
    if os.environ.get("SK_STANDALONE"):
        return False
    if _sdk is None:
        return False
    try:
        return bool(_sdk.is_available())
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("skcapstone present-check failed: %s", exc)
        return False


def alert(event: str, payload: dict[str, Any], level: str = "info") -> bool:
    """Emit an alert: via skcapstone sk-alert when present, else local log.

    The published topic follows the ecosystem convention ``skchat.<severity>``
    (so ``skcapstone alerts`` — which subscribes to ``*.error`` / ``*.critical``
    / ``*.warn`` — surfaces it).  The semantic *event* name is carried in the
    payload's ``event`` field rather than the topic, so routing stays
    severity-based while detail is preserved.

    Args:
        event: Semantic event name (e.g. ``"delivery_failed"``).  Stored in
            the payload as ``event``.
        payload: JSON-serialisable event body.
        level: ``info | warn | error | critical``.

    Returns:
        ``True`` if published to the shared bus, ``False`` if it fell back to
        local logging (which always also happens at the matching level).
    """
    body = {"event": event, **dict(payload)}
    if is_present():
        try:
            return bool(
                _sdk.alert(
                    f"{SERVICE}.{level}",
                    body,
                    level=level,
                    notify=level in _NOTIFY_LEVELS,
                )
            )
        except Exception as exc:
            logger.warning("sk-alert publish failed, logging locally: %s", exc)

    # native fallback — structured log at the matching level
    method = getattr(logger, _LOG_METHOD.get(level, "info"))
    method("[%s.%s] %s", SERVICE, level, body)
    return False


def ensure_schedule(interval_minutes: float = 5.0) -> bool:
    """Register the outbox-flush sweep with the fleet scheduler, if present.

    Writes a ``jobs.d/skchat_outbox_flush.yaml`` drop-in that runs ``skchat
    outbox flush`` every *interval_minutes*, so the skcapstone daemon owns the
    cadence (central retry/notify) for delivering queued messages.  Idempotent
    — safe to call on every startup.

    Args:
        interval_minutes: Flush cadence in minutes (default 5, matching the
            daemon's typical poll interval).

    Returns:
        ``True`` if registered with skscheduler; ``False`` when skcapstone is
        absent and the caller should rely on its native daemon loop.
    """
    if not is_present():
        return False
    try:
        _sdk.register_job(
            {
                "name": OUTBOX_JOB,
                "type": "shell",
                "command": "skchat outbox flush",
                "every": f"{int(interval_minutes * 60)}s",
                "timeout": 120,
                "notify": "on_failure",
                "notify_level": "error",
            }
        )
        logger.info(
            "Registered '%s' with skcapstone scheduler (every %.1fm).",
            OUTBOX_JOB,
            interval_minutes,
        )
        return True
    except Exception as exc:
        logger.warning("ensure_schedule failed (using native): %s", exc)
        return False


def unregister_schedule() -> bool:
    """Remove the outbox-flush drop-in from the fleet scheduler."""
    if _sdk is None:
        return False
    try:
        return bool(_sdk.unregister_job(OUTBOX_JOB))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("unregister_schedule failed: %s", exc)
        return False


def register_self(pid_file: Optional[str] = None) -> bool:
    """Advertise skchat to skcapstone's discovery registry, if present.

    Args:
        pid_file: Optional pid-file path used as a liveness signal.  Defaults
            to the systemd-managed daemon's pid-file (``~/.skchat/daemon.pid``).

    Returns:
        ``True`` if registered, ``False`` otherwise.
    """
    if not is_present():
        return False
    try:
        _sdk.register_service(
            SERVICE,
            health_url=DEFAULT_HEALTH_URL,
            pid_file=pid_file or DEFAULT_PID_FILE,
        )
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("register_self failed: %s", exc)
        return False


def capabilities() -> dict[str, Any]:
    """Report the adapter's mode + available backbone features.

    Lets callers (and ``skchat health`` / diagnostics) branch on whether the
    fleet backbone is wired without each duplicating the presence/escape-hatch
    logic.  When skcapstone is absent or ``SK_STANDALONE`` is set, ``features``
    is empty and ``integrated`` is ``False`` — skchat still runs, natively.

    Returns:
        A dict with ``service``, ``integrated`` (bool), and ``features`` (the
        backbone capabilities currently usable, e.g. ``["sk-alert",
        "skscheduler", "discovery"]``).
    """
    integrated = is_present()
    return {
        "service": SERVICE,
        "integrated": integrated,
        "features": list(_INTEGRATED_FEATURES) if integrated else [],
    }
