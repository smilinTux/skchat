# Chat Attachments — Spec 2 (CLI + TUI) Implementation Plan

> Reuses the Spec-1 Layer-0 core (`AttachmentService`, `FileRef`/`attachments`, `media`). Surfaces only — no transport/model changes.

**Goal:** parity for file/image attachments on the CLI and TUI surfaces: send a file *as a chat message* from the CLI, and send + see attachments in the terminal UI.

**Architecture:** `skchat send-file` routes through `AttachmentService.send_attachment` (so the file appears *in the conversation* with an optional caption, not just a raw transfer); the TUI gains a `/file <path>` command and renders an attachment indicator on messages that carry `attachments`.

**Tech Stack:** Python 3.12, Click (CLI), Textual (TUI), pytest. Repo `/home/cbrd21/clawd/skcapstone-repos/skchat`. Conventions: TDD, explicit `git add`, Co-Authored-By trailer, no push, tests standalone (tmp dirs, fakes, no network), conftest keeps `SK_DESKTOP_NOTIFY=0`.

Layer-0 API (already built): `AttachmentService(identity, history, file_service, thumb_root=None).send_attachment(recipient, path: Path, caption=None) -> ChatMessage`. `FileTransferService(identity, skcomms=...)`. `ChatMessage.attachments: list[FileRef]`; `FileRef.filename/mime_type/transfer_id/...`.

---

## Task 1: `skchat send-file` posts a chat message (+ `--caption`)

**Files:** Modify `src/skchat/cli.py` (the existing `send_file_cmd`, ~line 2250). Test: `tests/test_cli.py`.

The current `send_file_cmd` calls `FileTransferService(...).send_file(...)` directly and prints a transfer id — it does NOT record the file in chat history. Route it through `AttachmentService.send_attachment` so the file shows up as a message, and add `--caption`.

- [ ] **Step 1 — failing test** (add to `tests/test_cli.py`):
```python
def test_send_file_posts_chat_message(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from unittest.mock import patch, MagicMock
    f = tmp_path / "doc.pdf"; f.write_bytes(b"%PDF-1.4 x")
    captured = {}
    fake = MagicMock()
    def _send_attachment(recipient, path, caption=None):
        from skchat.models import ChatMessage, FileRef
        captured["recipient"] = recipient; captured["caption"] = caption
        return ChatMessage(sender="capauth:me@skworld.io", recipient=recipient,
            content=caption or "",
            attachments=[FileRef(transfer_id="t1", filename=path.name, size=1,
                mime_type="application/pdf", sha256="x", direction="sent")])
    fake.send_attachment.side_effect = _send_attachment
    from click.testing import CliRunner
    from skchat import cli
    with patch.object(cli, "_attachment_service_for", return_value=fake):
        r = CliRunner().invoke(cli.main, ["send-file", "capauth:peer@skworld.io",
                                          str(f), "--caption", "look"])
    assert r.exit_code == 0, r.output
    assert captured["recipient"] == "capauth:peer@skworld.io"
    assert captured["caption"] == "look"
```

- [ ] **Step 2 — run, confirm fail:** `~/.skenv/bin/python -m pytest tests/test_cli.py -k send_file_posts -q` → FAIL (`_attachment_service_for` missing / caption option missing).

- [ ] **Step 3 — implement:** In `cli.py`, add a small factory near the other helpers:
```python
def _attachment_service_for(identity, skcomms):
    from .attachments import AttachmentService
    from .files import FileTransferService
    from .history import ChatHistory
    fs = FileTransferService(identity=identity, skcomms=skcomms)
    return AttachmentService(identity, ChatHistory(), fs)
```
Add `@click.option("--caption", default=None, help="Optional caption shown with the file.")` to `send_file_cmd` and change its body: after resolving `identity`, `skcomms`, `resolved_recipient`, replace the direct `service.send_file(...)` with:
```python
    svc = _attachment_service_for(identity, skcomms)
    msg = svc.send_attachment(resolved_recipient, file_path, caption=caption)
    transfer_id = msg.attachments[0].transfer_id
    click.echo(f"  Sent {file_path.name} ({transfer_id}) — posted to chat.")
```
Keep the existing pre-flight echoes. Preserve the function signature additions (`caption: Optional[str]`).

