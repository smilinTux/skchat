All facts confirmed. The `file_locations` table has exactly `{id, node, path, doc_id, mtime, sha}` with `UNIQUE(node,path)` — so the placement-policy columns (`sha256` content-address, `placement_class`, `copies_required`, `last_access`) are genuinely a net-new extension, not duplicating existing schema. The L1-L5 pipeline and QC gate are exactly as the inventory described. I have everything I need to write the plan.

---

# skos Ingest — Universal Capture → Triage → Federated Placement

**Architecture + Epic/Sprint Plan**
Lead architect doc · v1 · grounded in skingest / GTD / skmem-pg / access-plane / Lumina

---

## 0. Thesis

> **Many doors, one room.** Every capture surface (phone scan, share-sheet, email, in-app, voice) drops into **one canonical skos Inbox**. An **agent (Lumina) drains the inbox** on a schedule, **clarifies** each item (the GTD process step), and **routes** it to the right sink. A **single placement-policy engine** decides *where the bytes physically live* across the fleet, recording every blob in the **access-plane `file_locations` index** so the fleet has one logical namespace over .158/.41/.100/Garage/Nextcloud/Syncthing.

Three hard rules from the research, baked into the design:

1. **Capture is append-only, zero-decision, sub-second.** No classification at capture time. (GTD #1 failure = friction at capture.)
2. **The model proposes, code disposes.** Lumina/qwen3.6 emits a strict JSON routing record; deterministic code executes side-effects gated by reversibility + confidence. Never one freeform "decide-and-act" turn.
3. **Address says WHAT, never WHERE.** Content-hash addressing; placement is a *separate* declarative policy layer over the hash keyspace. (git-annex / IPFS / Nextcloud-primary-storage pattern.)

We build **almost nothing new at the content layer** — skingest already does extract→OCR→embed→skmem-pg→AGE→canon with an idempotent 5-layer pipeline and SHA256 dedup. The net-new work is: (a) the **canonical inbox + capture rails**, (b) the **agentic triage loop** (the synth.py pattern, re-prompted), and (c) the **placement-policy engine** + wiring it into `file_locations`.

---

## 1. Capture Surfaces + The Canonical Inbox

### 1.1 The one logical inbox (where it physically lives)

The canonical inbox is a **flat-file drop-spot** — Obsidian/Paperless pattern, matching the existing flat-file-memory + Syncthing source-of-truth convention:

```
~/.skcapstone/agents/$SKAGENT/inbox/
  unprocessed/
    2026-06-23T14-03-12Z_a1b2c3.pdf          # the raw artifact (verbatim, never mutated)
    2026-06-23T14-03-12Z_a1b2c3.pdf.json      # the InboxItem envelope (sidecar)
  processing/                                 # claimed by a drain run (lease)
  done/                                       # processed, kept N days then GC'd
  failed/                                     # parse/triage errors, surfaced in daily brief
```

- **Source of truth** = the flat files in `unprocessed/` on **.158** (`SKACCESS_NODE`). Synced via Syncthing to .41 (redundancy + the .41 drain can run if .158 is down).
- **One item = one file pair**: the artifact + a `.json` envelope. Mirrors how skingest does sidecars and how memory is stored.
- This dir IS the inbox. Everything in §1.2 is *just a door into it*.

### 1.2 The four+1 capture rails (priority order)

Per the redundancy mantra: **two independent rails must reach the phone use-case** so no single broken rail loses a capture.

| # | Rail | Surface | How it writes to the inbox | Reuses |
|---|------|---------|---------------------------|--------|
| **R1** | **Share-sheet** (primary mobile) | `skchat-app` Flutter — add `share_handler`/`receive_sharing_intent` + Android `ACTION_SEND`/`<share-targets>`, iOS Share Extension + App Group | App POSTs artifact+envelope to the **skos Inbox endpoint** over tailnet (capauth-signed) | skchat-app (unified client, 23/23 done) |
| **R2** | **Telegram** (already-live phone rail) | Forward/DM/photo/voice to `@seaBird_Lumi_bot` | New capture handler in `skchat-telegram-opus` → calls Inbox endpoint | telegram_* tools, worship async-worker pattern |
| **R3** | **Email-to-inbox** (universal, device/person-agnostic) | Plus-addressed Gmail (e.g. `chefboyrdave2.1+inbox@gmail.com`) or a `1 Inbox` label | IMAP-poll via `gog` cron → subject=title, body+attachments=payload → Inbox endpoint; track processed UIDs | gog CLI, 4-C labels, SKAlert cron |
| **R4** | **Watched/synced folder** (Paperless consume dir) | A Syncthing-synced `drop/` folder; phone scanner apps (native iOS/Android) save-share into it | inotify watcher (or `--incremental` cron) → Inbox endpoint | Syncthing, skingest incremental |
| **R5** | **Voice** | skchat `record_voice_message` / `transcribe_audio_file` / piper-TTS | Voice memo lands as artifact; transcribe at **process** time | skchat voice, skwhisper/F5-TTS on .100 |

**The single write API behind all rails** = the **skos Inbox endpoint** (one capauth-signed HTTP POST on the tailnet). This is the choke-point that guarantees one canonical inbox and one place to hang dedup / notify-on-capture (sk-alert) / metrics. Every rail calls it; none writes the dir directly except R4's local watcher (which still calls the endpoint locally for uniformity).

**Mobile scan rule: do not build a scanner.** Use the phone's native scanner (edge-detect + dewarp + multi-page PDF already solved) → share-sheet (R1) or synced drop folder (R4). Agent OCRs server-side at process time.

### 1.3 The InboxItem envelope (the canonical representation)

```jsonc
{
  "id": "2026-06-23T14-03-12Z_a1b2c3",       // ULID-ish; also the file stem
  "schema": "skos.inbox/1",
  "source": "share|telegram|email|watch|voice|api",
  "captured_at": "2026-06-23T14:03:12Z",
  "captured_by": "lumina",                    // SKAGENT scope
  "origin": { "device": "pixel", "from": "...", "msg_id": "...", "list_id": "..." },
  "artifact": {
    "filename": "receipt.pdf",
    "mime": "application/pdf",
    "bytes": 824113,
    "sha256": "…",                            // content address — computed at capture
    "path": "unprocessed/2026-…_a1b2c3.pdf"
  },
  "raw_text": null,                           // populated at PROCESS time (OCR/transcribe), not capture
  "status": "unprocessed",                    // unprocessed→processing→done|failed|awaiting_confirm
  "lease": null,                              // {by, at, ttl} during a drain
  "triage": null,                             // the routing record (§2.3) once triaged
  "placement": null                           // the placement decision (§3) once filed
}
```

- `sha256` is computed at capture (cheap, enables dedup-on-capture: if the hash already exists in `file_locations`, mark `status:dedup` and skip).
- `raw_text`, `triage`, `placement` are **null at capture** — filled by the drain. This enforces the capture/process split.

---

## 2. The Agent Triage Pipeline (Lumina/GTD clarify)

### 2.1 Shape: CLASSIFY (model) → EXECUTE (code), reversibility-gated

The drain is a **batch sweep** (not per-message real-time — avoids 5060 load) running on a schedule + on-demand. For each `unprocessed` item:

```
lease item → deterministic parse → content pipeline (§4) → qwen3.6 CLASSIFY (json_schema)
   → validate (schema + semantic) → autonomy gate (reversibility × confidence)
   → EXECUTE (reversible) OR enqueue gtd_confirm (irreversible) → audit → place (§3) → done
```

The CLASSIFY engine is **`skingest/synth.py`'s `route()/synthesize()/_parse_json` pattern re-prompted** — local qwen3.6-27b-abliterated @ `.100:8082` `/v1` in `response_format: json_schema`, with the existing **auto→local→interface-defer→off fallback ladder** so "brain down" degrades gracefully. Zero new infra.

### 2.2 Deterministic parsers run BEFORE the LLM

The model classifies; it does **not** re-extract what code extracts reliably (research pitfall):

- **.ics** → `icalendar` lib → VEVENT `SUMMARY/DTSTART/DTEND/LOCATION/ORGANIZER/ATTENDEE/RRULE/UID`. **UID is the calendar idempotency key.**
- **email/.eml/.mbox** → stdlib `email`/`mailbox` → From/To/Date/Subject/List-Id/threading + MIME attachments.
- **scans/PDFs/images** → the §4 content pipeline (OCR router) → `raw_text` + structured fields.
- **.vcf** → `vobject`.

These emit markdown sidecars exactly like skingest's existing pandoc/pdftotext sidecars, so they flow through the pipeline with zero pipeline changes (register new extensions in `documents.DOC_EXTS`).

### 2.3 The routing record (the model↔executor contract)

`gtd_triage` — the **missing brain between capture and clarify**. New MCP tool.

**INPUT:** `{item_id | raw_payload, source_type: email|ics|scan|note|task|file|voice}`

**OUTPUT** (strict JSON, json_schema-enforced, then Pydantic-validated + semantic-checked):

```jsonc
{
  "classification": "actionable_next|project|waiting_for|calendar_event|reference|someday_maybe|trash",
  "fourc_bucket": "1-action|2-waiting|3-read|4-someday|projects|areas",  // 4-C label parity
  "confidence": 0.0,                       // bounded float
  "reasons": ["..."],                      // why (audit)
  "requires_human_review": false,          // hard override of autonomy
  "extracted": {
    "title": "...", "dates": [], "people": [], "money": [],
    "deadline": null, "project_hint": null, "doc_type": "receipt|invoice|note|..."
  },
  "proposed_action": { "tool": "gtd_move|gog_calendar_create|skingest_ingest", "args": {…} },
  "placement_hint": { "class": "doc|media|memory", "project": null, "host": null },
  "idempotency_key": "sha256(source_id + action_type + normalized_target)"
}
```

**The Lumina system prompt (strict contract):**
> *"You are a GTD clarifier. You receive ALREADY-PARSED structured fields. Output ONLY the routing JSON. You MAY file to reversible lists yourself. You may NEVER send email, accept an invite, or delete — propose those for confirmation. If unsure, set `requires_human_review=true` and explain in `reasons`. Confidence must reflect ambiguity, not certainty."*

### 2.4 Autonomy gate (in code, not in the prompt) — reversibility taxonomy

Governing axis = **reversibility/recovery-cost**, not "importance":

| Tier | Destination | Examples in our stack | Policy |
|------|-------------|----------------------|--------|
| **T0 read-only** | classify / inbox view | `gtd_inbox`, `gtd_next` | always autonomous |
| **T1 reversible** | GTD lists, reference filing | `gtd_move` → next/project/waiting/someday; `skingest ingest` → skmem-pg+wiki | autonomous **if conf ≥ 0.80 AND not requires_human_review** |
| **T2 partially-reversible** | drafts / tentatives | draft email reply (gog, **never send**); **tentative** calendar hold | autonomous-with-notice (sk-alert) |
| **T3 irreversible** | outbound / destructive | `gog calendar create` (notifies attendees on accept), `gog gmail send`, trash/delete, file to **shared** folder | **MUST confirm** regardless of confidence |

- **Below threshold OR T3** → stamp `proposed_action` onto the item, set `status:awaiting_confirm`, push to `gtd_confirm_queue` (`coordination/gtd/pending.json`), surface in the **7:15 daily brief / sk-alert** for one-press Telegram approval.
- `gtd_confirm <item_id> approve|reject` executes the stamped action **with its idempotency_key** (execution-time "already done?" check → no-op on replay) and writes the audit entry.

### 2.5 GTD destination ↔ 4-C label mapping (consistency everywhere)

| classification | GTD list | 4-C label | Physical sink |
|---|---|---|---|
| actionable_next | `next-actions.json` | `1 Action` | GTD (T1) |
| waiting_for | `waiting-for.json` | `2 Waiting` | GTD (T1) |
| project | `projects.json` | `Projects/` | GTD (T1) |
| **reference** | — (fix dead alias) | `3 Read` + `r/` | **skingest → skmem-pg docs + wiki canon (T1)** |
| someday_maybe | `someday-maybe.json` | `4 Someday` | GTD (T1) |
| calendar_event | — | (event) | **gog calendar create, right account (T3 confirm)** |
| trash | archive | — | (T3 confirm) |

**Critical fix:** today GTD `reference` is a degenerate alias to `someday-maybe.json`. Wire reference-classified captures through `run_pipeline_for_file()` so reference material becomes **vectored/searchable/graphed** in the store we already run. Calendar account routing matters — nootropic/personal scheduling → `david.knestrick@gmail.com`.

### 2.6 Audit + idempotency

- Every triaged item's routing record + parser output persists to **skmemory** (`memory_store`) keyed by `source_id` → auditable, and `gtd_review` can show **auto-filed vs awaiting-confirm vs human-corrected**. Corrections are the feedback signal to tune per-tier thresholds.
- Reuse **`skpdf/gtd_filer.py`'s metadata-sidecar + sensitive-field detection** as the tamper-evident provenance record (`filed_by, filed_to[], source, sensitive_fields, tags`).
- Confirmation UI shows **exact action params** ("create event: 2026-07-02 14:00, Dr Rich, 30min"), never a paraphrase.

---

## 3. THE STORAGE-PLACEMENT POLICY (the meta-decision)

> **The model Chef is unsure about. Here is the recommendation, stated plainly.**

### 3.1 Recommended model: *sensible default + auto-route by policy + explicit override*

Three layers, cleanly separated (the anti-pattern is conflating them):

1. **NAMESPACE / metadata** = `skmem-pg` (Postgres, already source-of-truth, HA-mirrored .158→.41). The catalog answers *"where does it live."*
2. **CONTENT ADDRESS** = `sha256` of the blob. Location-independent. Store-once-per-host, verify-on-read, free dedup.
3. **PLACEMENT POLICY** = an ordered, declarative ruleset (`skos placement.yaml`) over hosts, in the spirit of **git-annex preferred-content**. The system converges to policy and self-heals after outages.

### 3.2 The catalog (extends the existing `file_locations` index)

`file_locations` today = `{id, node, path, doc_id, mtime, sha}`, `UNIQUE(node,path)`. **It already exists and `record_ingest_location()` already populates it** — it's just not wired to the pipeline. We extend it with a small companion table (don't break the existing one):

