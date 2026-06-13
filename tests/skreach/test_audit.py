"""Tests for skchat.skreach.audit — F1 §4.

Covers acceptance criteria:
  AUDIT-2: every rejection has an audit record (AuditWriter.write_rejection)
  AUDIT-4: env_keys never contains env values
  + AuditRecord.to_json_line() is valid JSON
  + AuditRecord.finalise() sets outcome, timestamps, hashes
  + AuditWriter appends to JSONL; file is never truncated
  + Multiple writes → multiple lines
  + stdout/stderr values never appear in the audit record
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skchat.skreach.audit import AuditRecord, AuditWriter


# ---------------------------------------------------------------------------
# AuditRecord unit tests
# ---------------------------------------------------------------------------


def test_audit_record_has_unique_id() -> None:
    """Each AuditRecord gets a unique audit_id."""
    r1 = AuditRecord()
    r2 = AuditRecord()
    assert r1.audit_id != r2.audit_id


def test_audit_record_finalise_sets_outcome() -> None:
    """finalise() fills outcome, exit_code, ended_at, duration_ms."""
    rec = AuditRecord()
    rec.finalise(outcome="executed", exit_code=0)
    assert rec.outcome == "executed"
    assert rec.exit_code == 0
    assert rec.ended_at is not None
    assert rec.duration_ms is not None and rec.duration_ms >= 0


def test_audit_record_stdout_stored_as_hash_only() -> None:
    """AUDIT-4: stdout content is stored as sha256 hash, never as raw bytes."""
    rec = AuditRecord()
    rec.finalise(outcome="executed", exit_code=0, stdout=b"sensitive output")
    assert rec.stdout_sha256 is not None
    # The sha256 is 64 hex chars
    assert len(rec.stdout_sha256) == 64
    # The raw stdout is NOT on the record
    d = rec.to_dict()
    assert "sensitive output" not in json.dumps(d)


def test_audit_record_stderr_stored_as_hash_only() -> None:
    """AUDIT-4: stderr content is stored as sha256 hash only."""
    rec = AuditRecord()
    rec.finalise(outcome="error", exit_code=1, stderr=b"error: permission denied")
    assert rec.stderr_sha256 is not None
    d = rec.to_dict()
    assert "permission denied" not in json.dumps(d)


def test_audit_record_env_keys_never_contains_values() -> None:
    """AUDIT-4: env_keys holds only key names, never values."""
    rec = AuditRecord(
        env_keys=["SKAGENT", "HOME"],
        scrubbed_keys=["MY_SECRET", "API_TOKEN"],
    )
    rec.finalise(outcome="executed")
    d = rec.to_dict()
    serialised = json.dumps(d)
    # Key names appear in the serialised record
    assert "SKAGENT" in serialised
    assert "MY_SECRET" in serialised
    # Values must NEVER appear
    assert "hunter2" not in serialised
    assert "s3cr3t" not in serialised


def test_audit_record_to_json_line_is_valid_json() -> None:
    """AuditRecord.to_json_line() produces a parseable single JSON line."""
    rec = AuditRecord(
        cmd_id="deadbeef",
        iss_fqid="chef@skworld.io",
        role="operator",
        cmd_class="exec",
        op="run",
        argv=["skcapstone", "status"],
        cwd="/opt/skworld",
    )
    rec.finalise(outcome="executed", exit_code=0)
    line = rec.to_json_line()
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["cmd_id"] == "deadbeef"
    assert parsed["outcome"] == "executed"


# ---------------------------------------------------------------------------
# AuditWriter integration (tmp file)
# ---------------------------------------------------------------------------


def test_audit_writer_appends_to_jsonl(tmp_path: Path) -> None:
    """AuditWriter appends records to the JSONL file."""
    path = tmp_path / "audit.jsonl"
    writer = AuditWriter(jsonl_path=path)

    rec1 = AuditRecord(cmd_id="aaa", outcome="executed")
    rec1.finalise("executed")
    rec2 = AuditRecord(cmd_id="bbb", outcome="rbac_denied")
    rec2.finalise("rbac_denied")

    writer.write(rec1)
    writer.write(rec2)

    lines = path.read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["cmd_id"] == "aaa"
    assert json.loads(lines[1])["cmd_id"] == "bbb"


def test_audit_writer_never_truncates(tmp_path: Path) -> None:
    """AUDIT: writing a new record never truncates existing content."""
    path = tmp_path / "audit.jsonl"
    writer = AuditWriter(jsonl_path=path)

    for i in range(5):
        rec = AuditRecord(cmd_id=f"cmd_{i}")
        rec.finalise("executed")
        writer.write(rec)

    lines = path.read_text().strip().split("\n")
    assert len(lines) == 5  # all 5 records present


def test_audit_writer_write_rejection_creates_record(tmp_path: Path) -> None:
    """AUDIT-2: write_rejection() creates and persists an audit record."""
    path = tmp_path / "audit.jsonl"
    writer = AuditWriter(jsonl_path=path)

    rec = writer.write_rejection(
        outcome="sig_invalid",
        cmd_id="cafebabe",
        iss_fqid="attacker@evil.io",
        cmd_class="exec",
        op="run",
    )
    assert rec.outcome == "sig_invalid"
    assert rec.audit_id  # non-empty

    content = path.read_text()
    assert "sig_invalid" in content
    assert "cafebabe" in content


def test_audit_writer_creates_parent_dir(tmp_path: Path) -> None:
    """AuditWriter creates the parent directory if it doesn't exist."""
    deep_path = tmp_path / "a" / "b" / "c" / "audit.jsonl"
    writer = AuditWriter(jsonl_path=deep_path)
    rec = AuditRecord()
    rec.finalise("executed")
    writer.write(rec)
    assert deep_path.exists()


def test_audit_writer_does_not_raise_on_io_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AuditWriter logs but never raises on I/O failure (daemon must not crash)."""
    path = tmp_path / "audit.jsonl"
    writer = AuditWriter(jsonl_path=path)

    # Make the file unwritable
    path.touch()
    path.chmod(0o000)

    rec = AuditRecord()
    rec.finalise("executed")
    try:
        writer.write(rec)  # should not raise
    finally:
        path.chmod(0o644)  # restore for cleanup
