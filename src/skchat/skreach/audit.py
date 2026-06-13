"""Append-only audit writer — F1 §4.

Emits a structured AuditRecord for every command that reaches the execution
stage (whether executed, rejected, confirm-required, etc.).

Two write paths (§4.3):
  1. Local JSONL file: ~/.skcapstone/agents/<node>/skreach/audit.jsonl
     This is the offline/disaster-recovery copy.  Never truncated.
  2. skmem-pg INSERT (TODO in F2 MVP — path is wired but the DB connection is
     optional; if the pg path is unavailable the JSONL is the source of truth).

The AuditWriter NEVER logs env values — only key names (§4.2 / §4.4).

Spec: docs/superpowers/specs/2026-06-13-skreach-security.md §4
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AuditRecord dataclass  (§4.2)
# ---------------------------------------------------------------------------


@dataclass
class AuditRecord:
    """Structured audit record (§4.2).

    Outcome values: executed | rbac_denied | sig_invalid | expired | replay |
                    misdirected | unauthorized_iss | allowlist_denied |
                    confirm_required | confirm_rejected | error | timeout
    """

    # Identity
    audit_id: str = field(default_factory=lambda: secrets.token_hex(16))
    cmd_id: str = ""
    node_fqid: str = ""
    iss_fqid: str = ""
    role: str = ""

    # Command
    cmd_class: str = ""
    op: str = ""
    argv: list[str] = field(default_factory=list)
    cwd: str = ""

    # Security-safe env accounting (NEVER log values)
    env_keys: list[str] = field(default_factory=list)   # keys present (not stripped)
    scrubbed_keys: list[str] = field(default_factory=list)  # keys that were stripped

    # Outcome
    outcome: str = ""
    exit_code: Optional[int] = None
    stdout_sha256: Optional[str] = None  # hash of stdout blob (never the content)
    stderr_sha256: Optional[str] = None

    # Timing
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    duration_ms: Optional[int] = None

    def finalise(
        self,
        outcome: str,
        exit_code: Optional[int] = None,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        """Fill in outcome fields.  Called once the command completes (or is rejected)."""
        self.outcome = outcome
        self.exit_code = exit_code
        self.ended_at = time.time()
        self.duration_ms = int((self.ended_at - self.started_at) * 1000)
        if stdout:
            self.stdout_sha256 = hashlib.sha256(stdout).hexdigest()
        if stderr:
            self.stderr_sha256 = hashlib.sha256(stderr).hexdigest()

    def to_dict(self) -> dict:
        """Serialise to a plain dict (safe for JSON, never includes env values)."""
        d = asdict(self)
        # Defensive: ensure no env value leaks (belt-and-suspenders)
        d.pop("_env_values", None)
        return d

    def to_json_line(self) -> str:
        """Serialise to a single JSON line for JSONL append."""
        return json.dumps(self.to_dict(), separators=(",", ":"))


# ---------------------------------------------------------------------------
# AuditWriter
# ---------------------------------------------------------------------------


class AuditWriter:
    """Append-only audit record writer.

    Writes to the local JSONL file immediately (synchronous).  The skmem-pg
    INSERT path is a TODO — when wired, it will use INSERT ... ON CONFLICT DO
    NOTHING for crash-recovery replay safety.

    The writer is intentionally simple: no buffering, no background thread.
    Audit records must be durable before exec starts (§4.1).

    Args:
        jsonl_path:  Path to the audit JSONL file.  Default:
                     ~/.skcapstone/agents/<SKAGENT>/skreach/audit.jsonl
                     (reads SKAGENT env var, falls back to "default").
        pg_dsn:      Optional Postgres DSN for skmem-pg writes (TODO).
    """

    def __init__(
        self,
        jsonl_path: Optional[Path] = None,
        pg_dsn: Optional[str] = None,
    ) -> None:
        if jsonl_path is None:
            agent = os.environ.get("SKAGENT", "default")
            jsonl_path = (
                Path.home()
                / ".skcapstone"
                / "agents"
                / agent
                / "skreach"
                / "audit.jsonl"
            )
        self._path = Path(jsonl_path)
        self._pg_dsn = pg_dsn  # TODO: wire pg INSERT in F3/F4
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(self, record: AuditRecord) -> None:
        """Append *record* to the JSONL file.

        This is intentionally synchronous and called BEFORE exec starts
        (so a crash during exec cannot lose the record — §4.1).

        Never raises; logs any I/O error instead (the daemon must not crash
        because of an audit write failure).
        """
        line = record.to_json_line()
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            logger.error("skreachd audit write failed: %s; record: %s", exc, line)

        # TODO (F3/F4): INSERT INTO skreach_audit (audit_id, recorded_at, record)
        # VALUES (%s, now(), %s) ON CONFLICT (audit_id) DO NOTHING;
        if self._pg_dsn:
            logger.debug("skreachd audit pg INSERT not yet wired (TODO F3/F4)")

    def write_rejection(
        self,
        outcome: str,
        cmd_id: str = "",
        iss_fqid: str = "",
        role: str = "",
        cmd_class: str = "",
        op: str = "",
        argv: Optional[list[str]] = None,
        cwd: str = "",
    ) -> AuditRecord:
        """Create and persist a rejection-outcome audit record.

        Convenience wrapper for all the drop paths (sig_invalid, expired,
        rbac_denied, allowlist_denied, etc.).

        Returns the AuditRecord (for tests and downstream logging).
        """
        rec = AuditRecord(
            cmd_id=cmd_id,
            iss_fqid=iss_fqid,
            role=role,
            cmd_class=cmd_class,
            op=op,
            argv=argv or [],
            cwd=cwd,
        )
        rec.finalise(outcome=outcome)
        self.write(rec)
        return rec

    @property
    def path(self) -> Path:
        """The path to the JSONL audit file."""
        return self._path
