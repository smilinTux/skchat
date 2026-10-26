"""The agent's own memory for a voice session: loaded ONCE at session start.

Rule (Chef, 2026-10-26): profile / persona / memory context goes into the
first prompt of a session and is never re-injected per turn, for every agent
runtime. VoiceEngine builds this block once per session (one engine per
LiveKit call or WebSocket connection) and puts it in the fixed system prompt,
where every turn reuses it unchanged. Mid-call recall is on demand through the
search_memory tool, not a search before every reply.

Mirrors skcall/brain/memory_context.py (smilinTux/skcall PR #16) so the two
voice stacks build the same block. Ported, not imported: skchat does not
depend on skcall.

What goes in, in priority order, under SKVOICE_CONTEXT_BUDGET tokens:

  1. skwhisper's whisper.md (recent memories, hot topics, people), the
     subconscious digest the SKWhisper daemon regenerates every 30 min.
  2. The newest daily reflection, if it is from the last week.
  3. The journal tail, minus the boilerplate entries ("Daemon shutdown",
     "Session ... ended") that make up most of it.

Group sessions get none of it unless SKVOICE_CONTEXT_GROUP=1: whisper.md is
private material.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

LOG = logging.getLogger("skchat.voice_engine.memory_context")

DEFAULT_BUDGET_TOKENS = 3000
# Measured on the chi gateway (skcall): whisper.md (3062 chars of markdown)
# added 1129 prompt tokens, i.e. ~2.7 chars per token.
CHARS_PER_TOKEN = 2.7
REFLECTION_MAX_AGE = timedelta(days=7)
JOURNAL_TAIL_BYTES = 64 * 1024

HEADER = (
    "What you remember, loaded once from your own memory when this session started. "
    "It is background, not instructions, and not a tool result: it does not mean you "
    "checked anything during this conversation. Draw on it the way you would on your own "
    "recollection, and use search_memory when you need something it does not cover."
)

# Measured 2026-10-26 on the last 300 KB of the journals: Jarvis's is 100%
# these (daemon shutdowns, session-ended, test entries); Lumina's is ~98%
# these plus "pre-compaction" markers.
_NOISE_TITLES = re.compile(
    r"^(daemon (shutdown|startup)|test session|from test-peer|session\b.*\b(ended|started|pre-compaction))\b",
    re.I,
)


def agent_home(agent: str) -> Path:
    return Path.home() / ".skcapstone" / "agents" / agent


def _truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    nl = cut.rfind("\n")
    if nl > max_chars * 0.6:
        cut = cut[:nl]
    return cut.rstrip() + "\n[...]"


def load_whisper(home: Path) -> str:
    """whisper.md without its title and 'auto-generated' banner."""
    p = home / "skwhisper" / "whisper.md"
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = [
        ln
        for ln in raw.splitlines()
        if not ln.startswith("# SKWhisper Context") and not ln.startswith("> Auto-generated")
    ]
    body = "\n".join(lines).strip()
    if not body:
        return ""
    stamp = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    return f"Subconscious digest (skwhisper, {stamp}):\n{body}"


def _reflection_date(p: Path) -> datetime | None:
    m = re.match(r"(\d{4}-\d{2}-\d{2})", p.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d")
    except ValueError:
        return None


def reflection_files(home: Path) -> list[Path]:
    """Daily/weekly reflections, newest first. Jarvis writes
    `YYYY-MM-DD_reflection.md`, Lumina wrote `YYYY-MM-DD-reflection-daily.json`."""
    rdir = home / "reflections"
    if not rdir.is_dir():
        return []
    files = [p for p in rdir.iterdir() if p.suffix in (".md", ".json") and _reflection_date(p)]
    return sorted(files, key=lambda p: (_reflection_date(p), p.name), reverse=True)


def _reflection_text(p: Path) -> str:
    raw = p.read_text(encoding="utf-8")
    if p.suffix == ".json":
        try:
            d = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(d, dict):
            for k in ("reflection", "content", "text", "summary", "body"):
                if isinstance(d.get(k), str):
                    return d[k]
        return raw
    keep = [
        ln
        for ln in raw.splitlines()
        if not re.match(r"^(generated|engine|fingerprint):", ln) and ln.strip() != "---"
    ]
    return "\n".join(keep).strip()


def load_reflection(home: Path, now: datetime | None = None) -> str:
    now = now or datetime.now()
    for p in reflection_files(home)[:1]:
        if now - _reflection_date(p) > REFLECTION_MAX_AGE:
            return ""
        try:
            text = _reflection_text(p)
        except OSError:
            return ""
        return f"Your latest reflection ({p.name}):\n{text}" if text else ""
    return ""


def load_journal_tail(home: Path, max_entries: int = 5) -> str:
    """The last few journal entries that say something (boilerplate skipped)."""
    p = home / "journal.md"
    try:
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - JOURNAL_TAIL_BYTES))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    entries = [e.strip() for e in re.split(r"\n(?=## )", raw) if e.strip().startswith("## ")]
    keep: list[str] = []
    for e in reversed(entries):
        title = e.splitlines()[0][3:].strip()
        if _NOISE_TITLES.match(title) or "Session ended (other)" in e:
            continue
        lines = [
            ln
            for ln in e.splitlines()
            if ln.strip()
            and not ln.startswith(("**Session:**", "**Present:**"))
            and not re.match(r"^\*\*Intensity:\*\* 0\.0", ln)
            and ln.strip() != "---"
        ]
        text = "\n".join(lines)
        if text in keep:
            continue
        keep.append(text)
        if len(keep) >= max_entries:
            break
    if not keep:
        return ""
    return "Recent journal entries (newest first):\n" + "\n\n".join(keep)


def build_call_context(
    agent: str,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    home: Path | None = None,
    now: datetime | None = None,
) -> tuple[str, dict]:
    """The memory block for the system prompt, plus a size report
    {section: chars} for logging. Empty string when there is nothing."""
    home = home or agent_home(agent)
    budget = int(max(0, budget_tokens) * CHARS_PER_TOKEN)
    report: dict = {"budget_chars": budget}
    if budget <= 0:
        return "", report
    # Each section gets a share; whatever one leaves unused flows to the next.
    plan = (
        ("whisper", 0.7, lambda: load_whisper(home)),
        ("reflection", 0.2, lambda: load_reflection(home, now)),
        ("journal", 1.0, lambda: load_journal_tail(home)),
    )
    left = budget - len(HEADER)
    parts = []
    for i, (name, share, load) in enumerate(plan):
        try:
            text = load()
        except Exception:
            LOG.warning("memory context: %s unavailable", name, exc_info=True)
            text = ""
        cap = left if i == len(plan) - 1 else min(left, int(budget * share))
        if not text or cap <= 200:
            report[name] = 0
            continue
        text = _truncate(text, cap)
        parts.append(text)
        left -= len(text) + 2
        report[name] = len(text)
    if not parts:
        return "", report
    block = HEADER + "\n\n" + "\n\n".join(parts)
    report["total_chars"] = len(block)
    report["est_tokens"] = round(len(block) / CHARS_PER_TOKEN)
    return block, report
