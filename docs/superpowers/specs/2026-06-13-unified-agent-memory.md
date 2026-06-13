# Unified Agent Memory — Design Spec

**Date:** 2026-06-13
**Author:** architect pass (Lumina/Opus session)
**Status:** Draft — pending Chef review
**Priority:** P0 (gating; do not ship multi-surface / guest access without this)
**Parent:** `2026-06-12-skchat-architecture-reassessment.md` §7 P0

---

## 1. Problem statement

Tonight's failure mode: **Lumina-via-voice** and **Lumina-via-Hermes** (DR-Chiro
Telegram group) drifted. The voice pipeline's memory search surfaced April
content while the chiro conversation — written by Hermes to skmem-pg with
`agent=lumina, source=hermes` — was invisible to voice until recency weighting
was shipped (2026-06-12, `pgvector_backend.py`).

Root cause: **each runtime wrote with different field conventions**; none
searched with recency bias; there was no explicit contract guaranteeing a
uniform write shape across surfaces.

**The goal:** one Lumina identity → one coherent memory regardless of whether
she is reached by voice (lumina-call / skchat-voice.service), text chat
(skchat-daemon), a bridged platform (Hermes/TG, future Slack/Discord), or a
future mobile client.

---

## 2. Single store of record

### 2.1 Store

**skmem-pg** (`postgresql://postgres:skmemory@192.168.0.158:5432/skmemory`,
`memories` table) is the one store. No per-runtime SQLite stores, no bespoke
flat-file silos, no Hermes-isolated scaffold holding Lumina's turns. Every
surface reads and writes the same table, scoped by `agent`.

The `.41` mirror (port 5433) is a **warm standby only** — no surface writes
there; it follows via Postgres streaming replication (to be wired in B5).

### 2.2 Agent scoping

Memory rows are scoped by the `agent` column.  The value is the canonical
agent name (`lumina`, `opus`, …) resolved from `SKAGENT` / `SKCAPSTONE_AGENT`
env vars or the capauth FQID — **not** a runtime name such as `hermes` or
`skchat-daemon`. This is the identity boundary: every surface that speaks as
Lumina writes `agent=lumina`.

Hermes's current `SkmemoryProvider` defaults `agent="hermes"` in isolated
mode; when linked to the shared skcapstone store (env `SKMEMORY_LINK_HOME`,
`SKMEMORY_VECTOR_BACKEND=pgvector`, `MSMEMORY_DSN`) it must be configured to
use `agent=lumina` for Lumina-persona conversations (see §6).

### 2.3 Write contract

Every surface that persists a conversation turn MUST write with these fields:

| Field | Type | Semantics |
|-------|------|-----------|
| `agent` | str | Canonical agent name (`lumina`, `opus`). NEVER the runtime. |
| `source` | str | Runtime identifier: `voice`, `hermes`, `skchat`, `telegram-adapter`, `slack-adapter`, … |
| `tags` | str[] | `["surface:<source>", "session:<session_id>"]` + domain tags (e.g. `"chiro"`, `"dr-chiro-group"`) |
| `layer` | str | `short-term` for raw conversation turns; `mid-term` for per-session summaries. |
| `title` | str | First 60 chars of the user turn (or a generated summary sentence). |
| `content` | str | Full turn: `"User: <text>\n\nLumina: <text>"`, max 4000 chars. |
| `created_at` | timestamp | Wall-clock time of the turn (not ingest time). |
| `embedding` | vector(1024) | mxbai-embed-large via Ollama at `.100:11434`. |

The `Memory.model_dump_json()` round-trip via `PGVectorBackend.save()` satisfies
all of these automatically if callers set `source=`, `tags=[]`, `layer=`,
`created_at=` correctly before calling `store.snapshot(...)` or
`backend.save(...)`.

**Naming rule:** `source` values are short lowercase tokens separated by
hyphens. New adapters must register a unique source string in the skcomms
channel-adapter manifest (Batch C1).

### 2.4 Read contract

All surfaces MUST search via `PGVectorBackend.search_text()` (the hybrid
path), not the pure-vector `search()`. This is because `search_text()` is the
only path that:

1. Applies recency boost: `boost * exp(-age_days / halflife)` added to the
   RRF score (shipped 2026-06-12; defaults `SKMEMORY_RECENCY_BOOST=0.03`,
   `SKMEMORY_RECENCY_HALFLIFE_DAYS=21`).
