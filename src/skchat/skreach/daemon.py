"""skreachd skeleton daemon — F1 / F2 MVP.

Receives a CommandEnvelope via an injected transport, runs the full §1.3
verification pipeline, evaluates RBAC, audits the decision, and (if all
checks pass) dispatches to the sandbox executor.

**The transport and signature verifier are injected** — at import time and
in tests, no real network connection, no real PGP operation, and no real
subprocess is ever spawned.

Design:
  - Transport protocol: any callable matching TransportT (see below).
    In production this will be a WebRTC data-channel reader or a skcomms
    mailbox poll; in tests it is a list of pre-built envelopes.
  - The daemon is intentionally NOT async in the MVP — the transport is
    expected to yield envelopes synchronously (or the caller drives the loop).
    Async upgrade is deferred to F3 (terminal lane integration).

Spec: docs/superpowers/specs/2026-06-13-skreach-security.md §§1–5
      docs/superpowers/specs/2026-06-12-skchat-architecture-reassessment.md §2.6
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional, Union

from .audit import AuditRecord, AuditWriter
from .protocol import (
    _DEFAULT_REPLAY_CACHE,
    CommandEnvelope,
    RoleResolver,
    SigVerifier,
    VerifyResult,
    _ReplayCache,
    verify_envelope,
)
from .rbac import NodeGrants, authorize
from .sandbox import ExecDisabled, ExecResult, SandboxConfig, ValidationError, run

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transport protocol type
# ---------------------------------------------------------------------------

# A transport is any callable that yields (CommandEnvelope, raw_bytes) pairs.
# In production: a WebRTC data-channel reader or skcomms mailbox poll.
# In tests: a simple list-based stub.
TransportT = Callable[[], Iterator[CommandEnvelope]]

# ---------------------------------------------------------------------------
# Dispatch result
# ---------------------------------------------------------------------------


@dataclass
class DispatchResult:
    """Result of Skreachd.handle_one(envelope).

    Attributes:
        outcome:  Short outcome string (matches AuditRecord.outcome).
        verify:   The VerifyResult from the protocol layer.
        exec_out: ExecDisabled or ExecResult from sandbox, if we reached exec.
        audit_id: The audit record id (for correlation).
        error:    Any error message (non-empty on unexpected failures).
    """

    outcome: str
    verify: Optional[VerifyResult] = None
    exec_out: Optional[Union[ExecDisabled, ExecResult]] = None
    audit_id: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Skreachd daemon
# ---------------------------------------------------------------------------


class Skreachd:
    """skreachd — sovereign remote-control daemon skeleton.

    All injected dependencies (transport, sig_verifier, role_resolver) are
    set at construction time.  No global state is mutated after __init__.

    Args:
        self_fqid:      This node's FQID (used for node-binding check).
        sig_verifier:   Injectable PGP signature verifier (§1.3 step 1).
        role_resolver:  Injectable role resolver (§1.3 step 5).
        transport:      Callable returning an iterator of envelopes (injected).
        audit_writer:   AuditWriter instance; a new default writer if None.
        node_grants:    Per-node agent grants (§2.3).
        sandbox_config: Per-node sandbox policy (§5).
        replay_cache:   Anti-replay cache; uses module default if None.
    """

    def __init__(
        self,
        *,
        self_fqid: str,
        sig_verifier: SigVerifier,
        role_resolver: RoleResolver,
        transport: Optional[TransportT] = None,
        audit_writer: Optional[AuditWriter] = None,
        node_grants: Optional[NodeGrants] = None,
        sandbox_config: Optional[SandboxConfig] = None,
        replay_cache: Optional[_ReplayCache] = None,
    ) -> None:
        self._self_fqid = self_fqid
        self._sig_verifier = sig_verifier
        self._role_resolver = role_resolver
        self._transport = transport or (lambda: iter([]))
        self._audit = audit_writer or AuditWriter()
        self._node_grants = node_grants or NodeGrants()
        self._sandbox = sandbox_config or SandboxConfig()
        self._replay_cache = replay_cache or _DEFAULT_REPLAY_CACHE

    # ------------------------------------------------------------------
    # Core: handle a single envelope
    # ------------------------------------------------------------------

    def handle_one(self, envelope: CommandEnvelope) -> DispatchResult:
        """Process one CommandEnvelope through the full F1 pipeline.

        Steps:
          1. §1.3 verify_envelope (sig, freshness, anti-replay, role)
          2. §2/§3 RBAC check
          3. §4 Audit record (written before exec)
          4. §5 Sandbox exec (gated by SKREACH_EXEC_ENABLED)

        Returns a DispatchResult with the outcome regardless of path taken.
        """
        # -------- Step 1: Protocol-layer verification --------
        verify_result = verify_envelope(
            envelope=envelope,
            self_fqid=self._self_fqid,
            sig_verifier=self._sig_verifier,
            role_resolver=self._role_resolver,
            replay_cache=self._replay_cache,
        )

        if not verify_result.valid:
            outcome = verify_result.reason.value
            logger.warning(
                "skreachd: dropped envelope id=%s reason=%s iss=%s",
                verify_result.env_id,
                outcome,
                envelope.iss,
            )
            rec = self._audit.write_rejection(
                outcome=outcome,
                cmd_id=envelope.id,
                iss_fqid=envelope.iss,
                cmd_class=envelope.cmd.cls,
                op=envelope.cmd.op,
                argv=envelope.cmd.args,
                cwd=envelope.cmd.cwd,
            )
            return DispatchResult(
                outcome=outcome,
                verify=verify_result,
                audit_id=rec.audit_id,
            )

        role = verify_result.role or "guest"
        cmd = envelope.cmd

        # -------- Step 2: RBAC check --------
        decision = authorize(
            role=role,
            cmd_class=cmd.cls,
            issuer_fqid=envelope.iss,
            node_grants=self._node_grants,
        )

        if decision.denied:
            logger.warning(
                "skreachd: rbac_denied id=%s role=%s class=%s reason=%s",
                envelope.id,
                role,
                cmd.cls,
                decision.reason,
            )
            rec = self._audit.write_rejection(
                outcome="rbac_denied",
                cmd_id=envelope.id,
                iss_fqid=envelope.iss,
                role=role,
                cmd_class=cmd.cls,
                op=cmd.op,
                argv=cmd.args,
                cwd=cmd.cwd,
            )
            return DispatchResult(
                outcome="rbac_denied",
                verify=verify_result,
                audit_id=rec.audit_id,
            )

        if decision.confirm_required:
            # §3.3: return CONFIRM_REQUIRED without executing
            logger.info(
                "skreachd: confirm_required id=%s role=%s class=%s",
                envelope.id,
                role,
                cmd.cls,
            )
            rec = self._audit.write_rejection(
                outcome="confirm_required",
                cmd_id=envelope.id,
                iss_fqid=envelope.iss,
                role=role,
                cmd_class=cmd.cls,
                op=cmd.op,
                argv=cmd.args,
                cwd=cmd.cwd,
            )
            return DispatchResult(
                outcome="confirm_required",
                verify=verify_result,
                audit_id=rec.audit_id,
            )

        # -------- Step 3: Build audit record (written BEFORE exec) --------
        audit_rec = AuditRecord(
            cmd_id=envelope.id,
            node_fqid=self._self_fqid,
            iss_fqid=envelope.iss,
            role=role,
            cmd_class=cmd.cls,
            op=cmd.op,
            argv=cmd.args,
            cwd=cmd.cwd,
            started_at=time.time(),
        )
        # Write the pre-exec record (outcome TBD — written again after exec)
        # We use a provisional outcome so a crash during exec still has a record.
        audit_rec.outcome = "executing"
        self._audit.write(audit_rec)

        # -------- Step 4: Sandbox exec (gated) --------
        exec_out: Union[ExecDisabled, ExecResult, None] = None
        outcome = "executed"

        try:
            exec_out = run(
                argv=cmd.args,
                cwd=cmd.cwd,
                extra_env=cmd.env,
                cmd_id=envelope.id,
                config=self._sandbox,
            )
        except ValidationError as ve:
            outcome = ve.outcome
            logger.warning("skreachd: validation_error id=%s reason=%s", envelope.id, ve.message)
            audit_rec.finalise(outcome=outcome, stderr=ve.message.encode())
            self._audit.write(audit_rec)
            return DispatchResult(
                outcome=outcome,
                verify=verify_result,
                audit_id=audit_rec.audit_id,
                error=ve.message,
            )

        if isinstance(exec_out, ExecDisabled):
            outcome = "exec_disabled"
            audit_rec.scrubbed_keys = exec_out.scrubbed_keys
            audit_rec.finalise(outcome=outcome)
        elif isinstance(exec_out, ExecResult):
            outcome = exec_out.outcome
            audit_rec.scrubbed_keys = exec_out.scrubbed_keys
            audit_rec.exit_code = exec_out.exit_code
            audit_rec.finalise(
                outcome=outcome,
                exit_code=exec_out.exit_code,
                stdout=exec_out.stdout,
                stderr=exec_out.stderr,
            )

        # Final audit record update (post-exec)
        self._audit.write(audit_rec)

        return DispatchResult(
            outcome=outcome,
            verify=verify_result,
            exec_out=exec_out,
            audit_id=audit_rec.audit_id,
        )

    # ------------------------------------------------------------------
    # Main loop (transport-driven)
    # ------------------------------------------------------------------

    def run_loop(self, *, max_envelopes: Optional[int] = None) -> int:
        """Drain the transport and handle each envelope.

        This is the main entry-point for production use.  Transport must be
        injected at construction time.

        Args:
            max_envelopes: If set, stop after processing this many envelopes
                           (useful for testing; None = run forever).

        Returns:
            Number of envelopes processed.
        """
        processed = 0
        for envelope in self._transport():
            self.handle_one(envelope)
            processed += 1
            if max_envelopes is not None and processed >= max_envelopes:
                break
        return processed
