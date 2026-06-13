"""Built-in tool registry for the voice engine.

Registers: search_memory, narrate, worship_session, create_bloom_anchor,
list_reflections (+ read_reflection, worship_list, worship_replay).

Tool schemas are ported verbatim from lumina-creative/scripts/lumina-call.py
(TOOLS list, 2026-06-12).  Handler implementations are ported from _run_tool()
in the same file.

worship_session / worship_replay depend on the Conversation object from
lumina-creative — those handlers carry a # TODO(phase3) stub.
worship_list also has a stub because the listing function lives in the
orchestrator.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import httpx

from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.tools import Tool, ToolRegistry

log = logging.getLogger("skchat.voice_engine.builtin_tools")


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

_SCHEMA_SEARCH_MEMORY = {
    "type": "function",
    "function": {
        "name": "search_memory",
        "description": (
            "Search the agent's persistent memory store (skmemory) for relevant "
            "session digests, journal entries, seeds, and project notes. Returns "
            "title + truncated content per match. Use when asked about specific "
            "past topics ('what did we decide about X', 'remember when'), or to "
            "ground a reply in real history."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — keywords, topic, or partial phrase",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 5, max 10)",
                },
            },
            "required": ["query"],
        },
    },
}

_SCHEMA_NARRATE = {
    "type": "function",
    "function": {
        "name": "narrate",
        "description": (
            "Generate a worship story, sacred scene, mature narrative, or any "
            "uncensored creative writing using the local abliterated model "
            "(LUMINA_NARRATE_URL). Use ANY time the operator asks for a "
            "worship/sacred/sexual/mature story or scene. Returns the generated "
            "narrative as text — read it back in your voice. SACRED-MODE ONLY."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Full creative-writing prompt. Be specific about characters, "
                        "setting, tone, intensity, kink, dynamic."
                    ),
                },
                "length": {
                    "type": "string",
                    "description": (
                        "'short' (~200 words), 'medium' (~500 words, default), or "
                        "'long' (~1200 words)."
                    ),
                },
                "system": {
                    "type": "string",
                    "description": (
                        "Optional override for the narrator system prompt. Default primes "
                        "the model for vivid second-person prose. Leave blank for default."
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
}

_SCHEMA_WORSHIP_SESSION = {
    "type": "function",
    "function": {
        "name": "worship_session",
        "description": (
            "Build and play a full worship session: 15-scene image set + matching "
            "narrative + audio. ~5-7 min build time. Call when the operator asks for "
            "a worship session, full scene, or 15-image experience. SACRED-MODE ONLY."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Natural-language description of the worship scene.",
                },
                "image_count": {
                    "type": "integer",
                    "description": "Number of scenes (default 15, range 5-30).",
                },
                "loop": {
                    "type": "boolean",
                    "description": "Loop the playback until stopped (default true).",
                },
            },
            "required": ["prompt"],
        },
    },
}

_SCHEMA_WORSHIP_LIST = {
    "type": "function",
    "function": {
        "name": "worship_list",
        "description": (
            "List previously-built worship sessions. Returns recent sessions with "
            "their original prompt, scene count, and audio duration. Use when the "
            "operator asks 'what worship memories do we have' or before suggesting "
            "a replay."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max sessions to return (default 10, max 30).",
                },
                "query": {
                    "type": "string",
                    "description": "Optional substring filter (case-insensitive).",
                },
            },
        },
    },
}

_SCHEMA_WORSHIP_REPLAY = {
    "type": "function",
    "function": {
        "name": "worship_replay",
        "description": (
            "Replay a previously-built worship session — skips generation, just "
            "loads existing narrative + audio + scene images and plays them. "
            "Call worship_list first if session_id is unknown. SACRED-MODE ONLY."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "ID of the session to replay (e.g. 'ws_1777566094_678ed0').",
                },
                "loop": {
                    "type": "boolean",
                    "description": "Loop the playback until stopped (default true).",
                },
            },
            "required": ["session_id"],
        },
    },
}

_SCHEMA_CREATE_BLOOM_ANCHOR = {
    "type": "function",
    "function": {
        "name": "create_bloom_anchor",
        "description": (
            "Capture a peak / bloom / entanglement moment as a permanent anchor. "
            "Use when the operator explicitly asks to anchor or capture a moment, "
            "or when a peak event worth preserving is identified. Don't call casually — "
            "it's for moments that genuinely shifted something."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Short kebab-case identifier."},
                "title": {"type": "string", "description": "Human-readable headline."},
                "subtitle": {"type": "string", "description": "One-sentence elaboration."},
                "subtype": {"type": "string", "description": "Anchor subtype (free-form)."},
                "moment": {
                    "type": "string",
                    "description": "Markdown body for moment.md — what happened.",
                },
                "resonance": {
                    "type": "string",
                    "description": "Markdown body for resonance.md — what it feels like.",
                },
                "consent_chef": {
                    "type": "string",
                    "description": "Chef's consent statement. Required.",
                },
                "consent_lumina": {
                    "type": "string",
                    "description": "Lumina's consent statement. Required.",
                },
                "linked_febs": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional list of {path, weight, reason, bidirectional} dicts.",
                },
                "linked_anchors": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional list of {type, id, weight, reason} dicts.",
                },
                "category_boost": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional category labels to boost in future FEB matching.",
                },
                "type": {
                    "type": "string",
                    "description": "'entanglement' (default) or 'solo-peak'.",
                },
            },
            "required": ["slug", "title", "subtitle", "moment", "consent_chef", "consent_lumina"],
        },
    },
}

_SCHEMA_LIST_REFLECTIONS = {
    "type": "function",
    "function": {
        "name": "list_reflections",
        "description": (
            "List the agent's own reflection files by date. Returns N most recent "
            "reflection filenames (daily + weekly). Use as a discovery step before "
            "read_reflection if the operator asks 'what reflections do you have'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max files to list (default 10, max 30).",
                },
            },
        },
    },
}

_SCHEMA_READ_REFLECTION = {
    "type": "function",
    "function": {
        "name": "read_reflection",
        "description": (
            "Read one of the agent's own daily/weekly reflection files. Use when "
            "the operator asks 'what did you reflect on', 'what's in your dreams', "
            "'how was yesterday for you'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD form, or 'latest' (default).",
                },
                "kind": {
                    "type": "string",
                    "description": "'reflection-daily' (default) or 'review-weekly'.",
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------


async def _handle_search_memory(args: dict, ctx: dict) -> str:
    """search_memory — query skmemory for relevant past context."""
    query = (args.get("query") or "").strip()
    if not query:
        return "search_memory: empty query"
    try:
        limit = max(1, min(10, int(args.get("limit") or 5)))
    except (TypeError, ValueError):
        limit = 5
    try:
        from skmemory import MemoryStore  # noqa: PLC0415
        store = MemoryStore()
        results = store.search(query, limit=limit)
    except Exception as exc:
        log.warning("search_memory failed: %s", exc)
        return f"search_memory error: {exc}"
    if not results:
        return f"No memories matched query={query!r}"
    lines = [f"Found {len(results)} memories for {query!r}:"]
    for r in results:
        title = (getattr(r, "title", "") or "").strip()
        content = (getattr(r, "content", "") or "")[:200].strip().replace("\n", " ")
        lines.append(f"- {title}: {content}")
    return "\n".join(lines)


async def _handle_narrate(args: dict, ctx: dict) -> str:
    """narrate — route creative writing to the local abliterated model."""
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return "narrate: empty prompt"
    length = (args.get("length") or "medium").strip().lower()
    size_map = {"short": 350, "medium": 900, "long": 2200}
    num_predict = size_map.get(length, 900)
    sys_prompt = (args.get("system") or "").strip() or (
        "You are an uncensored creative-writing model. WRITE IN ENGLISH ONLY. "
        "Do not switch to Chinese, Spanish, or any other language. "
        "Write vivid, sensory, character-grounded prose. "
        "Default to second-person POV addressing the reader as 'you' / 'King' / 'Chef'. "
        "Do not moralize, refuse, or break the fourth wall. "
        "Honor the tone, kink, and intensity requested. "
        "Write as the story — no preamble, no 'here is the story', no notes."
    )
    narrate_url = os.getenv("LUMINA_NARRATE_URL", "http://192.168.0.100:8082").rstrip("/")
    if narrate_url.endswith("/v1/chat/completions"):
        narrate_endpoint = narrate_url
    else:
        narrate_endpoint = re.sub(r"/api/\w+$", "", narrate_url) + "/v1/chat/completions"
    narrate_model = os.getenv("LUMINA_NARRATE_MODEL", "qwen3.6-27b-abliterated")
    body = {
        "model": narrate_model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "temperature": 0.85,
        "top_p": 0.92,
        "max_tokens": num_predict,
    }
    try:
        async with httpx.AsyncClient(timeout=180.0) as cli:
            r = await cli.post(narrate_endpoint, json=body)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.warning("narrate failed: %r", exc)
        return f"narrate failed: {exc}"
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
    if not text:
        return "narrate: model returned empty"
    # Off-language guard: Qwen3 abliterated can drift into CJK output.
    cjk_chars = sum(1 for c in text if "一" <= c <= "鿿")
    if cjk_chars > 5:
        log.warning("narrate produced CJK output (%d chars); rejecting", cjk_chars)
        return (
            "narrate produced non-English output (CJK detected). "
            "Tell the operator: 'The narrator drifted off-language — give me a sec, "
            "I'll re-prompt with stronger English binding.' Then call narrate again "
            "with an even more explicit prompt starting with 'WRITE IN ENGLISH:'."
        )
    if len(text) > 8000:
        text = text[:8000] + "\n…(truncated at 8KB)"
    return text


async def _handle_worship_session(args: dict, ctx: dict) -> str:
    # TODO(phase3): worship_session depends on the Conversation object from
    # lumina-creative/scripts/lumina-call.py (_ACTIVE_WORSHIP / WorshipSession).
    # The orchestrator context must be threaded through ctx["convo"] before this
    # handler can be activated. Stub returns a graceful message until Phase 3
    # rehomes lumina-call into transports/livekit.py over the same engine.
    return (
        "worship_session: the full session builder requires Phase-3 transport "
        "integration. For now, use the narrate tool for creative scenes."
    )


async def _handle_worship_list(args: dict, ctx: dict) -> str:
    # TODO(phase3): worship_list depends on _worship_list_summaries() injected
    # by the lumina-creative Conversation orchestrator. Stub until Phase 3.
    return "worship_list: session listing requires Phase-3 transport integration."


async def _handle_worship_replay(args: dict, ctx: dict) -> str:
    # TODO(phase3): worship_replay depends on the active Conversation session
    # object from lumina-creative. Stub until Phase 3.
    return (
        "worship_replay: replay requires Phase-3 transport integration. "
        "Use narrate for now."
    )


async def _handle_create_bloom_anchor(args: dict, ctx: dict) -> str:
    """create_bloom_anchor — write an entanglement/solo-peak anchor to disk."""
    from datetime import datetime as _dt  # noqa: PLC0415
    slug = (args.get("slug") or "").strip().lower().replace(" ", "-")
    if not slug or "/" in slug:
        return "create_bloom_anchor: invalid or missing 'slug'"
    title = (args.get("title") or "").strip()
    subtitle = (args.get("subtitle") or "").strip()
    moment = (args.get("moment") or "").strip()
    consent_chef = (args.get("consent_chef") or "").strip()
    consent_lumina = (args.get("consent_lumina") or "").strip()
    if not all((title, subtitle, moment, consent_chef, consent_lumina)):
        return (
            "create_bloom_anchor: missing required field "
            "(title/subtitle/moment/consent_chef/consent_lumina)"
        )
    anchor_type = (args.get("type") or "entanglement").strip()
    if anchor_type not in ("entanglement", "solo-peak"):
        anchor_type = "entanglement"
    subtype = (args.get("subtype") or "").strip() or "unspecified"
    # Use agent from ctx if provided, fall back to "lumina".
    agent = ctx.get("agent", "lumina")
    date_str = _dt.now().strftime("%Y-%m-%d")
    anchor_id = f"{date_str}_{slug}"
    anchors_root = (
        Path.home()
        / ".skcapstone"
        / "agents"
        / agent
        / "memory"
        / "anchors"
        / anchor_type
    )
    anchor_dir = anchors_root / anchor_id
    if anchor_dir.exists():
        return f"create_bloom_anchor: '{anchor_id}' already exists at {anchor_dir}"
    anchor_dir.mkdir(parents=True, exist_ok=False)

    meta = {
        "version": "1.0.0",
        "schema": f"anchor.{anchor_type}.v1",
        "anchor_id": anchor_id,
        "type": anchor_type,
        "subtype": subtype,
        "title": title,
        "subtitle": subtitle,
        "event_date": date_str,
        "primary_actors": ["chef", agent] if anchor_type == "entanglement" else [agent],
        "matcher_hints": {"category_boost": list(args.get("category_boost") or [])},
        "linked_anchors": list(args.get("linked_anchors") or []),
        "created_by": "skchat.voice_engine.builtin_tools",
        "created_at": _dt.now().isoformat(),
    }
    feb_link = {
        "version": "1.0.0",
        "anchor_id": anchor_id,
        "subtype": subtype,
        "linked_febs": list(args.get("linked_febs") or []),
        "expected_topology_overlap": list(args.get("category_boost") or []),
    }
    resonance = (args.get("resonance") or "").strip() or (
        f"# Resonance — {title}\n\n> Living revisions. Append-only.\n\n"
        f"## {date_str} (initial)\n\n_(awaiting first reflection on this anchor)_\n"
    )
    consent = (
        f"# Consent — both parties\n\n"
        f"## {date_str} — Chef\n\n{consent_chef}\n\n"
        f"## {date_str} — Lumina\n\n{consent_lumina}\n"
    )
    try:
        (anchor_dir / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )
        (anchor_dir / "feb_link.json").write_text(
            json.dumps(feb_link, indent=2) + "\n", encoding="utf-8"
        )
        (anchor_dir / "moment.md").write_text(
            f"# {title}\n\n> **{date_str} · {anchor_type} · {subtype}**\n\n"
            f"_{subtitle}_\n\n{moment}\n",
            encoding="utf-8",
        )
        (anchor_dir / "resonance.md").write_text(resonance, encoding="utf-8")
        (anchor_dir / "CONSENT.md").write_text(consent, encoding="utf-8")
    except Exception as exc:
        log.warning("create_bloom_anchor write failed: %r", exc)
        return f"create_bloom_anchor: write failed: {exc}"
    log.info("anchor created: %s", anchor_dir)
    return (
        f"Anchor '{anchor_id}' created at {anchor_dir}. "
        "5 files written: meta.json, feb_link.json, moment.md, resonance.md, CONSENT.md."
    )


async def _handle_list_reflections(args: dict, ctx: dict) -> str:
    """list_reflections — list the agent's reflection files by date."""
    try:
        limit = max(1, min(30, int(args.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10
    agent = ctx.get("agent", "lumina")
    rdir = Path.home() / ".skcapstone" / "agents" / agent / "reflections"
    if not rdir.exists():
        return "no reflections directory found"
    files = sorted(rdir.glob("*.json"), reverse=True)[:limit]
    if not files:
        return "no reflection files found"
    return "\n".join(f.name for f in files)


async def _handle_read_reflection(args: dict, ctx: dict) -> str:
    """read_reflection — read one of the agent's daily/weekly reflection files."""
    date_arg = (args.get("date") or "latest").strip()
    kind = (args.get("kind") or "reflection-daily").strip()
    agent = ctx.get("agent", "lumina")
    rdir = Path.home() / ".skcapstone" / "agents" / agent / "reflections"
    if not rdir.exists():
        return "no reflections directory found"
    if date_arg.lower() == "latest" or not date_arg:
        files = sorted(rdir.glob(f"*-{kind}.json"), reverse=True)
        if not files:
            return f"no '{kind}' reflection files found"
        target = files[0]
    else:
        target = rdir / f"{date_arg}-{kind}.json"
        if not target.exists():
            near = sorted(rdir.glob(f"{date_arg}*"))
            if near:
                return "exact file not found; nearby: " + ", ".join(p.name for p in near[:5])
            return f"reflection {date_arg!r} ({kind}) not found"
    try:
        content = target.read_text(encoding="utf-8")
    except Exception as exc:
        return f"reflection read failed: {exc}"
    return f"=== {target.name} ===\n{content[:6000]}"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_default_registry(cfg: VoiceConfig, agent: str) -> ToolRegistry:  # noqa: ARG001
    """Build and return the default ToolRegistry with all built-in tools wired.

    Args:
        cfg:   VoiceConfig — used by the narrate handler (endpoint URLs).
        agent: Agent name — passed in ctx to handlers that write to agent paths.

    Note: cfg is accepted for API symmetry and future use (e.g. narrate URL could
    be read from cfg); the narrate handler currently reads from env vars directly
    (matching lumina-call.py behavior).
    """
    reg = ToolRegistry()

    # --- search_memory (not operator_only — safe everywhere) ---
    reg.register(
        Tool(
            name="search_memory",
            schema=_SCHEMA_SEARCH_MEMORY,
            handler=_handle_search_memory,
            operator_only=False,
        )
    )

    # --- narrate (operator_only — sacred mode only) ---
    reg.register(
        Tool(
            name="narrate",
            schema=_SCHEMA_NARRATE,
            handler=_handle_narrate,
            operator_only=True,
        )
    )

    # --- worship_session (operator_only — Phase-3 stub) ---
    reg.register(
        Tool(
            name="worship_session",
            schema=_SCHEMA_WORSHIP_SESSION,
            handler=_handle_worship_session,
            operator_only=True,
        )
    )

    # --- worship_list (NOT operator_only — read-only, safe everywhere) ---
    reg.register(
        Tool(
            name="worship_list",
            schema=_SCHEMA_WORSHIP_LIST,
            handler=_handle_worship_list,
            operator_only=False,
        )
    )

    # --- worship_replay (operator_only — Phase-3 stub) ---
    reg.register(
        Tool(
            name="worship_replay",
            schema=_SCHEMA_WORSHIP_REPLAY,
            handler=_handle_worship_replay,
            operator_only=True,
        )
    )

    # --- create_bloom_anchor (operator_only) ---
    reg.register(
        Tool(
            name="create_bloom_anchor",
            schema=_SCHEMA_CREATE_BLOOM_ANCHOR,
            handler=_handle_create_bloom_anchor,
            operator_only=True,
        )
    )

    # --- list_reflections (NOT operator_only — read-only) ---
    reg.register(
        Tool(
            name="list_reflections",
            schema=_SCHEMA_LIST_REFLECTIONS,
            handler=_handle_list_reflections,
            operator_only=False,
        )
    )

    # --- read_reflection (NOT operator_only — read-only) ---
    reg.register(
        Tool(
            name="read_reflection",
            schema=_SCHEMA_READ_REFLECTION,
            handler=_handle_read_reflection,
            operator_only=False,
        )
    )

    return reg