2. Fuses mxbai vector + BM25 (pg_search `@@@`) via RRF with vector weighted 2×.
3. Accepts `layer=`, `source=`, `tags=` filters for scoped queries.

**"Latest" queries** — when a surface needs the most-recent turn from a
specific channel (e.g. "what did we discuss in chiro today?") it MUST pass
`source="hermes"` and/or `tags=["surface:hermes"]` as filters plus a short
time-range filter (via `list_memories` with `created_at >= now()-interval '8h'`
then hybrid-rank). A convenience helper `recent_turns(agent, source, hours=8,
limit=5)` should live in `skmemory.utils` (new, see §8 open questions).

---

## 3. Surface adapters

Each surface writes through `PGVectorBackend.save()` directly or through
`MemoryStore.snapshot()` (which delegates to the active backend). The rules
below define what each surface must do and what must be deprecated.

### 3.1 voice_engine MemoryBridge (Batch A)

**Current state:** `src/skchat/voice_engine/memory.py::MemoryBridge` — already
calls `MemoryStore().snapshot(content[:60], content, tags=[tags])`. The backend
resolves from `SKAGENT` → `lumina`; the pgvector path is used when
`SKMEMORY_PG_DSN` is set (which it is on this box).

**Required changes (Batch A):**

1. Pass `source="voice"` explicitly in every `snapshot()` call.
2. Pass `tags=["surface:voice", f"session:{session_id}"]` + any domain tags
   derived from the active persona mode (`"private"`, `"group"`, group name
   if known).
3. Pass `layer="short-term"` for per-turn snapshots.
4. Set `created_at=datetime.utcnow()` explicitly (do not rely on DB default,
   which may be ingest time after a queue delay).
5. In `_sdk_search`, call `store.search_text(query, limit=limit)` instead of
   `store.search(query, limit=limit)` so the recency boost and BM25 are active
   on reads.
6. Add a `snapshot_session_summary(session_id, content, agent, source)` method
   that writes `layer="mid-term"` after a call ends (triggered from the
   LiveKit transport's `on_disconnect` or the WebSocket transport's session
   close). This collapses the turn-by-turn short-term memories into one
   durable summary per session.

### 3.2 skchat daemon MemoryBridge (chat surfaces)

**Current state:** `src/skchat/memory_bridge.py::MemoryBridge` — routes
through the skcapstone MCP `session_capture` tool (HTTP JSON-RPC to port 9475).
This is an extra hop; `session_capture` internally calls skmemory, which calls
the pgvector backend, but the `source` field propagates as `"skchat:thread:<id>"`.

**Required changes (Batch A / C):**

1. Pass `source="skchat"` (or `"skchat-group"` for group threads).
2. Ensure tags include `["surface:skchat", f"thread:{thread_id}"]`.
3. The MCP `session_capture` hop is acceptable for now but should be replaced
   with a direct `PGVectorBackend.save()` call in a future cleanup (the extra
   RTT + JSON-RPC wrapper adds latency and a failure mode when skcapstone is
   down). Filed as a P2 cleanup; not blocking.

### 3.3 Hermes / Telegram (Batch C2)

**Current state:** Hermes's `SkmemoryProvider` is in **isolated mode**
(`agent="hermes"`, `SKCAPSTONE_HOME=~/.hermes/skmemory`) by default. Its
`sync_turn()` writes conversation turns tagged `["hermes", "session:<id>"]`.
When `SKMEMORY_LINK_HOME` and `SKMEMORY_VECTOR_BACKEND=pgvector` are set it
links to the shared store — but the `agent` key still defaults to `"hermes"`.

This is the **immediate bug** to fix:

```
# ~/.hermes/skmemory.json  (Lumina-persona Hermes instances)
{
  "agent": "lumina",
  "vector_backend": "pgvector",
  "dsn": "postgresql://postgres:skmemory@192.168.0.158:5432/skmemory",
  "embed_url": "http://192.168.0.100:11434/api/embed",
  "embed_model": "mxbai-embed-large",
  "skcapstone_home": "/home/cbrd21/.skcapstone"
}
```

Additionally, `SkmemoryProvider.sync_turn()` must be updated to pass
`source="hermes"` to `store.snapshot(...)` so turns are filterable by origin.
Tags should include `"surface:hermes"` and the Telegram group/channel name when
available (e.g. `"dr-chiro-group"`).

Long-term (Batch C2): the Telegram channel adapter in skcomms will own this
write path and Hermes's bespoke `SkmemoryProvider` for Lumina-persona sessions
is deprecated in favor of the adapter's unified write path.