```sql
CREATE TABLE blob_catalog (
  sha256          text PRIMARY KEY,          -- content address
  logical_path    text,                      -- project/name (the namespace, NO host)
  content_type    text,
  doc_type        text,                      -- receipt/invoice/note/photo/...
  size            bigint,
  placement_class text,                      -- memory|doc|media|archive
  copies_required smallint NOT NULL DEFAULT 1,
  created         timestamptz, last_access   timestamptz
);
-- file_locations stays as the per-(node,path) replica index; blob_catalog.sha256 == file_locations.sha
```

One query joins `blob_catalog` ⨝ `file_locations` to answer *"what is this, where are all its copies, is it safe to drop here?"* — and `pg_search` already left-joins `file_locations` so **every retrieval hit carries `{node,path}`**.

### 3.3 The decision flowchart (evaluated at ingest, in order)

```
                  ┌─────────────────────────────────────────────┐
                  │  item has bytes + sha256 + triage record     │
                  └───────────────────┬─────────────────────────┘
                                      ▼
   (1) EXPLICIT OVERRIDE?  ── caller/skchat passed --host or --project ──► honor it. DONE.
                                      │ no
                                      ▼
   (2) PROJECT AFFINITY?   ── triage.project_hint matches a project ────► that project's home host
                                      │ no                                  (git-annex include=project/**)
                                      ▼
   (3) CONTENT-TYPE RULE:
        ├─ text / doc / note / email / reference ─► skmem-pg docs on .158  (small, queried, HOT)
        ├─ memory / journal / seed ──────────────► skmem-pg memories on .158 (copies_required=2 → .41 HA)
        └─ large media (img/video/audio > 5MB) ──► OBJECT TIER (Garage)     (NEVER the laptop)
                                      │
                                      ▼
   (4) CAPACITY GUARD: chosen host below free-space floor?
        └─ yes ─► fall back to next host by capacity weight (Garage native; else df-check)
                                      │ no
                                      ▼
   (5) DEFAULT LANDING: nothing matched ─────────► .158 PRIMARY
                                      │
                                      ▼
   (6) DURABILITY: ensure copies_required satisfied (replicate); record every copy in file_locations
                                      │
                                      ▼
   (7) TIERING (background, later): cold + large → demote .158 → Garage EC; never below copies_required
```

