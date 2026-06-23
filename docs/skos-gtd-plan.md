The Flutter coord/skos dirs aren't at that exact path on this checkout, but the inventory documents them. I have enough confirmed grounding (coord task model is derived-status/immutable, dashboard.py has GET-only `do_GET` routing with `_serve_json`, GTD tools use flat JSON with the degenerate reference alias, skos-ingest plan defines the InboxItem envelope + triage record + reversibility gate). I'll write the plan now.

The plan is the deliverable. Here it is.

---

# skos GTD — One Inbox, One Board, One Lineage

**Architecture + Epic/Sprint Plan · Lead-architect doc · v1**
Grounded in: skmem-pg HA · skos-ingest-plan.md (epic 984b8fbb) · gtd_tools.py + coordination.py · skchat-app Flutter · Lumina living-mind bridge · gog 4-C

---

## 0. Thesis

> **One inbox, one item model, one board — and you drive each item by talking to it.**

Today there are **three GTD silos that share only the 4-C vocabulary by convention**: (1) the flat JSON lists in `coordination/gtd/*.json`, (2) Gmail labels across 5 accounts, (3) the Nextcloud/`~/clawd/gtd/` file tree. None reconciles with the others, and none reconciles with the **coord board** or the **designed-but-unbuilt skos-ingest** capture pipeline. The "ultimate flex" is to collapse all of that into **one coherent lineage**:

```
   CAPTURE  →  TRIAGE/CLARIFY  →  PLACE  →  ENGAGE
   (rails)     (Lumina+code)      (skmem-pg/blob)   (board + per-item chat)
   └────────────────── one `items` envelope in skmem-pg ──────────────────┘
```

Four design rules, taken straight from the research and our own scar tissue:

1. **Overlay, don't migrate.** Gmail (4-C labels) and the Nextcloud file tree stay authoritative *for content*. The unified store is a **projection + write-back router**, not a new silo that fights them. (Akiflow/Sunsama model.)
2. **One envelope, infinite sources.** A CloudEvents-shaped `items` table in skmem-pg with a typed header + JSONB `payload` + verbatim `raw`. New sources are **drop-in adapters**, never schema migrations.
3. **The task is not the conversation.** Item lifecycle/state lives in durable structured storage; the chat thread is the *interface*, not the source of truth for status. State changes are tool calls, never free-text.
4. **The model proposes, code disposes.** Lumina emits a strict routing record; deterministic code executes, gated by reversibility × confidence. Irreversible/external actions always confirm. (Reuses the skos-ingest T0-T3 gate verbatim.)

**This unifies skos-ingest and GTD into ONE epic.** The skos-ingest `InboxItem` (`skos.inbox/1`) *is* the capture stage of this item model; the coord `Task` becomes `kind=io.skworld.task`; a GTD next-action becomes `kind=io.skworld.gtd`. Same table, same lineage, same board.

---

## 1. The Unified Item Model (skmem-pg)

### 1.1 The envelope: `skitem/1` (CloudEvents-shaped, future-proof)