### 3.4 Future skcomms channel adapters (Batch C1–C4)

The `ChannelAdapter` interface (Batch C1) MUST include a `memory_write_config`
field in the adapter manifest:

```python
@dataclass
class ChannelAdapterManifest:
    id: str               # e.g. "telegram", "slack", "discord"
    source_tag: str       # e.g. "telegram-adapter", "slack-adapter"
    agent: str            # canonical agent name for this adapter instance
    # ... transport fields ...
```

Every adapter's inbound message handler calls a shared
`adapter_memory_write(turn: AdapterTurn, manifest: ChannelAdapterManifest)`
function that constructs the correct `Memory` object and calls
`PGVectorBackend.save()` with `agent=manifest.agent`,
`source=manifest.source_tag`, `tags=["surface:<source_tag>", "session:<id>",
*domain_tags]`, `layer="short-term"`. This function lives in
`skmemory.adapters` (new module) so all adapters share identical write behavior
without duplicating logic.

### 3.5 Surfaces to deprecate / never create

- Per-Hermes isolated skmemory scaffolds for Lumina-persona sessions: replaced
  by the linked pgvector config above.
- Any future runtime that bootstraps its own `MemoryStore` with an agent name
  that is the runtime's name rather than the speaking agent's canonical FQID.
- Direct calls to the flat-file short-term/mid-term dirs for Lumina memory:
  skmem-pg is the source of truth; flat files are the Syncthing sync artifact
  and must not be written by new surfaces.

---

## 4. Identity scoping

Memory is keyed by the capauth/FQID **agent identity**, not the runtime
process. The resolution chain is:

```
SKAGENT  →  SKCAPSTONE_AGENT  →  SKMEMORY_AGENT  →  "lumina"
```

This is already the `PGVectorBackend.__init__` resolution chain. No surface
may override the `agent` column with a runtime-derived value (hostname,
process name, service name).