**Worked examples (the concrete answers Chef asked for):**

- **Scanned receipt, no project** → OCR'd, classified `receipt`, small → **skmem-pg docs on .158** (text/vectors); the **binary PDF → Garage** (or Nextcloud `r/` in Phase 1) with `copies_required=1`. Catalog row links them by sha256.
- **Doc tagged project Y** → project affinity (rule 2) → lands on **project Y's home host**; text → skmem-pg, binary → object tier; `r/` reference label.
- **Large media (video / image set)** → rule 3 → **Garage**, `replication_factor` enforces durability; **never replicated to .41 laptop** (low free space, USB disk). `largerthan=` steers it away.
- **A memory/journal entry** → `placement_class=memory`, `copies_required=2` → skmem-pg **.158 primary → .41 replica** (this IS our numcopies=2 — stated explicitly in policy).
- **Active project doc, recent** → stays HOT on .158 until the tiering sweep finds it cold + large.

### 3.4 Defaults + override UX

- **Default landing target = .158 primary** (it runs skmem-pg and is always-on). This is the "you don't have to choose" path — **auto-placement is the default**, choice is the exception.
- **Override UX**: one skos primitive — `skos store <file> [--project X] [--host H] [--class media|doc] [--copies N]` — runs the ordered ruleset, writes the catalog row, places bytes, returns locations. **All paths go through it** (skchat upload, skingest ingest, CLI). In skchat-app, an optional "File to…" affordance after share lets the user override; default is silent auto-place.

