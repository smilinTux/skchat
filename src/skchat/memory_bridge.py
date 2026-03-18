"""MemoryBridge -- capture SKChat conversation threads as skcapstone memories.

Fetches recent thread messages from ChatHistory, formats them as a readable
conversation transcript, and forwards them to the skcapstone session_capture
MCP tool over HTTP JSON-RPC.

Usage:
    bridge = MemoryBridge.from_config()
    result = bridge.capture_thread("thread-abc123")
    results = bridge.auto_capture()   # captures threads with 5+ msgs / 24h
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from .history import ChatHistory

logger = logging.getLogger(__name__)

SKCAPSTONE_MCP_URL = "http://127.0.0.1:9475/mcp"
_CAPTURE_TIMEOUT = 15  # seconds per HTTP call


class MemoryBridge:
    """Bridge between SKChat conversation threads and skcapstone memory.

    Captures chat threads as sovereign memories by calling the skcapstone
    MCP session_capture tool over HTTP JSON-RPC (MCP 1.x streamable-http
    transport).

    Args:
        history: ChatHistory instance used for thread retrieval.
        mcp_url: Base URL of the skcapstone MCP HTTP server.
    """

    def __init__(
        self,
        history: ChatHistory,
        mcp_url: str = SKCAPSTONE_MCP_URL,
    ) -> None:
        self._history = history
        self._mcp_url = mcp_url
        self._req_id = 0

    @classmethod
    def from_config(cls, mcp_url: str = SKCAPSTONE_MCP_URL) -> "MemoryBridge":
        """Create a MemoryBridge backed by the default ChatHistory.

        Args:
            mcp_url: Override the skcapstone MCP URL (default: localhost:9475).

        Returns:
            MemoryBridge: Ready-to-use instance.
        """
        return cls(history=ChatHistory.from_config(), mcp_url=mcp_url)

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def capture_thread(
        self,
        thread_id: str,
        min_importance: float = 0.5,
    ) -> dict:
        """Capture the last 50 messages of a thread as skcapstone memories.

        Fetches messages from ChatHistory, formats them as a human-readable
        conversation transcript, then POSTs to the skcapstone MCP
        session_capture tool so the agent retains the conversation.

        Args:
            thread_id: The thread identifier to capture.
            min_importance: Minimum importance threshold for stored memories
                (passed to session_capture, default 0.5).

        Returns:
            dict: Result payload from skcapstone or an error dict.
        """
        messages = self._history.get_thread_messages(thread_id, limit=50)
        if not messages:
            logger.debug("capture_thread: no messages found for thread %s", thread_id)
            return {"error": f"no messages found for thread {thread_id!r}"}

        conversation_text = self._format_conversation(thread_id, messages)
        tags = ["skchat", thread_id]

        return self._call_session_capture(
            content=conversation_text,
            tags=tags,
            min_importance=min_importance,
            source=f"skchat:thread:{thread_id}",
        )

    def auto_capture(self) -> list[dict]:
        """Find active threads and capture each one.

        Scans messages from the past 24 hours, groups them by thread, and
        captures every thread that has 5 or more messages.

        Returns:
            list[dict]: One result dict per captured thread.
        """
        recent_messages = self._history.get_messages_since(minutes=24 * 60)

        thread_counts: dict[str, int] = {}
        for msg in recent_messages:
            tid = msg.get("thread_id")
            if tid:
                thread_counts[tid] = thread_counts.get(tid, 0) + 1

        eligible = [tid for tid, count in thread_counts.items() if count >= 5]

        if not eligible:
            logger.debug("auto_capture: no eligible threads (need 5+ msgs in last 24h)")
            return []

        results: list[dict] = []
        for tid in eligible:
            logger.info(
                "auto_capture: capturing thread %s (%d msgs in last 24h)",
                tid[:12],
                thread_counts[tid],
            )
            result = self.capture_thread(tid)
            result["thread_id"] = tid
            results.append(result)

        return results

    # -----------------------------------------------------------------
    # Formatting helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _format_conversation(thread_id: str, messages: list[dict]) -> str:
        """Format message dicts as a readable chronological transcript.

        Messages arrive newest-first from ChatHistory; we reverse for output.

        Args:
            thread_id: Thread identifier used in the header.
            messages: Message dicts as returned by get_thread_messages.

        Returns:
            str: Multi-line conversation transcript.
        """
        lines = [f"# SKChat Thread: {thread_id}", ""]
        for msg in reversed(messages):
            sender = msg.get("sender") or "unknown"
            short_sender = sender.split("@")[0].replace("capauth:", "")
            ts = msg.get("timestamp", "")
            if hasattr(ts, "isoformat"):
                ts = ts.isoformat()
            content = (msg.get("content") or "").strip()
            lines.append(f"[{ts}] {short_sender}: {content}")
        return "\n".join(lines)

    # -----------------------------------------------------------------
    # MCP HTTP call
    # -----------------------------------------------------------------

    def _call_session_capture(
        self,
        content: str,
        tags: list[str],
        min_importance: float,
        source: str = "skchat",
    ) -> dict:
        """POST a session_capture tool call to the skcapstone MCP server.

        Uses the JSON-RPC 2.0 / MCP 1.x tools/call wire format over HTTP.
        The response wraps the tool result in result.content[0].text as
        a JSON string, which we unwrap before returning.

        Args:
            content: Conversation transcript to store.
            tags: Memory tags to apply (forwarded to session_capture).
            min_importance: Lower-bound importance score (0.0-1.0).
            source: Source label embedded in stored memories.

        Returns:
            dict: Unwrapped session_capture result or an error dict.
        """
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": "tools/call",
            "params": {
                "name": "session_capture",
                "arguments": {
                    "content": content,
                    "tags": tags,
                    "source": source,
                    "min_importance": min_importance,
                },
            },
        }

        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._mcp_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=_CAPTURE_TIMEOUT) as resp:
                raw = resp.read().decode()
        except urllib.error.URLError as exc:
            logger.warning(
                "MemoryBridge: skcapstone MCP unavailable at %s: %s",
                self._mcp_url,
                exc,
            )
            return {"error": f"skcapstone unreachable: {exc}"}
        except Exception as exc:
            logger.warning("MemoryBridge: HTTP request failed: %s", exc)
            return {"error": str(exc)}

        try:
            response = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("MemoryBridge: invalid JSON response: %s", exc)
            return {"error": f"invalid response: {exc}"}

        if "error" in response:
            err = response["error"]
            logger.warning("MemoryBridge: session_capture error: %s", err)
            return {"error": err}

        result = response.get("result", {})

        # MCP tools/call wraps the tool output in result.content[0].text
        if isinstance(result, dict) and "content" in result:
            try:
                text = result["content"][0].get("text", "{}")
                return json.loads(text)
            except (KeyError, IndexError, json.JSONDecodeError):
                pass

        return result if isinstance(result, dict) else {"raw": result}