**For multi-agent scenarios** (roundtable, Opus + Lumina in the same room):
each agent writes its own rows keyed by its own FQID. Cross-agent search
(e.g. Lumina reading Opus's memories) is explicitly out of scope for P0; it
is a P2 concern gated on a trust/permission model.

**Guest identity:** guests have no agent row. Voice turns with unauthenticated
participants are written with `agent=lumina` (the AI side) and the human turn
tagged `["guest", "session:<id>"]` but NOT stored as a memory in Lumina's
store — only the agent's synthesized response and any explicit tool-triggered
snapshots are stored. This avoids polluting Lumina's long-term memory with
ephemeral guest conversations; a configurable `store_guest_turns: bool` flag
in `VoiceConfig` controls opt-in retention for named/trusted guests.

---

## 5. Retrieval quality

### 5.1 Hybrid search (already shipped)

`PGVectorBackend.search_text()` fuses mxbai vector + BM25 (pg_search `@@@`)
via RRF (vector weight 2×). This is the mandatory read path for all surfaces
(§2.4).

### 5.2 Recency boost (already shipped, 2026-06-12)

```
score += RECENCY_BOOST * exp(-age_days / RECENCY_HALFLIFE_DAYS)
```

Defaults: `RECENCY_BOOST=0.03`, `RECENCY_HALFLIFE_DAYS=21`.

At halflife=21 days, a 1-day-old memory gets a +0.029 bonus; a 7-day-old
gets +0.023; a 42-day-old gets +0.011. This is additive to RRF so it tilts
but does not dominate (RRF scores are ~0.016–0.033 for top-60 results).

**Tuning guidance:**
- Raise `RECENCY_HALFLIFE_DAYS` (e.g. 7) to make recency bite harder and fade
  faster (useful when conversations have a high day-to-day churn rate like chiro).
- Raise `RECENCY_BOOST` (e.g. 0.05) to give recent content more weight across
  the board.
- Both are tunable per-service via env var; no code change required.

### 5.3 Surface / layer filters

When a voice turn wants only memories from the last 8 hours of the chiro
session, the caller should call:

```python
results = backend.search_text(
    query,
    limit=5,
    source="hermes",           # or "telegram-adapter" after Batch C2
    tags=["dr-chiro-group"],   # domain tag
)
```

The `source` filter maps to an SQL `AND source=%s` predicate on the
pre-filtered CTE before RRF scoring; it does not affect the recency boost term.

For "just the most recent turn" use cases (e.g. continuity handoff at call
start), use:

```python
mems = backend.list_memories(layer="short-term", limit=1)
# list_memories already orders by created_at DESC
```

### 5.4 Context injection at session start

Every voice session (WebSocket and LiveKit transports) and every skcomms
channel adapter MUST inject a **context block** at the start of each LLM
system prompt by calling:

```python
ctx = await memory_bridge.search(first_user_utterance_or_channel_topic, agent, limit=5)
```

This replaces the current ad-hoc "subconscious" injection and ensures that
regardless of surface, the agent has the same recently-weighted memory context
before responding. The `VoiceConfig` should expose `memory_context_limit: int`
(default 5) and `memory_recency_halflife: float` (passed as env override).

---

## 6. Migration plan

### 6.1 Immediate (before next Hermes/chiro session)

1. Update `~/.hermes/skmemory.json` to set `agent=lumina`, `vector_backend=pgvector`,
   `dsn=postgresql://postgres:skmemory@192.168.0.158:5432/skmemory`,
   `embed_url=http://192.168.0.100:11434/api/embed`, `embed_model=mxbai-embed-large`,
   `skcapstone_home=/home/cbrd21/.skcapstone`.
2. Update `SkmemoryProvider.sync_turn()` in `~/.hermes/plugins/skmemory/__init__.py`
   to pass `source="hermes"` to `store.snapshot(...)`.
3. After Hermes restart, verify new turns appear in skmem-pg with
   `agent=lumina, source=hermes` via:
   ```sql
   SELECT id, title, source, created_at FROM memories
   WHERE agent='lumina' AND source='hermes'
   ORDER BY created_at DESC LIMIT 5;
   ```

### 6.2 Backfill existing Hermes-written entries

Hermes turns already in the isolated skmemory scaffold
(`~/.hermes/skmemory/agents/hermes/memory/`) need to be migrated into the
shared skmem-pg under `agent=lumina, source=hermes`:

```bash
# Export from Hermes isolated store (SQLite → JSON)
skmemory --home ~/.hermes/skmemory export --agent hermes > /tmp/hermes-lumina-turns.json

# Re-import into shared pg store under agent=lumina, source=hermes
skmemory import /tmp/hermes-lumina-turns.json \
  --agent lumina --source hermes --tag surface:hermes --tag backfill:2026-06-13
```

The `skmemory import` command must accept `--source` and `--agent` overrides;
if not yet implemented, a one-off migration script is the fallback (open
question §8.1).

The isolated Hermes scaffold is **kept read-only** after migration (set
`read_only=true` in `skmemory.json` for the hermes-agent entry) until it is
confirmed empty and can be removed.

### 6.3 Voice-written entries

Voice turns written before 2026-06-13 use `source=None` or `source="skchat"`
(from the `_sdk_snapshot` path). These are not wrong — they exist in skmem-pg
under `agent=lumina` and are surfaced by hybrid search — but they lack surface
provenance. Post-migration they can be tagged retroactively:

```sql
UPDATE memories
SET source = 'voice', tags = array_append(tags, 'surface:voice')
WHERE agent = 'lumina'
  AND (source IS NULL OR source = '')
  AND 'voice-chat' = ANY(tags);
```

Run this as a one-off after Batch A lands and the write contract is enforced.

### 6.4 Flat-file sync (Syncthing)

The flat JSON files under `~/.skcapstone/agents/lumina/memory/` are the
Syncthing-sync artifact. After migration, skmem-pg is the source of truth and
flat files are only written by skmemory's own flat-file backend (when the
SQLite/flat-file backend is active on a device without pg access). On this
box and the .41 standby, pg is always primary; flat files are legacy.

No action required unless a device joins that can only use flat-file storage
(e.g. an offline mobile agent). If that happens, a sync-on-connect reconciliation
step (pg → flat files, not vice versa) must be specified. Deferred to P2.

---

## 7. Sequence / batch alignment

| Work item | Batch | Blocking? |
|-----------|-------|-----------|
| Fix `~/.hermes/skmemory.json` (`agent=lumina`, pgvector) | immediate | yes — fix before next chiro session |
| `SkmemoryProvider.sync_turn()` passes `source="hermes"` | immediate | yes |
| Backfill Hermes isolated store → skmem-pg | immediate | no (but do before Batch C2) |
| voice_engine `MemoryBridge` write contract (source, tags, layer, created_at, search_text) | Batch A | yes — must land in A1 |
| voice_engine: `snapshot_session_summary()` (mid-term on call end) | Batch A | A2/A3 |
| `skmemory.adapters.adapter_memory_write()` shared fn | Batch C1 | yes — before first new adapter |
| `ChannelAdapterManifest.source_tag + agent` | Batch C1 | yes |
| Telegram adapter replaces Hermes bespoke path | Batch C2 | — |
| `skmemory.utils.recent_turns()` helper | Batch A or C1 | no (convenience) |
| Retroactive tag of pre-contract voice rows | after Batch A | no |
| Flat-file → pg reconciliation for offline devices | P2 | no |