### 3.5 Durability floor (git-annex `required-content` semantics)

skos **refuses to delete/evict** a blob from a host if it would drop below `copies_required`, **regardless of capacity pressure**. Capacity-driven eviction only ever *moves cold blobs to a tier that still satisfies the floor* (hot .158 → Garage EC), never below it. This is structurally enforced, not left to ordering luck.

### 3.6 Object tier decision (a Chef decision — §7)

- **Phase-1 (no new infra): Nextcloud `r/` as the canonical binary archive** — already HA-ish, has `nextcloud-mcp` + `nextcloudcmd` sync. Fastest path to "captured photos/PDFs have a sovereign home."
- **Phase-2 (recommended target): stand up Garage** as the S3-native object/content tier. 2026 consensus fit for exactly our topology (3 heterogeneous home-lab nodes, residential links, CRDT > consensus on flaky links; Ceph/MinIO-distributed are "too heavy"). Tag `.158=core`, `.41=replica`, `.100=gpu/bulk`; capacity-weight the laptop low; `replication_factor=2–3`. skingest's `vision.py` already supports S3-accessible files.
- **Syncthing is NOT a placement decider** — no capacity/policy/durability logic; it replicates whole folders to whole devices and the introducer is a foot-gun. Keep it as a *dumb replication transport* for the flat-file dirs only. Placement decisions live in the skos policy layer + Garage layout.

