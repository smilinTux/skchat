"""Tests for SKChat CLI consent commands — requests/accept/decline/contacts.

These commands are the operator surface over the skcomms first-contact consent
gate. The backend (skcomms.consent_requests / consent_pipeline) owns all state;
the CLI just resolves the running agent (SKAGENT) and renders/drives it.

State is isolated per test via SKCOMMS_HOME (skcomms_home() honors it) so a fresh
in-process CliRunner invocation sees exactly what the test enqueued.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from skchat.cli import main

AGENT = "testbot"
SENDER = "alice@operator.realm"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def consent_env(tmp_path, monkeypatch):
    """Isolate consent state to a tmp SKCOMMS_HOME and pin the running agent."""
    monkeypatch.setenv("SKCOMMS_HOME", str(tmp_path / "skcomms"))
    monkeypatch.setenv("SKAGENT", AGENT)
    monkeypatch.delenv("SKCAPSTONE_AGENT", raising=False)
    monkeypatch.delenv("SKMEMORY_AGENT", raising=False)
    return tmp_path


def _enqueue(sender: str = SENDER, envelope_id: str = "env-1") -> None:
    """Drop a first-contact knock into the agent's quarantine queue."""
    from skcomms.consent import RequestQueue

    RequestQueue(AGENT).enqueue(sender, b"hello there", envelope_id=envelope_id)


def test_requests_lists_pending(runner, consent_env):
    _enqueue()
    result = runner.invoke(main, ["requests"])
    assert result.exit_code == 0, result.output
    assert SENDER in result.output


def test_requests_empty(runner, consent_env):
    result = runner.invoke(main, ["requests"])
    assert result.exit_code == 0, result.output
    # No crash, no pending sender shown.
    assert SENDER not in result.output


def test_accept_promotes_and_mints_token(runner, consent_env):
    _enqueue()
    result = runner.invoke(main, ["accept", SENDER])
    assert result.exit_code == 0, result.output
    assert f"Accepted {SENDER}" in result.output

    # The sender is now a known contact.
    from skcomms.consent_requests import list_known

    assert SENDER in list_known(AGENT)

    # The minted delivery token is deterministic (HKDF-derived) and verifies
    # against the per-contact TokenStore. on_accept returns it; assert it both
    # verifies and is surfaced in the CLI output (a leading fragment survives any
    # rich line-wrapping intact since the token is one unbroken word).
    from skcomms.consent_tokens import TokenStore

    store = TokenStore(AGENT)
    expected = store.issue(SENDER)  # idempotent: same token on repeat
    assert store.verify(SENDER, expected)
    assert expected[:16] in result.output, result.output


def test_accept_clears_from_requests(runner, consent_env):
    _enqueue()
    runner.invoke(main, ["accept", SENDER])
    after = runner.invoke(main, ["requests"])
    assert after.exit_code == 0, after.output
    assert SENDER not in after.output


def test_decline_removes_request(runner, consent_env):
    _enqueue()
    result = runner.invoke(main, ["decline", SENDER])
    assert result.exit_code == 0, result.output
    assert SENDER in result.output  # echoes which sender was declined

    from skcomms.consent import ContactStore

    # Declined (not blocked) → returns to UNKNOWN, not known, not blocked.
    cs = ContactStore(AGENT)
    assert not cs.is_known(SENDER)
    assert not cs.is_blocked(SENDER)
    # Queue cleared.
    after = runner.invoke(main, ["requests"])
    assert SENDER not in after.output


def test_decline_with_block(runner, consent_env):
    _enqueue()
    result = runner.invoke(main, ["decline", SENDER, "--block"])
    assert result.exit_code == 0, result.output

    from skcomms.consent import ContactStore

    assert ContactStore(AGENT).is_blocked(SENDER)


def test_contacts_lists_known(runner, consent_env):
    _enqueue()
    runner.invoke(main, ["accept", SENDER])
    result = runner.invoke(main, ["contacts"])
    assert result.exit_code == 0, result.output
    assert SENDER in result.output


def test_contacts_empty(runner, consent_env):
    result = runner.invoke(main, ["contacts"])
    assert result.exit_code == 0, result.output
    assert SENDER not in result.output