One table beside the existing `docs`/`memories` in skmem-pg (`pg17-bm25-age` image). Typed common header columns for what every source shares; JSONB `payload` for type-specific body; verbatim `raw` JSONB as the escape hatch (the #1 unified-API lesson — never lose the source shape).

```sql
CREATE TABLE items (
  id            text PRIMARY KEY,            -- ULID; also inbox file stem
  specversion   text NOT NULL DEFAULT 'skitem/1',
  kind          text NOT NULL,              -- polymorphic discriminator, reverse-DNS:
                                            --  io.skworld.gtd | .task | .email | .calendar
                                            --  | .scan | .message | .reference | .project | .area
  source        text NOT NULL,             -- adapter that produced it:
                                            --  share|telegram|email|gcal|watch|voice|coord|chat|api
  source_account text,                      -- e.g. cbd2dot11@gmail.com (cross-account key)
  source_id     text,                       -- gmail message-id / ics UID / coord task id / file path
  source_url    text,
  subject       text,                        -- human title
  agent         text,                        -- SKAGENT scope (captured_by)
  node          text,                        -- host that captured it (.158/.41/...)
  state         text NOT NULL DEFAULT 'inbox',   -- DERIVED, see §1.3
  fourc_cadence  text, fourc_capability text, -- 4-C parity (Capture/Clarify/Organize/Engage
  fourc_context  text, fourc_connection text, --  axes; mirrors Gmail Projects/Areas/Accounts)
  project_id    text,                        -- FK to an items row with kind=project
  scheduled_for timestamptz,                 -- aggregator-owned
  due           timestamptz,
  energy        text,  time_estimate_min int, priority text,  -- engage axes (time+energy+priority)
  time_block_event_id text,                  -- gog calendar event id when time-blocked
  thread_id     text,                        -- 1:1 per-item chat thread (§3)
  autonomy      text NOT NULL DEFAULT 'suggest_confirm', -- per-item Autonomy Dial
  sha256        text,                        -- content address (attachment dedup → blob_catalog)
  dedup_key     text,                        -- normalized(title)+project+due hash (cross-source dedup)
  payload       jsonb NOT NULL DEFAULT '{}', -- normalized type-specific body
  raw           jsonb,                       -- verbatim source payload (escape hatch)
  embedding     vector(1024),               -- mxbai, matches docs/memories
  tsv           tsvector,
  created_at    timestamptz NOT NULL DEFAULT now(),
  occurred_at   timestamptz,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz
);
CREATE UNIQUE INDEX items_idem ON items (source, source_account, source_id);  -- idempotent upsert
CREATE INDEX items_dedup ON items (dedup_key);
CREATE INDEX items_payload_gin ON items USING gin (payload);
CREATE INDEX items_emb_hnsw ON items USING hnsw (embedding vector_cosine_ops);
-- + items_bm25 (pg_search) for hybrid_search parity with docs/memories
```

Adding a new source type adds **no required column** (CloudEvents property). `source+source_account+source_id` is the global uniqueness/idempotency key. Promote any hot JSONB key (e.g. `priority`, `due`) to a Pg18 **generated column** later with zero data migration.

### 1.2 Typed views (store once, query as perspectives)

Next/Waiting/Someday/contexts are **views**, never duplicated rows (Todoist filters / OmniFocus perspectives). This is also the bridge that lets the existing `gtd_*` MCP verbs and `coord_*` read from one store unchanged:

```sql
CREATE VIEW v_gtd_next   AS SELECT * FROM items WHERE kind='io.skworld.gtd' AND state='next';
CREATE VIEW v_gtd_waiting AS SELECT * FROM items WHERE state='waiting';
CREATE VIEW v_today      AS SELECT * FROM items
   WHERE state IN ('next','scheduled') AND (scheduled_for::date = current_date OR due::date <= current_date);
CREATE VIEW tasks        AS SELECT id, subject AS title, agent AS created_by, state,
   payload->>'priority' AS priority, payload->'acceptance_criteria' AS acceptance_criteria,
   payload->'dependencies' AS dependencies FROM items WHERE kind='io.skworld.task';
CREATE VIEW reference_docs AS SELECT * FROM items WHERE kind='io.skworld.reference';
```

### 1.3 State is derived, not stored mutably (preserves the coord property + fixes Syncthing race)

The coord board already learned this: **status is derived from append-only claim files, the task body is immutable.** We carry that forward and make GTD clarify-steps and coord claims *the same mechanism*:

```sql
CREATE TABLE item_events (             -- append-only; the audit/provenance log
  item_id text, seq bigint, event_type text,  -- captured|clarified|claimed|moved|filed|completed|...
  actor text, at timestamptz DEFAULT now(), data jsonb,
  PRIMARY KEY (item_id, seq)
);
```

`items.state` is a trigger-maintained projection of `item_events` (or a materialized rollup). This gives a free audit trail (satisfies provenance), makes every transition timestamped + queryable, and **eliminates the Syncthing write-contention bug** (live `.sync-conflict-*` files today) because the mutable index lives in one authoritative Postgres, not flat JSON raced across machines.

### 1.4 Relationship to InboxItem and coord Task (same lineage, not parallel)

| Today | Under `skitem/1` |
|---|---|
| skos-ingest `InboxItem` (`skos.inbox/1`) | An `items` row at `state='inbox'`, `kind` TBD until triage; `raw`=artifact metadata. The inbox stage **is** the item's birth. |
| coord `Task` (immutable JSON + derived status) | `kind=io.skworld.task`; `acceptance_criteria/dependencies/notes` → `payload`; claims → `item_events`. |
| GTD item (`gtd_tools._make_item`) | `kind=io.skworld.gtd`; the thin `{text,context,priority,energy}` schema becomes header columns; `reference` stops being a degenerate someday-alias and becomes `kind=io.skworld.reference` routed through skingest. |
| Gmail "1 Action" email | `kind=io.skworld.email`, `source=email`, `source_account=<acct>`, `source_id=<message-id>`; `raw`=full MIME headers. |
| .ics invite | `kind=io.skworld.calendar`, `source_id=<VEVENT UID>`. |

### 1.5 Provenance + linking (PROV-O, one edge table + AGE)

```sql
CREATE TABLE item_links (
  src text, dst text, rel text, created_at timestamptz DEFAULT now(),
  PRIMARY KEY (src, dst, rel)            -- rel ∈ wasDerivedFrom|belongsToProject|refersTo
);                                        --        |attachmentOf|blockedBy|partOf|subtaskOf
```

A task derived from an email = `item_links(task_id, email_id, 'wasDerivedFrom')`. Mirror edges into the existing AGE `lumina_knowledge` graph so cross-source graph queries work. Projects/areas are themselves `items` (`kind=io.skworld.project|area`). The dedup collapse (same item arriving as email AND calendar invite) keeps **one** item with multiple `item_links`/source refs.

---

## 2. Cross-Account + Cross-Machine Aggregation

### 2.1 The aggregator daemon (per host, systemd `--user`, like `skwhisper@`)

`skgtd-aggregator@<agent>.service`. One loop per source. **skmem-pg on .158 is the single authoritative store**; the live mutable task index never goes on Syncthing (that caused the 462k-file/30s thrash and has no conflict semantics — reserve Syncthing for append-only artifact/reference files only).

**Gmail rail (per account, ×5)** — incremental cursor, not full polling:
- Store `last_history_id` per account in `items` metadata (or a small `gmail_cursors` table).
- Initial full sync once; then `users.history.list?startHistoryId=X` → only label/message deltas. `gog` drives it (`gog gmail list -q 'label:"1 Action" OR label:"2 Waiting"'` for the initial seed; history deltas thereafter). On HTTP 404 (expired cursor) → full resync fallback.
- `users.watch` + Pub/Sub for push later (P2 polish); **daily renewal cron** (7-day expiry) — start with poll, it's simpler and we already run cron.
- Map Gmail 4-C labels → `fourc_*` columns + `state` (1 Action→next, 2 Waiting→waiting, 3 Read→reference, 4 Someday→someday). Upsert by `(email, account, message-id)`.

**Calendar rail** — `gog calendar events --all --days N` for all 5 accounts → upsert time-bound commitments as `kind=io.skworld.calendar`, `state` next/scheduled. Calendar is **source AND sink** (see §4 write-back).

**Flat-file GTD rail** — import the existing `~/clawd/gtd/{next,waiting,...}` `.md` tree keyed by file path (`source=watch`, `source_id=<path>`). One-directional read into `items`; the file tree stays the human source of truth for that lane.

**Coord rail** — project existing `coordination/tasks/*.json` into `items` (`source=coord`) read-only first, so the board and GTD share one query surface immediately; write-through later.

### 2.2 Write-back (overlay, field-level ownership)

The only safe conflict model is **per-field ownership**, not blanket last-write-wins:

| Field | Owner | Direction |
|---|---|---|
| content / labels / 4-C classification | **Gmail** (source) | Gmail → pg only |
| scheduled_for / today / time_block | **aggregator** (pg) | pg-local |
| completion | **bidirectional**, last-write-wins on `updated_at` | pg ↔ Gmail (remove "1 Action", apply done/archive via gog) |

This preserves the 4-C label system as the email-side source of truth while pg is the unified engage surface. Same for calendar: time-blocking a next-action **writes** a `gog calendar create` event back and stores `time_block_event_id` (Sunsama timeboxing).

### 2.3 Cross-machine sync (HA pair, not Syncthing)

Per the redundancy mantra, **the .158 ↔ .41:5433 pg mirror IS the HA pair**:
- Default: .41 points at .158 over tailnet for live writes (single authoritative DB).
- HA/offline: .41 runs its own `skmem-pg:5433` (already engine-ready, empty) with **Postgres logical replication** OR a small last-write-wins reconciler keyed on `updated_at + (source,source_account,source_id)`. Each host = primary; sync deltas, not full state.
- Syncthing keeps only: raw inbox artifacts, reference `.md`, soul/memory JSON — never the mutable item index.

### 2.4 Dedup + "tweaks" to the existing setup for scale-out

- **Dedup** at clarify time: `dedup_key = hash(normalized(title)+project+due)`. An item arriving as both email + calendar collapses to one item with multiple `item_links`.
- **Idempotent upserts** on `items_idem` so Pub/Sub redelivery / cron retries are no-ops.
- **Email tweak**: introduce a single capture choke-point label (`1 Inbox`) or plus-address (`...+inbox@gmail.com`) per account so real primary mail flows in (today `gtd-triage.sh` only sweeps noise *out*, one-directional). **Chef decision** — label vs plus-address (Q below).
- **Flat-file tweak**: keep the `~/clawd/gtd/` tree but make the aggregator the *index*; the JSON `coordination/gtd/*.json` lists become **views** over `items` (delete the raced flat-JSON mutable index; keep flat artifacts).

---

## 3. The Board UI + Per-Item Chat

### 3.1 IA — 5 surfaces, keeping the coord-board look (clone the existing pattern)

Reuse the `coord_board_screen.dart` / `coord_board_provider.dart` pattern (AsyncNotifier 60s poll + my/team providers + detail sheet → upgraded to a thread). Bottom-nav/rail: **Today · Board · Projects · Inbox · Review**.

1. **TODAY** — daily-engage home; agenda of `v_today` + a "Now" shortlist, filtered by **time + energy + priority** (2027 GTD: location contexts are dead). Delivered as the morning ritual via the existing sk-alert 7:15 brief.
2. **BOARD** — coord-board columns mapped to GTD lanes: **Inbox → Next → Waiting → Scheduled → Done**, Projects as a swimlane toggle.
3. **PROJECTS** — each project page = its items + a project-level chat thread (Linear project pages).
4. **INBOX / TRIAGE** — captured-but-unclarified items; Lumina pre-tags them (GTD Clarify).
5. **REVIEW** — weekly-review saved filters (stale Next, Waiting follow-ups, Someday).

### 3.2 Item detail = ONE chat thread (Height's proven merge)

**Do not split "comments" and "activity" into two tabs** — that defeats the whole feature. The item detail is a single chronological stream:
- **Attributes header** (pinned): state pill · assignee (incl. the agent) · due/scheduled · project · energy/time chips · item id · **Autonomy Dial**.
- **Unified thread**: human messages + system events (status/assignee/due changed) + **agent action rows** (action + one-line rationale + confidence chip + Undo), all as typed rows in one list.
- **Composer = command surface**: slash-commands `/done /assign @lumina /due fri /move waiting /snooze /sub <text>`. Driving the item *is* chatting.
- Desktop/tablet = right-side slide-in preview pane from the board; phone = full-screen route. **Reuse the skchat interaction kit** (`features/chat`) for the stream/composer — do not fork chat code.

### 3.3 Thread-vs-session: **HYBRID** (recommended)

> **One durable thread per item (the record) + a fresh hydrated session per agent run (the compute).**

- Each item owns **exactly one persistent thread** (`thread_id`, 1:1) — human-legible history of every "do X" exchange — PLUS the structured `items`/`item_events` state.
- Each agent **run** is a fresh scoped context hydrated from (a) the item's state JSON, (b) a *compacted* thread summary, (c) the last N raw turns. Reasoning is ephemeral; state is durable.
- This is the LangGraph `thread_id` model exactly. It avoids both failure modes: throwaway sessions (no continuity) and one infinite raw thread (token blow-up, model loses the plot).

Thread storage in skmem-pg as an **append-only message tree** (`parent_id` + version counter) — gives forking ("draft option B" without losing A), non-destructive compaction, replay/audit for free:

```sql
CREATE TABLE item_messages (
  id text PRIMARY KEY, item_id text, parent_id text,
  role text,                         -- user|agent|system
  kind text,                         -- message|system_event|agent_action|agent_question|intent_preview
  content text, rationale text, confidence real, reversible bool, undo_token text,
  tool_calls jsonb, tool_results jsonb,
  ts timestamptz DEFAULT now()
);
```

### 3.4 Item state machine (GTD vocabulary, native to the stack)

```
inbox → next → waiting → scheduled → reference → done
   ↘ someday          ↘ blocked ↗
```

Wire transitions to the existing `gtd_*` MCP verbs (`gtd_clarify/move/next/waiting/done`) so "file as reference" / "schedule it" / "break into subtasks" are **confirmed state transitions logged to `item_events`**, never free-text. The lifecycle-from-chat loop:

```
operator msg → agent classifies intent → proposes {tool_call, state_transition}
   → interrupt for confirm → on approve: execute tool, log item_events transition,
     update items.state, append agent_action row → thread walks the item through its FSM
```

### 3.5 Agent tool contract (HITL, 4 decisions, hash-verified)

Adopt the LangGraph `interrupt() / Command(resume={decisions:[...]})` primitive with the **four standard decisions: approve / edit / reject / respond**. Item-scoped tools each return a *proposed* action through the gate:

```
draft_reply(item)   schedule(item, when)   split_into_subtasks(item)
set_state(item, to) file_as_reference(item) send(item, channel)
```

Risk-tiered routing keyed on (tool, target, reversibility) — **reuses the skos-ingest T0-T3 gate**:
- **T0/T1 reads + internal mutations** (search, set_state, move) → auto-run.
- **T3 external/irreversible** (`gog gmail send`, `gog calendar create`, `telegram_send`, file send, real coord task create) → **always interrupt + confirm**.

Hardening (acute under Unhinged Mode + live gog/telegram tools): **hash the proposed args at interrupt time, verify on resume** (defeats prompt-injection arg mutation between propose and execute); **24h TTL** on pending sensitive actions then re-propose; **teammate-style one-line rationale**, never dumped chain-of-thought (fights approval fatigue). Pending confirmations live in `gtd_confirm_queue` (`coordination/gtd/pending.json` → an `items` row at `state=awaiting_confirm`).

### 3.6 Confirmation UX in skchat

Agent proposal renders inline in the item thread as an **intent-preview action card**: one-line rationale + confidence chip + `[Proceed] [Edit] [Handle Manually]` (the 4th, *respond*, = just type a reply). Every reversible agent row carries **Undo** — the thread literally is the Action Audit log. Async/populate-over-time: agent suggestions appear as pending rows that resolve in place (leverage existing pubsub so the item thread is a live channel, not request-response).

---

## 4. Agentic Engage (Lumina advances items conversationally)

**Reuse the Lumina living-mind bridge** (`scripts/bridge_consciousness.py` + `telegram_bridge.py`) — it already does live skmemory recall/store + an MCP tool-calling loop (`MAX_TOOL_ROUNDS=5`) over gtd/coord/gmail/calendar/nextcloud, honoring each agent's `expose_tools` allow-list. **The one net-new thing is re-scoping its context injection to a board-item id**: inject the item's state JSON + thread summary into the prompt, restrict tools to the item-scoped contract, run the propose→confirm→execute loop bound to that `thread_id`.

Concrete conversational verbs, each mapped to existing infra and gated:
- **"draft reply"** → `draft_reply` proposes text via qwen3.6; T3 `gog gmail send` requires Proceed. Posts draft as a pending row.
- **"schedule it / time-block Thursday 2pm"** → `schedule` → `gog calendar create` (T3 confirm) → store `time_block_event_id`, set `state=scheduled`.
- **"break this down"** → `split_into_subtasks` (T1 auto) → child `items` with `subtaskOf` links.
- **"file it"** → `file_as_reference` → route through `skingest run_pipeline_for_file` → skmem-pg `docs` + AGE + wiki canon, and **wire `record_ingest_location()`** (the documented one-line gap) so every filed reference is locatable across the fleet. Fixes the broken degenerate reference→someday alias.
- **"do this" / assign to Lumina** → make the agent a **first-class assignee** ("drive to done"); the Autonomy Dial (Observe&Suggest / Suggest&Confirm / Act Autonomously, default Suggest&Confirm) governs how far it goes before asking.

**LLM routing**: qwen3.6 @ `.100:8082` (64k ctx, `--parallel 1`, VRAM near-limit) for classification/rationale, with **claude-opus-4-8 fallback via SKGateway `:18780`** (the existing model picker). Triage is **BATCH not per-item** to avoid 5060 load; heavy synthesis can route to opus instead of piling on .100.

---

## 5. Mapping to Our Stack — Reuse / Extend / Build

**REUSE (as-is):**
- `skingest/synth.py` classify ladder (route/_parse_json, auto→local→interface-defer→off) → re-prompt as `gtd_triage` json_schema on qwen3.6. Zero new inference infra.
- `skmem-pg` HA (.158 + .41:5433 mirror), `file_locations` index, `record_ingest_location()` (skcomms `access/knowledge.py`), access-plane `files.py`.
- `gog` CLI (5 accounts authed) + the `gtd-triage.sh` sweep pattern → extend to a *capture* poller.
- Lumina bridge (`bridge_consciousness.py`) — the per-item chat brain.
- skchat-app `features/chat` (interaction kit) + `features/skos` (capauth-signed access-plane client — the GTD board rides this signed plumbing, no new auth).
- Flutter `coord_board_screen.dart` pattern; `skpdf/gtd_filer.py` (provenance sidecar + sensitive-field detection).

**EXTEND:**
- `gtd_tools.py` MCP verbs — keep as the verb layer, back them by `items`/views instead of flat JSON; widen `_VALID_SOURCES`; un-break the `reference` destination.
- `coordination.py` — coord Task becomes `kind=io.skworld.task` projected into `items`.
- `dashboard.py` (port 7778, currently GET-only `do_GET` + `_serve_json`) — add `GET /api/gtd` (lists+items+views), **POST mutation routes** (capture/move/done/triage-confirm), and `/api/items/<id>/thread`.
- skmem-pg schema — add `items`, `item_events`, `item_links`, `item_messages`, `gmail_cursors`.

**BUILD (net-new, thin):**
- `skgtd-aggregator@.service` (the per-host overlay daemon) + source adapters.
- `gtd_triage` / `gtd_confirm` MCP tools + `gtd_confirm_queue` + T0-T3 gate.
- Source-adapter plugin contract (`spec()/discover()/read()`, Airbyte/Singer-style, registered like skingest `DOC_EXTS` via a `skos.adapters` entry-point group).
- Flutter GTD board screen + item-detail-with-chat module (in the app's module system).

**How this UNIFIES coord + GTD-4C + skos-ingest:** one `items` table is the shared backing store; coord/gtd/inbox are all `kind`s of one envelope; the board shows them through views; capture (skos-ingest rails) → triage (synth ladder) → place (skmem-pg/blob) → engage (board + per-item chat) is one pipeline, not four. The skos-ingest epic 984b8fbb S1-S8 are **absorbed as the capture/triage/placement sprints of this epic** rather than run in parallel.

---

## 6. Epic / Sprint Plan (boardable as coord tasks)

**EPIC: skos GTD — Unified Conversational GTD** (`tag: skos-gtd`). Goal: one GTD across all 5 Gmail + all machines, extensible item model, coord-board UI where you open an item and chat to drive it, unifying GTD-4C + coord + skos-ingest into one lineage. Each sprint is independently shippable + reversible.

---

**S1 — Item model + skmem-pg foundation**
- *Goal:* `skitem/1` envelope live; coord+gtd readable through it without behavior change.
- *Components:* skmem-pg migrations (`items`, `item_events`, `item_links`, typed views `v_gtd_next/v_today/tasks`); `skos/items.py` (upsert/idempotency/state-projection); coord adapter (read-only projection of `coordination/tasks/*.json`); back `gtd_tools.py` reads with views.
- *Acceptance:* `coord board` + `gtd next/waiting/projects` return identical results sourced from `items`; idempotent re-import is a no-op; state derived from `item_events`.
- *Deps:* none. *Risk:* dual-read drift during cutover → keep flat JSON as fallback read for one sprint.

**S2 — One Gmail account E2E (the proof)**
- *Goal:* one account's 1 Action/2 Waiting flows in, completion writes back.
- *Components:* `skgtd-aggregator` skeleton (`@.service`); `adapters/gmail.py` (historyId cursor + `gmail_cursors`, 404→full-resync); 4-C label→`fourc_*`/`state` map; write-back (complete → remove label/archive via gog); field-level ownership.
- *Acceptance:* labelling an email "1 Action" creates an `items` row within one cycle; completing in pg removes the label; redelivery = no dup.
- *Deps:* S1. *Risk:* gog rate limits / cursor expiry → daily renewal + fallback already specced.

**S3 — All 5 accounts + calendar + dedup**
- *Goal:* full cross-account aggregation + calendar source/sink + dedup.
- *Components:* per-account loop ×5; `adapters/gcal.py` (read commitments, write time-blocks, store `time_block_event_id`); `dedup_key` + collapse-to-`item_links`; capture choke-point (`1 Inbox` label or plus-address — Chef decision).
- *Acceptance:* an email + calendar invite for the same thing = one item, two source links; time-blocking writes a real gog event.
- *Deps:* S2. *Risk:* false-merge dedup → surface merges at clarify for confirm.

**S4 — Triage/clarify + reversibility gate (absorbs skos-ingest S3/S7)**
- *Goal:* Lumina drains inbox, proposes 4-C classification + next-action, gated execution.
- *Components:* `gtd_triage` MCP (re-prompt of `synth.py` route, json_schema on qwen3.6, BATCH); routing record (classification+fourc_bucket+confidence+proposed_action+placement_hint+idempotency_key); T0-T3 gate; `gtd_confirm`/`gtd_confirm_queue` (`pending.json`→`awaiting_confirm`).
- *Acceptance:* reversible classifications auto-apply; irreversible enqueue confirm; every decision logged to `item_events`; brain-down degrades to interface-defer.
- *Deps:* S1. *Risk:* hallucinated fields → json_schema + semantic validation before any field is load-bearing.

**S5 — Board UI (clone coord screen)**
- *Goal:* Today/Board/Projects/Inbox/Review Flutter surfaces over `items`.
- *Components:* `dashboard.py` `GET /api/gtd` + views; Flutter `features/gtd/{gtd_board_screen.dart,gtd_board_provider.dart}` (clone coord pattern); attributes header; time/energy/priority filters; default saved views = the gtd_* verbs.
- *Acceptance:* board renders all kinds in GTD lanes; tap opens detail; pull-to-refresh; rides existing capauth-signed access plane.
- *Deps:* S1, S4. *Risk:* read-only → mutations land in S6.

**S6 — Per-item chat + agentic engage (the headline)**
- *Goal:* open an item, chat to drive it; Lumina proposes/executes gated tool calls.
- *Components:* `item_messages` tree; `dashboard.py` POST mutation + `/api/items/<id>/thread` routes; item-detail-with-chat module (reuse `features/chat`); slash-commands; intent-preview action cards + Undo + Autonomy Dial; re-scope `bridge_consciousness.py` to a board-item id; hash-verify + TTL on confirms; item-scoped tool contract.
- *Acceptance:* "draft reply / schedule / break down / file" each post a proposal, confirm gates external actions, transition logs to `item_events`, thread walks the FSM; reply E2E sends via gog only after Proceed.
- *Deps:* S5, S4. *Risk:* forking chat code → reuse interaction kit; approval fatigue → gate only T3.

**S7 — Cross-machine HA sync**
- *Goal:* .158 ↔ .41 authoritative-or-replicated; no Syncthing on the mutable index.
- *Components:* .41 points-at-.158 default + logical-replication/LWW reconciler on `:5433`; remove flat-JSON mutable index (keep artifacts on Syncthing); resolve existing `.sync-conflict-*` debt.
- *Acceptance:* edit on .41 while .158 down → reconciles cleanly on rejoin; no new conflict files; HA pair survives single-host loss.
- *Deps:* S1-S3. *Risk:* split-brain → field-level ownership + `updated_at` LWW on completion only.

**S8 — Extensibility / plugin sources + reference placement**
- *Goal:* a new source = a drop-in plugin, no schema change; reference filing fixed fleet-wide.
- *Components:* adapter entry-point group `skos.adapters` (`spec/discover/read`); migrate R1-R5 capture rails + coord + chat as the first adapters; wire `record_ingest_location()` into `skingest run_pipeline_for_file`; one example new adapter (RSS or scanner-folder).
- *Acceptance:* `pip install` a new adapter → its items appear with new `source`, zero migration; filed references land in skmem-pg `docs`+AGE and are fleet-locatable.
- *Deps:* S1, S4. *Risk:* second-pipeline temptation → adapters register into skingest (the sole ingestion home), never fork it.

---

## 7. Open Questions / Decisions for Chef

1. **Thread-vs-session per item — recommend HYBRID** (one durable thread per item + fresh hydrated session per agent run, §3.3). Confirm, or do you want a *visible* "new session" reset button per item (useful when an item's chat gets noisy)?
2. **How aggressively should Lumina auto-act?** Recommend default **Suggest & Confirm**, T0/T1 auto, **all T3 external (gog send / calendar create / telegram / file-send) always confirm** with hash-verify + 24h TTL. Do you want a global "trusted hours / trusted accounts" override where, say, *your own* calendar writes go T1-auto?
3. **Email capture choke-point: `1 Inbox` label vs plus-address (`...+inbox@`)?** (skos-ingest Q6.) Label = works for forwards + native triage; plus-address = zero-label, but exposes the address. Recommend `1 Inbox` label, reusing the 4-C system.
4. **Cross-machine: live-point-at-.158 vs full logical replication on .41:5433?** Recommend point-at-.158 default + replication only for the genuine off-tailnet case. Confirm .41 should be a true hot standby (redundancy mantra) vs a thin client.
5. **Attachment/blob tier** (skos-ingest Q1): Phase-1 Nextcloud `r/` (ships now) vs Phase-2 Garage S3 (recommended target). Plus `copies_required` default + default landing node (.158).
6. **Auto-merge dedup confidence threshold** — at what confidence does email+calendar auto-collapse vs ask you to confirm the merge?
7. **Coord write-through timing** — keep coord tasks read-only-projected into `items` indefinitely, or cut coord over to write *through* `items` (single store) after S6? Recommend read-only until the board is trusted, then cut over.

---

**Files referenced (absolute):**
`/home/cbrd21/clawd/skcapstone-repos/skchat/docs/skos-ingest-plan.md` · `/home/cbrd21/clawd/skcapstone-repos/skcapstone/src/skcapstone/mcp_tools/gtd_tools.py` · `/home/cbrd21/clawd/skcapstone-repos/skcapstone/src/skcapstone/coordination.py` · `/home/cbrd21/clawd/skcapstone-repos/skcapstone/src/skcapstone/dashboard.py` · `/home/cbrd21/clawd/skcapstone-repos/skchat/skchat-app/lib/features/{coord,chat,skos}/` · `/home/cbrd21/clawd/skcapstone-repos/skchat/scripts/{bridge_consciousness.py,telegram_bridge.py}` · `/home/cbrd21/clawd/wiki/tools/gtd-4c-system.md` · `/home/cbrd21/clawd/scripts/gtd-triage.sh` · skingest `synth.py` · skcomms `access/knowledge.py` (`record_ingest_location`).