---

## 4. The Content Pipeline (reuse skingest; bolt on the gaps)

skingest is **the most complete piece** — multimodal extract / embed / store / graph / synth, idempotent L1-L5, SHA256 dedup, hybrid skmem-pg (BM25+vector RRF) + AGE + wiki-canon. **Declared the SOLE ingestion home — new extractors register into `documents.DOC_EXTS` and flow through the existing pipeline; do NOT spawn a second pipeline.**

```
inbox artifact
  └─► [L1 PRESERVE] raw kept as provenance
  └─► [L2 EXTRACT]  ocr_router (NEW): Tier0 PyMuPDF embedded-text check → Tier1 tesseract
                    image_to_data (per-word conf, area-weighted) → escalate to Tier2 qwen3.6-VL
                    only when conf < 0.90  ◄── highest-value upgrade, cuts .100 GPU load
                    + metadata.py (NEW): Pillow/exif, pypdf props, mutagen/pymediainfo → sidecar frontmatter
                    + email.py / calendar.py / vcard.py (NEW): stdlib email/mailbox, icalendar, vobject
  └─► [L2.5 CLASSIFY] (NEW) zero-shot doc_type via qwen3.6 → frontmatter + docs.meta + AGE label;
                    receipts/invoices → structured-field extract (JSON schema + checksum: line-items==total;
                    cross-model digit verify tesseract vs VLM on financial fields → qc.gate on disagreement)
  └─► [L3 DECOMPOSE] chunker — tune CHUNK_TARGET 900→~450 (keep OVERLAP 200), A/B with eval.py first
  └─► [L4 EMBED]    mxbai-embed-large (1024-dim, .100:11434, 1100-char truncation) → skmem-pg docs upsert
                    ★ THEN call record_ingest_location(abs_path, doc_id, node) — WIRE THE INDEX (one-liner gap)
  └─► [L5 GRAPH+CANON] AGE nodes + wiki canon node
  └─► [L6 SYNTH]    (existing) summary/tags/entities JSON, qwen3.6 routed
```