- [ ] **Step 4 — run, confirm pass:** `~/.skenv/bin/python -m pytest tests/test_cli.py -k send_file -q` → PASS. Also run the full `tests/test_cli.py` to confirm no regression.

- [ ] **Step 5 — commit:**
```bash
git add src/skchat/cli.py tests/test_cli.py
git commit -m "feat(cli): send-file posts a chat message via AttachmentService (+ --caption)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: TUI `/file <path>` command + attachment rendering

**Files:** Modify `src/skchat/tui.py`. Test: `tests/test_tui.py` (create if absent).

Two changes: (a) in the input handler (`_send`), recognize a leading `/file <path>` (optionally `@name /file <path>`) and dispatch a file send via the `skchat send-file` subprocess (mirrors how `_send_dm` already shells to the CLI); (b) where the TUI builds each message row (it reads `m.content`), if the underlying message has `attachments`, show a `📎 <filename>` indicator (append to, or stand in for, empty content).

- [ ] **Step 1 — failing test** (`tests/test_tui.py`): test the pure helper, not the live Textual app.
```python
from skchat.tui import format_attachment_label, parse_file_command

def test_parse_file_command():
    assert parse_file_command("/file /tmp/a.png") == (None, "/tmp/a.png")
    assert parse_file_command("@bob /file /tmp/a.png") == ("bob", "/tmp/a.png")
    assert parse_file_command("hello") is None

def test_format_attachment_label():
    assert format_attachment_label([{"filename": "p.png", "mime_type": "image/png"}]) == "📎 p.png"
    assert format_attachment_label([]) == ""
```

- [ ] **Step 2 — run, confirm fail:** `~/.skenv/bin/python -m pytest tests/test_tui.py -q` → FAIL (functions missing).

- [ ] **Step 3 — implement:** Add two module-level pure helpers to `tui.py`:
```python
def parse_file_command(text: str):
    """Parse '/file <path>' or '@name /file <path>'. Returns (recipient|None, path) or None."""
    recipient = None
    t = text.strip()
    if t.startswith("@"):
        parts = t.split(" ", 1)
        recipient = parts[0][1:]
        t = parts[1].strip() if len(parts) > 1 else ""
    if t.startswith("/file "):
        path = t[len("/file "):].strip()
        if path:
            return (recipient, path)
    return None

def format_attachment_label(attachments) -> str:
    """One-line indicator for a message's attachments (TUI message list)."""
    if not attachments:
        return ""
    names = [a.get("filename", "file") if isinstance(a, dict) else getattr(a, "filename", "file")
             for a in attachments]
    return "📎 " + ", ".join(names)
```
Wire them: in `_send`, before the existing mention/group logic, `fc = parse_file_command(text)` — if not None, call a new `_send_file(recipient or "lumina", path)` that shells `skchat send-file <recipient> <path>` (mirror `_send_dm`'s `asyncio.create_subprocess_exec` with the same env), flash status, and return. In the message-row builder (where `content = str(msg.get("content",""))`), compute `att = format_attachment_label(msg.get("attachments"))` and if `att`, set the displayed text to `att` when content is empty else `f"{content} {att}"`. Ensure `_read_messages` carries `attachments` through into the row dicts (add `"attachments": getattr(m, "attachments", [])` next to `"content"`).

- [ ] **Step 4 — run, confirm pass:** `~/.skenv/bin/python -m pytest tests/test_tui.py -q` → PASS. Import-check the TUI: `~/.skenv/bin/python -c "import skchat.tui"`.

- [ ] **Step 5 — commit:**
```bash
git add src/skchat/tui.py tests/test_tui.py
git commit -m "feat(tui): /file <path> send command + attachment indicators

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Full-suite verification

- [ ] Run `~/.skenv/bin/python -m pytest -q -p no:cacheprovider -m "not e2e_live"` — confirm green (note: one live-daemon-dependent test, `test_cli::TestStatusCommand::test_status`, can flap when a daemon is running on the box; re-run in isolation to confirm it passes — that's environmental, not a regression). No new failures vs the 755-passed baseline.
