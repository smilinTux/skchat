"""skreachd — sovereign remote-control daemon for SKWorld.

Security-first: exec is DISABLED by default (SKREACH_EXEC_ENABLED=0).
No subprocess is ever spawned unless that env var is explicitly set to "1".

The F1 security model (capauth-signed envelopes, RBAC tiers, sandbox, audit)
is fully implemented; the actual exec call is gated behind the env flag.

Spec: docs/superpowers/specs/2026-06-13-skreach-security.md
"""

from .audit import AuditRecord, AuditWriter
from .daemon import Skreachd
from .protocol import CommandEnvelope, VerifyResult, verify_envelope
from .rbac import CommandClass, Decision, Role, authorize
from .sandbox import ExecDisabled, ExecResult, SandboxConfig, run

__all__ = [
    # protocol
    "CommandEnvelope",
    "VerifyResult",
    "verify_envelope",
    # rbac
    "CommandClass",
    "Decision",
    "Role",
    "authorize",
    # sandbox
    "ExecDisabled",
    "ExecResult",
    "SandboxConfig",
    "run",
    # audit
    "AuditRecord",
    "AuditWriter",
    # daemon
    "Skreachd",
]