**The single highest-value upgrade** = the Tier-0/1/2 OCR router (skingest jumps straight to qwen-VL for *every* image today — wastes the VRAM-constrained .100 box and risks hallucinated digits on receipts). **The single missing wire** = call `record_ingest_location()` inside `run_pipeline_for_file` after L4 — both pieces exist, they're just not connected. Keep PaddleOCR-VL 1.6 / MinerU 2.5-Pro / Docling on the radar as Tier-2 layout/table upgrades.

---

## 5. Mapping to Our Stack (reuse / extend / build)

| Capability | Verdict | Component |
|---|---|---|
| Triage classify engine | **REUSE as-is (re-prompt)** | `skingest/synth.py` route/_parse_json/fallback ladder |
| Reference filing (embed→store→graph) | **REUSE** | `skingest pipeline.py run_pipeline_for_file` + skmem-pg + AGE + canon |
| Multimodal extract | **REUSE** | `skingest extract/{documents,vision,transcribe,web}.py` |
| Federated locate/read | **REUSE** | access-plane `files.py`, `knowledge.py pg_locate`, skmem-pg HA mirror |
| Task sink (GTD verbs) | **REUSE** | `gtd_tools.py`; `dreaming.py` shows the auto-file pattern |
| Event sink + email source | **REUSE** | `gog` calendar/gmail; SKAlert cron + 7:15 brief = scheduler/observability |
| Phone rail | **REUSE** | `skchat-telegram-opus` + telegram_* + worship async-worker; skchat voice |
| `file_locations` index | **EXTEND** | add `blob_catalog` companion + wire `record_ingest_location` into pipeline |
| OCR | **EXTEND** | new `extract/ocr_router.py` (Tier 0/1/2) wrapping `vision.py` |
| Extractors | **BUILD (register into DOC_EXTS)** | `extract/{metadata,email,calendar,vcard}.py`, `classify.py` (L2.5) |
| Canonical inbox + write API | **BUILD** | inbox dir convention + `skos-inbox` capauth-signed endpoint + InboxItem envelope |
| Capture rails | **BUILD** | share-target in skchat-app; Telegram capture handler; gog IMAP poller; folder watcher |
| Agentic triage loop | **BUILD** | `gtd_triage` + `gtd_confirm_queue` + `gtd_confirm` MCP tools + batch drain |
| Placement-policy engine | **BUILD** | `skos placement.yaml` DSL + `skos store` primitive + capacity guard + durability floor |
| Object tier | **BUILD (Chef decides Phase)** | Nextcloud `r/` (P1) → Garage (P2) |
| Orchestration / observability | **BUILD** | coord epic + audit join + daily-brief triage surface |

---

## 6. Epic / Sprint Plan

> **EPIC: skos Ingest — Universal Capture → Agent Triage → Federated Placement**
> Tag: `skos-ingest`. Goal: one canonical inbox drained by Lumina into the right GTD/reference/calendar sink, with bytes auto-placed across the fleet and indexed in `file_locations`. 7 shippable sprints; each is independently demoable.

---

**S1 — The Inbox + drop endpoint** *(foundation)*
- **Goal:** One canonical inbox dir + InboxItem envelope + one capauth-signed write API; capture-on-capture dedup.
- **Components:** inbox dir convention (`~/.skcapstone/agents/$SKAGENT/inbox/{unprocessed,processing,done,failed}`); `skos-inbox` HTTP endpoint (tailnet, capauth-signed); `InboxItem` schema (`skos.inbox/1`); sha256-on-capture + dedup check vs `file_locations`.
- **Accept:** POST artifact+envelope → file pair in `unprocessed/`; duplicate sha → `status:dedup`, no second copy; `skos inbox list` shows queue.
- **Deps:** capauth, access-plane. **Risk:** large-payload handling (multi-page PDFs / video) — stream, don't buffer.

