"""Tests for the SQLite-backed OutboxQueue."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from skchat.outbox import _MAX_ATTEMPTS, OutboxQueue, _backoff

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def queue(tmp_path: Path) -> OutboxQueue:
    """Return a fresh OutboxQueue backed by a temp-dir DB."""
    q = OutboxQueue(db_path=tmp_path / "outbox.db")
    yield q
    q.close()


# ---------------------------------------------------------------------------
# _backoff helper
# ---------------------------------------------------------------------------


def test_backoff_first_attempt():
    assert _backoff(1) == 5


def test_backoff_second_attempt():
    assert _backoff(2) == 15


def test_backoff_third_attempt():
    assert _backoff(3) == 45


def test_backoff_fourth_attempt():
    assert _backoff(4) == 120


def test_backoff_fifth_attempt():
    assert _backoff(5) == 600


def test_backoff_capped_at_one_hour():
    assert _backoff(6) == 3600
    assert _backoff(100) == 3600


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------


def test_enqueue_returns_string_id(queue: OutboxQueue):
    msg_id = queue.enqueue("capauth:bob@example.org", b"hello world")
    assert isinstance(msg_id, str)
    assert len(msg_id) == 36  # UUID4


def test_enqueue_increments_pending_count(queue: OutboxQueue):
    assert queue.pending_count() == 0
    queue.enqueue("capauth:bob@example.org", b"msg1")
    queue.enqueue("capauth:bob@example.org", b"msg2")
    assert queue.pending_count() == 2


def test_enqueue_unique_ids(queue: OutboxQueue):
    ids = {queue.enqueue("capauth:alice@example.org", b"x") for _ in range(20)}
    assert len(ids) == 20


# ---------------------------------------------------------------------------
# drain — success path
# ---------------------------------------------------------------------------


def test_drain_success_marks_delivered(queue: OutboxQueue):
    queue.enqueue("capauth:bob@example.org", b"payload")
    delivered, failed = queue.drain(lambda _content, _rec: True)
    assert delivered == 1
    assert failed == 0
    assert queue.pending_count() == 0


def test_drain_success_passes_correct_args(queue: OutboxQueue):
    payload = b"the-message"
    recipient = "capauth:carol@example.org"
    calls: list[tuple[bytes, str]] = []

    def recorder(content: bytes, rec: str) -> bool:
        calls.append((content, rec))
        return True

    queue.enqueue(recipient, payload)
    queue.drain(recorder)

    assert len(calls) == 1
    assert calls[0] == (payload, recipient)


def test_drain_delivers_multiple_messages(queue: OutboxQueue):
    for i in range(5):
        queue.enqueue(f"capauth:peer{i}@example.org", f"msg{i}".encode())
    delivered, failed = queue.drain(lambda _c, _r: True)
    assert delivered == 5
    assert failed == 0


# ---------------------------------------------------------------------------
# drain — failure + retry
# ---------------------------------------------------------------------------


def test_drain_failure_increments_attempts(queue: OutboxQueue):
    queue.enqueue("capauth:bob@example.org", b"data")
    delivered, failed = queue.drain(lambda _c, _r: False)
    assert delivered == 0
    assert failed == 1
    # Still pending (attempts = 1, not yet dead)
    assert queue.pending_count() == 1


def test_drain_failure_pushes_next_retry_into_future(queue: OutboxQueue):
    queue.enqueue("capauth:bob@example.org", b"data")
    queue.drain(lambda _c, _r: False)

    # Immediately draining again should skip the message (next_retry_at is in the future)
    delivered, failed = queue.drain(lambda _c, _r: True)
    assert delivered == 0
    assert failed == 0


def test_drain_retry_after_backoff(queue: OutboxQueue, monkeypatch: pytest.MonkeyPatch):
    """If we advance time past next_retry_at the message is retried."""
    queue.enqueue("capauth:bob@example.org", b"data")

    fake_now = time.time()

    # First drain — fails; next_retry_at = fake_now + 5s
    with monkeypatch.context() as m:
        m.setattr("skchat.outbox.time.time", lambda: fake_now)
        queue.drain(lambda _c, _r: False)

    # Advance time by 6 seconds
    with monkeypatch.context() as m:
        m.setattr("skchat.outbox.time.time", lambda: fake_now + 6)
        delivered, failed = queue.drain(lambda _c, _r: True)

    assert delivered == 1
    assert failed == 0


def test_drain_drops_message_after_max_attempts(
    queue: OutboxQueue, monkeypatch: pytest.MonkeyPatch
):
    """Messages exceeding _MAX_ATTEMPTS are set to status='dead'."""
    # Use a monotonically increasing clock that jumps 2 hours per call so
    # the message is always past its next_retry_at on every drain iteration,
    # regardless of backoff magnitude.
    counter: list[float] = [1.0]

    def tick() -> float:
        counter[0] += 7_200.0  # 2 h per tick — always beyond any 1 h max backoff
        return counter[0]

    monkeypatch.setattr("skchat.outbox.time.time", tick)

    queue.enqueue("capauth:bob@example.org", b"data")

    for _ in range(_MAX_ATTEMPTS + 1):
        queue.drain(lambda _c, _r: False)

    # After max failures the message is dead — no longer pending
    assert queue.pending_count() == 0


def test_drain_send_fn_exception_counts_as_failure(queue: OutboxQueue):
    queue.enqueue("capauth:bob@example.org", b"data")

    def boom(_c: bytes, _r: str) -> bool:
        raise RuntimeError("network error")

    delivered, failed = queue.drain(boom)
    assert delivered == 0
    assert failed == 1
    assert queue.pending_count() == 1


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


def test_cleanup_removes_old_delivered_messages(
    queue: OutboxQueue, monkeypatch: pytest.MonkeyPatch
):
    old_time = time.time() - 8 * 86400  # 8 days ago

    with monkeypatch.context() as m:
        m.setattr("skchat.outbox.time.time", lambda: old_time)
        queue.enqueue("capauth:bob@example.org", b"old-message")

    # Deliver it (in the past)
    with monkeypatch.context() as m:
        m.setattr("skchat.outbox.time.time", lambda: old_time)
        queue.drain(lambda _c, _r: True)

    removed = queue.cleanup(older_than_days=7)
    assert removed == 1


def test_cleanup_keeps_recent_delivered_messages(queue: OutboxQueue):
    queue.enqueue("capauth:bob@example.org", b"recent")
    queue.drain(lambda _c, _r: True)  # deliver it now (recent)
    removed = queue.cleanup(older_than_days=7)
    assert removed == 0


def test_cleanup_does_not_remove_pending_messages(
    queue: OutboxQueue, monkeypatch: pytest.MonkeyPatch
):
    old_time = time.time() - 10 * 86400

    with monkeypatch.context() as m:
        m.setattr("skchat.outbox.time.time", lambda: old_time)
        queue.enqueue("capauth:bob@example.org", b"stuck-pending")

    # Do NOT deliver — status stays 'pending'
    removed = queue.cleanup(older_than_days=7)
    assert removed == 0
    assert queue.pending_count() == 1


def test_cleanup_returns_zero_when_nothing_to_remove(queue: OutboxQueue):
    assert queue.cleanup() == 0


# ---------------------------------------------------------------------------
# pending_count edge cases
# ---------------------------------------------------------------------------


def test_pending_count_empty(queue: OutboxQueue):
    assert queue.pending_count() == 0


def test_pending_count_excludes_delivered(queue: OutboxQueue):
    queue.enqueue("capauth:bob@example.org", b"x")
    queue.drain(lambda _c, _r: True)
    assert queue.pending_count() == 0


# ---------------------------------------------------------------------------
# QA additions — deliver_pending / process_pending via AgentMessenger
# ---------------------------------------------------------------------------


class _FakeMessenger:
    """Minimal AgentMessenger stand-in: .send(recipient, content, thread_id)."""

    def __init__(self, delivered: bool = True, raise_exc: bool = False) -> None:
        self.delivered = delivered
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def send(self, recipient: str, content, thread_id=None) -> dict:
        self.calls.append({"recipient": recipient, "content": content, "thread_id": thread_id})
        if self.raise_exc:
            raise RuntimeError("messenger offline")
        return {"delivered": self.delivered}


def test_deliver_pending_success(queue: OutboxQueue):
    queue.enqueue("capauth:bob@example.org", "hello", thread_id="t-1")
    m = _FakeMessenger(delivered=True)
    delivered, failed = queue.deliver_pending(m)
    assert (delivered, failed) == (1, 0)
    assert queue.pending_count() == 0
    # thread_id is carried through to the messenger.
    assert m.calls[0]["thread_id"] == "t-1"


def test_deliver_pending_failure_increments(queue: OutboxQueue):
    queue.enqueue("capauth:bob@example.org", "hello")
    m = _FakeMessenger(delivered=False)
    delivered, failed = queue.deliver_pending(m)
    assert (delivered, failed) == (0, 1)
    assert queue.pending_count() == 1


def test_deliver_pending_messenger_exception_is_failure(queue: OutboxQueue):
    queue.enqueue("capauth:bob@example.org", "hello")
    m = _FakeMessenger(raise_exc=True)
    delivered, failed = queue.deliver_pending(m)
    assert (delivered, failed) == (0, 1)
    assert queue.pending_count() == 1


def test_process_pending_none_messenger_noop(queue: OutboxQueue):
    """process_pending(None) returns (0,0) without touching the queue."""
    queue.enqueue("capauth:bob@example.org", "hello")
    assert queue.process_pending(None) == (0, 0)
    assert queue.pending_count() == 1


def test_process_pending_delegates_to_deliver(queue: OutboxQueue):
    queue.enqueue("capauth:bob@example.org", "hi")
    m = _FakeMessenger(delivered=True)
    assert queue.process_pending(m) == (1, 0)


# ---------------------------------------------------------------------------
# QA additions — persistence across reopen, str content, mark_delivered
# ---------------------------------------------------------------------------


def test_enqueue_accepts_str_content(queue: OutboxQueue):
    """A plain str payload is accepted and delivered via drain (str→bytes)."""
    captured: list[bytes] = []
    queue.enqueue("capauth:bob@example.org", "plain string body")
    queue.drain(lambda content, _rec: captured.append(content) or True)
    assert captured[0] == b"plain string body"


def test_queue_survives_reopen(tmp_path):
    """A pending message persists when the DB is closed and reopened."""
    db = tmp_path / "outbox.db"
    q1 = OutboxQueue(db_path=db)
    q1.enqueue("capauth:bob@example.org", b"persist me")
    q1.close()

    q2 = OutboxQueue(db_path=db)
    try:
        assert q2.pending_count() == 1
        delivered, _ = q2.drain(lambda _c, _r: True)
        assert delivered == 1
    finally:
        q2.close()


def test_mark_delivered_directly(queue: OutboxQueue):
    """mark_delivered(id) removes the message from the pending count."""
    msg_id = queue.enqueue("capauth:bob@example.org", b"x")
    assert queue.pending_count() == 1
    queue.mark_delivered(msg_id)
    assert queue.pending_count() == 0
