"""SKChat TUI — terminal chat interface built with Textual.

Launch with:
    skchat tui
    skchat-tui
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Optional

from rich.markup import escape as _markup_escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.widgets import Input, Label, Static

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SELF_IDENTITY: str = os.environ.get("SKCHAT_IDENTITY", "capauth:opus@skworld.io")
GROUP_ID: str = "d4f3281e-fa92-474c-a8cd-f0a2a4c31c33"
GROUP_NAME: str = "skworld-team"
KNOWN_PEERS: list[str] = ["lumina", "chef", "claude", "opus"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short_sender(sender: str) -> str:
    """Extract friendly name from a capauth URI like capauth:lumina@skworld.io."""
    if ":" in sender:
        name = sender.split(":", 1)[1]
        if "@" in name:
            name = name.split("@", 1)[0]
        return name
    return sender


def _peer_css_class(sender: str) -> str:
    name = _short_sender(sender).lower()
    if sender == SELF_IDENTITY or name == "opus":
        return "msg-self"
    if name == "lumina":
        return "msg-lumina"
    if name == "chef":
        return "msg-chef"
    return "msg-other"


def _common_prefix(strs: list[str]) -> str:
    if not strs:
        return ""
    prefix = strs[0]
    for s in strs[1:]:
        while not s.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix


def _fetch_recent_messages() -> list[dict]:
    """Read recent chat messages from JSONL history files."""
    import hashlib

    def _to_uid(sender: str, ts: object, content: str) -> str:
        return hashlib.sha1(f"{sender}{ts}{content}".encode()).hexdigest()

    # Primary: in-process via history.load() — reads JSONL files, same source as CLI
    try:
        from skchat.history import ChatHistory

        history = ChatHistory.from_config()
        raw = history.load(limit=100)
        # load() returns newest-first; reverse to oldest-first for display
        messages = []
        for m in reversed(raw):
            messages.append(
                {
                    "memory_id": _to_uid(m.sender, m.timestamp, m.content),
                    "sender": m.sender,
                    "recipient": getattr(m, "recipient", ""),
                    "content": m.content,
                    "thread_id": getattr(m, "thread_id", ""),
                    "timestamp": str(m.timestamp) if m.timestamp else "",
                }
            )
        return messages
    except Exception:
        pass

    # Fallback: subprocess (avoids skmemory namespace collision in dev envs)
    try:
        import json
        import subprocess

        result = subprocess.run(
            ["skchat", "inbox", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            raw = json.loads(result.stdout or "[]")
            out = []
            for m in raw:
                m["memory_id"] = _to_uid(
                    m.get("sender", ""), m.get("timestamp", ""), m.get("content", "")
                )
                out.append(m)
            out.sort(key=lambda d: str(d.get("timestamp", "")))
            return out[-100:]
    except Exception:
        pass

    return []


# ---------------------------------------------------------------------------
# TUI App
# ---------------------------------------------------------------------------


class SKChatTUI(App):
    CSS = """
    Screen { background: #0a0a0a; }
    #messages { height: 1fr; border: solid #333; padding: 1; }
    #input-bar { height: 3; dock: bottom; }
    #status { height: 1; dock: top; background: #111; color: #888; }
    .msg-self { color: #4a9eff; text-align: right; }
    .msg-other { color: #e0e0e0; }
    .msg-lumina { color: #c084fc; }
    .msg-chef { color: #f59e0b; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+g", "toggle_group", "Group"),
        Binding("tab", "complete_mention", "@mention", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._seen_ids: set[str] = set()
        self._group_mode: bool = False

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(
            f"SKChat | {SELF_IDENTITY} | group: {GROUP_NAME}  [ctrl+g=group  ctrl+c=quit]",
            id="status",
        )
        yield ScrollableContainer(id="messages")
        yield Input(
            placeholder="Message... (@lumina @chef — tab to complete, ctrl+g for group)",
            id="input-bar",
        )

    def on_mount(self) -> None:
        self.set_interval(3.0, self.poll_messages)
        self.call_after_refresh(self.poll_messages)

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def poll_messages(self) -> None:
        """Fetch new messages and append them to the messages panel."""
        messages = await asyncio.to_thread(_fetch_recent_messages)
        container = self.query_one("#messages", ScrollableContainer)
        new_found = False
        for msg in messages:
            msg_id = str(msg.get("memory_id", msg.get("chat_message_id", "")))
            if not msg_id or msg_id in self._seen_ids:
                continue
            self._seen_ids.add(msg_id)
            new_found = True

            sender = str(msg.get("sender", "unknown"))
            content = str(msg.get("content", ""))
            ts_raw = msg.get("timestamp", "")
            try:
                if isinstance(ts_raw, str):
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                else:
                    ts = ts_raw
                ts_str = ts.strftime("%H:%M")
            except Exception:
                ts_str = "--:--"

            short = _short_sender(sender)
            css_cls = _peer_css_class(sender)
            # Escape dynamic content to avoid Rich markup interpretation.
            # Use \[ to prevent [HH:MM] being parsed as a Rich tag.
            label_text = f"\\[{ts_str}] {_markup_escape(short)}: {_markup_escape(content)}"
            container.mount(Label(label_text, classes=css_cls))

        if new_found:
            container.scroll_end(animate=False)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()
        await self._send(text)

    async def _send(self, text: str) -> None:
        """Parse text for @mention prefix then dispatch via skchat CLI."""
        recipient: Optional[str] = None

        # @mention at start overrides group mode
        if text.startswith("@"):
            parts = text.split(" ", 1)
            mention = parts[0][1:]  # strip leading @
            if len(parts) < 2 or not parts[1].strip():
                self._status_flash("Usage: @name message")
                return
            recipient = mention
            text = parts[1].strip()

        if recipient is None and self._group_mode:
            await self._send_group(text)
        else:
            await self._send_dm(recipient or "lumina", text)

    async def _send_dm(self, recipient: str, text: str) -> None:
        env = {**os.environ, "SKCHAT_IDENTITY": SELF_IDENTITY}
        try:
            proc = await asyncio.create_subprocess_exec(
                "skchat",
                "send",
                recipient,
                text,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
            ok = proc.returncode == 0
        except Exception:
            ok = False
        ts_str = datetime.now().strftime("%H:%M")
        suffix = "" if ok else " [!]"
        container = self.query_one("#messages", ScrollableContainer)
        label_text = (
            f"\\[{ts_str}] you → {_markup_escape(recipient)}: {_markup_escape(text)}{suffix}"
        )
        container.mount(Label(label_text, classes="msg-self"))
        container.scroll_end(animate=False)

    async def _send_group(self, text: str) -> None:
        env = {**os.environ, "SKCHAT_IDENTITY": SELF_IDENTITY}
        try:
            proc = await asyncio.create_subprocess_exec(
                "skchat",
                "group-send",
                GROUP_ID,
                text,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
            ok = proc.returncode == 0
        except Exception:
            ok = False
        ts_str = datetime.now().strftime("%H:%M")
        suffix = "" if ok else " [!]"
        container = self.query_one("#messages", ScrollableContainer)
        label_text = f"\\[{ts_str}] you → {GROUP_NAME}: {_markup_escape(text)}{suffix}"
        container.mount(Label(label_text, classes="msg-self"))
        container.scroll_end(animate=False)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_toggle_group(self) -> None:
        """Ctrl+G: toggle group-send mode."""
        self._group_mode = not self._group_mode
        status = self.query_one("#status", Static)
        inp = self.query_one("#input-bar", Input)
        if self._group_mode:
            status.update(
                f"SKChat | {SELF_IDENTITY} | [GROUP MODE → {GROUP_NAME}]  [ctrl+g=exit group]"
            )
            inp.placeholder = f"Message to {GROUP_NAME}... (ctrl+g to exit group mode)"
        else:
            status.update(
                f"SKChat | {SELF_IDENTITY} | group: {GROUP_NAME}  [ctrl+g=group  ctrl+c=quit]"
            )
            inp.placeholder = "Message... (@lumina @chef — tab to complete, ctrl+g for group)"

    def action_complete_mention(self) -> None:
        """Tab: @mention completion from KNOWN_PEERS."""
        inp = self.query_one("#input-bar", Input)
        val = inp.value

        if not val.startswith("@"):
            inp.value = "@"
            inp.cursor_position = 1
            return

        # Extract the partial name after @
        space_pos = val.find(" ")
        if space_pos != -1:
            # Cursor is past the mention — do nothing
            return

        partial = val[1:].lower()
        matches = [p for p in KNOWN_PEERS if p.startswith(partial)]

        if not matches:
            return
        if len(matches) == 1:
            inp.value = f"@{matches[0]} "
            inp.cursor_position = len(inp.value)
        else:
            prefix = _common_prefix(matches)
            new_val = f"@{prefix}"
            if new_val != val:
                inp.value = new_val
                inp.cursor_position = len(new_val)

    def _status_flash(self, msg: str) -> None:
        """Briefly show a message in the status bar."""
        status = self.query_one("#status", Static)
        status.update(f"SKChat | {msg}")
        self.set_timer(2.0, self._restore_status)

    def _restore_status(self) -> None:
        status = self.query_one("#status", Static)
        if self._group_mode:
            status.update(
                f"SKChat | {SELF_IDENTITY} | [GROUP MODE → {GROUP_NAME}]  [ctrl+g=exit group]"
            )
        else:
            status.update(
                f"SKChat | {SELF_IDENTITY} | group: {GROUP_NAME}  [ctrl+g=group  ctrl+c=quit]"
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Launch the SKChat TUI."""
    app = SKChatTUI()
    app.run()


if __name__ == "__main__":
    main()