**S2 — Phone-scan capture** *(the primary mobile rail + its redundant twin)*
- **Goal:** "Share to Lumina" from any phone app lands in the inbox; synced drop folder as the redundant twin.
- **Components:** `share_handler`/`receive_sharing_intent` in skchat-app + Android `ACTION_SEND`/`<share-targets>` + iOS Share Extension + App Group; R4 Syncthing `drop/` + inotify watcher.
- **Accept:** share a scanned PDF from phone → appears in `unprocessed/` within seconds; drop a file in `drop/` → same; both verified with a multi-page PDF and a large image.
- **Deps:** S1. **Risk:** iOS Share Extension out-of-process memory limits (drops large scans) — test explicitly.

**S3 — Agent triage MVP** *(the brain)*
- **Goal:** Lumina drains the inbox, classifies each item, auto-files reversible high-confidence, queues the rest.
- **Components:** `gtd_triage` MCP tool (synth.py-pattern, json_schema); deterministic parsers (icalendar, email/mailbox); autonomy gate (T0/T1 auto if conf≥0.80); routing record persisted to skmemory; batch drain (cron + on-demand command).
- **Accept:** 3 mandatory tests — (a) happy-path autonomous reversible file; (b) low-confidence → stays in inbox with `proposed_action`; (c) irreversible (calendar/email) never auto-executes. Reference items route through skingest (fix the dead alias).
- **Deps:** S1. **Risk:** structured hallucination (valid JSON, hallucinated date/confidence) — pair schema validation with semantic checks; batch on .100, no per-item embeds (5060 load).

**S4 — Storage-placement policy engine** *(the meta-decision)*
- **Goal:** One `skos store` primitive runs the ordered ruleset and records every copy; durability floor enforced.
- **Components:** `skos placement.yaml` DSL (git-annex preferred-content semantics); `skos store` primitive; `blob_catalog` table + wire `record_ingest_location` into `run_pipeline_for_file`; capacity guard (df / Garage weights); durability floor (required-content semantics); `placement_class`/`copies_required` columns.
- **Accept:** receipt-no-project → text to skmem-pg/.158 + binary to object tier; project-Y doc → home host; large media → object tier (never .41); memory → copies=2 (.158→.41); eviction refuses to drop below floor; `pg_locate` returns all copies.
- **Deps:** S1, object tier (S4 can ship with Nextcloud `r/` as the object tier; Garage is a stretch). **Risk:** encoding host into path (kills rebalance/dedup) — enforce hash-addressing in review.

**S5 — OCR / content pipeline upgrade** *(quality + GPU savings)*
- **Goal:** Tiered confidence-routed OCR + metadata + classify, all through skingest.
- **Components:** `extract/ocr_router.py` (Tier0 PyMuPDF / Tier1 tesseract area-weighted conf / Tier2 qwen-VL <0.90); `extract/metadata.py` (Pillow/exif, pypdf, mutagen, pymediainfo); `classify.py` L2.5 (doc_type + receipt structured-extract + checksum + cross-model digit verify → qc.gate); chunker 900→~450 A/B'd with eval.py.
- **Accept:** digital-native PDF skips OCR (Tier-0); clean scan handled by Tier-1 (no GPU); messy/handwritten escalates to VLM; receipt totals checksum-validated; EXIF capture-date lands in `docs.meta`; eval recall@10/MRR not regressed.
- **Deps:** S3 (triage consumes doc_type). **Risk:** VLM digit transposition on financials — cross-model verify mandatory for receipts.

**S6 — Email + .ics capture** *(the universal rail + calendar)*
- **Goal:** Email-to-inbox live; .ics invites parsed; calendar events routed (confirm-gated).
- **Components:** gog IMAP poller (plus-address or `1 Inbox` label, IDLE/poll, track UIDs) → Inbox endpoint; calendar.py VEVENT parse (UID = idempotency key); `gog calendar create` as T3 confirm action; account routing (personal → david.knestrick@gmail.com); tentative-hold default for invites (never auto-accept — PARTSTAT is outbound/irreversible).
- **Accept:** forward an email w/ attachment → inbox item; .ics → tentative calendar proposal in `gtd_confirm_queue`; FYI-only invite files to reference autonomously; no duplicate events on retry (UID check).
- **Deps:** S3. **Risk:** IMAP duplicate-delivery / draining real mail — dedicated capture mailbox/label + processed-UID tracking.