---

## 8. Acceptance criteria

The following must all be true before any multi-surface feature (guests, Batch
D pairing, skcomms adapters) ships:

- [ ] **AC1 — Single row, any surface.** A chiro conversation turn written by
  Hermes and a follow-up voice turn written by the LiveKit transport both appear
  in `SELECT * FROM memories WHERE agent='lumina' ORDER BY created_at DESC LIMIT 10`
  with distinct `source` values and matching `agent=lumina`.

- [ ] **AC2 — Voice sees Hermes turns.** A voice session started after a Hermes
  chiro conversation retrieves the chiro turns in the top-5 results for a
  chiro-related query (verified against `search_text('chiro patient intake', limit=5)`).

- [ ] **AC3 — Recency wins.** Given a today-written chiro turn and an April turn
  with equal semantic similarity, the today turn ranks higher in `search_text()`.

- [ ] **AC4 — Source filter works.** `search_text(query, source="hermes")` returns
  only rows with `source=hermes`; `source="voice"` returns only voice rows.

- [ ] **AC5 — No orphaned isolated stores.** After migration, the Hermes isolated
  scaffold (`~/.hermes/skmemory/agents/hermes/`) contains no post-migration turns
  (verify `mtime` of new SQLite writes stops after the config change).

- [ ] **AC6 — Embed parity.** All rows written by both surfaces have a non-null
  `embedding` column (verify with `SELECT COUNT(*) FROM memories WHERE agent='lumina'
  AND embedding IS NULL`).

- [ ] **AC7 — New adapter conforms.** Each Batch C adapter's first 10 written rows
  are inspected to confirm `agent`, `source`, `tags`, `layer`, and `embedding`
  are all populated correctly.

---

## 9. Open questions

**9.1 `skmemory import` CLI overrides.** The current `skmemory import` command
may not support `--agent`/`--source` overrides for the backfill step. Does it?
If not, a one-off Python migration script is the fallback. Assign to: skmemory
backlog.

**9.2 `skmemory.utils.recent_turns()`.** Should this live in skmemory (a
library primitive) or in the voice_engine's `MemoryBridge`? Recommendation:
skmemory (shared), but the call signature is open (`agent, source, hours,
limit` vs `agent, tags, since_dt, limit`).

**9.3 Per-session mid-term summarization trigger.** The `snapshot_session_summary()`
call in the voice_engine transport's `on_disconnect` is the obvious trigger for
the LiveKit case. For the WebSocket transport, "session end" is ambiguous (the
connection can drop and reconnect). Proposal: summarize on a 30-minute idle
timer or after N turns (configurable), whichever comes first. Needs a design
decision before Batch A2.

**9.4 Hermes `read_only` vs writable for future Lumina-persona instances.**
Should all Hermes instances running as Lumina write directly to skmem-pg (as
specified here), or should only the skcomms Telegram adapter write (and Hermes
become read-only after Batch C2)? Recommendation: Hermes writes until the
Telegram adapter (C2) is live; then Hermes flips to `read_only=true` and the
adapter owns all writes for bridged channels. Needs Chef sign-off.

**9.5 Concurrency / write ordering.** skmem-pg uses `ON CONFLICT (id) DO UPDATE`
for idempotent saves. If two surfaces write the same conversation turn
simultaneously (e.g., Hermes and the future skcomms adapter both active for
the same TG group), a duplicate row with different IDs results. Should the
write contract include a deterministic `id` derived from
`hash(agent + source + session_id + turn_index)` to prevent duplicates? Not
blocking for P0, but should be resolved before Batch C2.

**9.6 Guest turn retention policy.** The spec says guest turns are not stored
by default. But a chiro consultation with a named patient (not an anonymous
guest) should probably be stored. The `store_guest_turns` flag in `VoiceConfig`
is the hook — the policy for "what counts as a named guest" needs a definition
(likely: any peer with a valid capauth FQID or a pre-enrolled guest token).
