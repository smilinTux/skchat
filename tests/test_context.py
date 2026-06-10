"""Tests for memory-context injection in :func:`skchat.context.fetch_context`.

The reply path (lumina-bridge / opus-bridge / webui) assembles its prompt
from ``fetch_context``. Historically that returned only chat *history*.
These tests pin the behaviour that relevant *memory* snippets are merged in
when a memory source is available, and that the function degrades to
history-only when memory is absent.

No network: the memory source is always injected as a mock callable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path


def _build_history(tmp_path: Path):
    """A real ChatHistory with a small group-thread conversation."""
    from skchat.history import ChatHistory
    from skchat.models import ChatMessage

    history = ChatHistory(store=None, history_dir=tmp_path / "history")

    chef = "capauth:chef@skworld.io"
    lumina = "capauth:lumina@skworld.io"
    group = "group-mem"
    base_ts = datetime.now(timezone.utc) - timedelta(minutes=10)

    for i, (sender, content) in enumerate(
        [(chef, "did we ship the release?"), (lumina, "yes, tagged v1.2")]
    ):
        msg = ChatMessage(
            sender=sender,
            recipient=lumina if sender == chef else chef,
            content=content,
            thread_id=group,
            timestamp=base_ts + timedelta(seconds=i * 30),
        )
        msg.id = f"msg-{i}"
        history.save(msg)

    return history, chef, lumina, group


class TestMemoryContextInjection:
    def test_memory_snippets_injected_when_source_returns_hits(
        self, tmp_path: Path
    ) -> None:
        from skchat.context import fetch_context

        history, chef, lumina, group = _build_history(tmp_path)

        calls: list[str] = []

        def fake_memory_source(query: str, limit: int):
            calls.append(query)
            return [
                "Chef prefers concise replies",
                "release v1.2 shipped on 2026-06-09",
            ]

        ctx = fetch_context(
            self_identity=lumina,
            sender=chef,
            thread_id=group,
            limit=10,
            history=history,
            memory_source=fake_memory_source,
        )

        # History is still present.
        assert "did we ship the release?" in ctx
        assert "tagged v1.2" in ctx

        # Memory snippets are merged in, clearly delimited.
        assert "Chef prefers concise replies" in ctx
        assert "release v1.2 shipped on 2026-06-09" in ctx
        assert "Relevant memories" in ctx

        # The memory source was actually queried.
        assert calls, "memory_source was never called"

    def test_history_only_when_memory_source_absent(self, tmp_path: Path) -> None:
        from skchat.context import fetch_context

        history, chef, lumina, group = _build_history(tmp_path)

        ctx = fetch_context(
            self_identity=lumina,
            sender=chef,
            thread_id=group,
            limit=10,
            history=history,
        )

        assert "did we ship the release?" in ctx
        assert "Relevant memories" not in ctx

    def test_memory_source_failure_degrades_to_history(self, tmp_path: Path) -> None:
        from skchat.context import fetch_context

        history, chef, lumina, group = _build_history(tmp_path)

        def boom(query: str, limit: int):
            raise RuntimeError("skmemory down")

        ctx = fetch_context(
            self_identity=lumina,
            sender=chef,
            thread_id=group,
            limit=10,
            history=history,
            memory_source=boom,
        )

        assert "did we ship the release?" in ctx
        assert "Relevant memories" not in ctx

    def test_empty_memory_hits_omits_memory_block(self, tmp_path: Path) -> None:
        from skchat.context import fetch_context

        history, chef, lumina, group = _build_history(tmp_path)

        ctx = fetch_context(
            self_identity=lumina,
            sender=chef,
            thread_id=group,
            limit=10,
            history=history,
            memory_source=lambda query, limit: [],
        )

        assert "did we ship the release?" in ctx
        assert "Relevant memories" not in ctx