**S7 — Placement UX + override + confirm loop + observability** *(close the loop)*
- **Goal:** Human-in-the-loop confirm from Telegram; override UX; full audit trail surfaced in the daily brief.
- **Components:** `gtd_confirm_queue` (`coordination/gtd/pending.json`) + `gtd_confirm approve|reject` (executes with idempotency_key, exact-params UI); sk-alert one-press Telegram approval; "File to…" override in skchat-app; audit join (captured→triaged→filed→located) in skmemory; 7:15 daily-brief triage section (auto-filed vs awaiting-confirm vs corrected) → threshold-tuning feedback loop.
- **Accept:** an irreversible action waits in the queue, Chef approves from Telegram (one tap), executes exactly once; daily brief shows the day's triage decisions; a correction is logged as a tuning signal.
- **Deps:** S3, S4, S6. **Risk:** approval-latency UX — durable interrupt/resume (minute+ latency is normal), idempotency non-negotiable.

**Optional S8 — Garage object tier** *(if Chef chooses S3-native)*
- **Goal:** Replace/augment Nextcloud `r/` with Garage. Tag zones (`core/replica/gpu`), capacity-weight nodes, `replication_factor=2–3`, staged/versioned layout. **Risk:** serialize layout-apply versions (duplicated `--version N` → inconsistency).

---

## 7. Open Questions / Decisions for Chef

1. **Object tier (the big one):** Phase-1 **Nextcloud `r/`** (zero new infra, ships now) vs Phase-2 **Garage** (S3-native, the 2026-right tool for our topology, more durability/dedup control)? **Recommendation: ship S4 on Nextcloud `r/`, schedule Garage as S8.**
2. **Default landing target:** confirm **.158 primary** as the silent default, with auto-placement (not user-choice) as the norm. Agree?
3. **`copies_required` defaults:** memory/journal = **2** (.158→.41 HA); docs/reference = **1** (skmem-pg HA covers the index; binary single-copy in object tier) or **2**? Large media in Garage = `replication_factor` 2 or 3?
4. **Capacity floors:** what free-space floor triggers the capacity guard per host (esp. the .41 laptop on USB disk)?
5. **Autonomy threshold:** start at **conf ≥ 0.80** for T1 reversible filing? And confirm **always-confirm** for all T3 (calendar/email/delete/shared-folder) regardless of confidence.
6. **Email capture address:** plus-addressed on an existing Gmail (`+inbox`) vs a dedicated `1 Inbox` label polled on `chefboyrdave2.1@gmail.com`? (Avoid self-hosting inbound SMTP.)
7. **Inbox scope:** per-agent (`$SKAGENT/inbox/`) only, or a shared fleet inbox too? Recommendation: per-agent canonical, Lumina is the default drainer.
8. **Tiering aggressiveness:** how cold + how large before demoting .158 → Garage EC? (Drives the background sweep cadence.)

---

**Key file/path references (absolute):**
- Pipeline + layers: `/home/cbrd21/clawd/skingest/src/skingest/pipeline.py`
- Triage engine to re-prompt: `/home/cbrd21/clawd/skingest/src/skingest/synth.py`
- Extractors to extend: `/home/cbrd21/clawd/skingest/src/skingest/extract/{documents,vision,transcribe,web}.py`
- QC gate: `/home/cbrd21/clawd/skingest/src/skingest/qc.py`
- `file_locations` index + `record_ingest_location` (the wire-up): `/home/cbrd21/clawd/skcapstone-repos/skcomms/src/skcomms/access/knowledge.py`
- Access-plane file tools: `/home/cbrd21/clawd/skcapstone-repos/skcomms/src/skcomms/access/files.py`
- GTD verbs + auto-file pattern: `skcapstone/src/skcapstone/mcp_tools/gtd_tools.py`, `dreaming.py`
- GTD filer (audit/sensitive-field reuse): `/home/cbrd21/clawd/skpdf/src/skpdf/gtd_filer.py`
- 4-C system reference: `/home/cbrd21/clawd/wiki/tools/gtd-4c-system.md`

Verified against live code: `file_locations` schema is `{id, node, path, doc_id, mtime, sha}` `UNIQUE(node,path)` with `record_ingest_location()` present but **not called by `pipeline.run_pipeline_for_file`** (the one-line wire-up gap), and the L1-L5 idempotent layers + QC gate are as inventoried.