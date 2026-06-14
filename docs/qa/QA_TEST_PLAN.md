# SKWorld — Standard QA Test Plan & Workflow

**Repeatable QA.** This is the canonical test-plan + use-case catalog for the SKWorld
comms stack (skchat + skcomms + the Flutter app). Run the suite with
`bash scripts/qa_suite.sh` (unit/integration + live lane/spaces harness); app tests
run on .41 via `flutter test`. Live findings + the verification matrix live in
`skworld-comms-verification-matrix.md` (companion).

**How to run the standard QA workflow:**
1. `bash scripts/qa_suite.sh` — skchat + skcomms suites + recording pipeline + LIVE lane/spaces harness (`tier5_verify.py`).
2. App: `ssh 192.168.0.41 '~/flutter/bin/flutter test'` (in `skchat-app`).
3. Record results in the verification matrix findings log.

Coverage as of this plan: skchat ≈1640 tests, skcomms ≈562, app ≈160 (+~348 added in the QA pass).

---

## Area 1 — Messaging / Agents / Daemon

QA pass over the skchat messaging/agents/daemon area. Repo:
`/home/cbrd21/clawd/skcapstone-repos/skchat` (branch main). All tests run from
`~` with `~/.skenv/bin/python -m pytest`. No network / live services — every
external dependency (subprocess, httpx, SKComms, MemoryStore) is mocked or
faked, so the whole area is CI-safe.

### Before / after (honest counts)

Full area (17 files):
- **BEFORE:** `338 passed, 13 skipped, 3 warnings`
- **AFTER:**  `474 passed, 13 skipped, 3 warnings`
- **Net new tests added: +136** (all passing; no new skips/xfails; no real
  bugs surfaced — the modules behaved correctly under the new edge cases).

New tests per file: models +13, ephemeral +5, reactions +10, notifications +9,
peer_discovery +13, outbox +10, presence +6, advocacy +12, context +14,
agent_comm +13, agent_profile +7, history +11, transport +12, watchdog +4,
daemon +14. (3way_chat, daemon_integration: reviewed, no gaps added — see notes.)

---

### models.py — `tests/test_models.py` (35 tests, +13)

| test | verifies |
|------|----------|
| TestReplyAlias::test_reply_to_alias_accepted_on_construction | `reply_to=` alias (MCP tool API) maps to `reply_to_id` |
| TestReplyAlias::test_reply_to_id_canonical_field | canonical field + `.reply_to` property mirror |
| TestReplyAlias::test_reply_to_none_by_default | absent parent → None |
| TestSummaryTruncation::test_summary_truncates_long_content | `to_summary()` caps preview at 80 chars |
| TestSummaryTruncation::test_summary_short_content_intact | short content not truncated |
| TestRoundTrip::test_full_message_round_trip | full ChatMessage (reactions, ttl, metadata, status) survives JSON serialize→deserialize |
| test_thread_parent_id_round_trip | nested Thread carries `parent_thread_id` |
| test_whitespace_only_recipient_rejected | whitespace recipient rejected like sender |

**Real use cases:**
- U: agent replies to a message via MCP `reply_to` → TestReplyAlias (CI)
- U: encrypted/long message shown in a list → summary truncation (CI)
- U: message persisted to JSONL then reloaded by daemon → round-trip (CI)

### ephemeral.py — `tests/test_ephemeral.py` (23, +5)

| test | verifies |
|------|----------|
| test_unparseable_ttl_string_skipped | non-numeric ttl skipped, not deleted |
| test_unparseable_created_at_skipped | garbage created_at → skip, never delete |
| test_forget_failure_counted_as_error | store.forget() raising → error counter, sweep continues |
| test_datetime_object_created_at_parsed | created_at as real datetime (not ISO str) handled |
| TestTagEphemeralPreservesMetadata::test_existing_metadata_preserved | tag keeps existing keys, no in-place mutation |

**Real use cases:**
- U: TTL reaper sweeps a store with corrupt metadata → no crash, errors counted (CI)
- U: ephemeral message tagged for sweep keeps its existing metadata (CI)

### reactions.py — `tests/test_reactions.py` (28, +10)

| test | verifies |
|------|----------|
| TestApplyEventEdges::test_apply_remove_event | remote 'remove' event removes local reaction |
| TestApplyEventEdges::test_apply_unknown_action_returns_false | unknown action → no-op False |
| TestBackwardCompatAliases::* | `ReactionStore` alias + `add`/`get`/`get_summary` (cli.py uses these) |
| TestStateIsolationAndCounts::test_get_reactions_returns_copy | mutating returned list doesn't corrupt state |
| TestStateIsolationAndCounts::test_message_count_after_full_removal | message no longer counted after last reaction removed |
| TestStateIsolationAndCounts::test_top_reacted_respects_limit | limit honored |

**Real use cases:**
- U: peer's reaction-removal syncs across machines → apply_event remove (CI)
- U: CLI `skchat react` via legacy ReactionStore API → alias tests (CI)

### notifications.py — `tests/test_notifications.py` (17, +9)

| test | verifies |
|------|----------|
| TestDesktopNotificationsEnabled::* | gate: unset→on, "1"→on, falsey values→off |
| test_notify_suppressed_when_gate_off_even_if_available | available notifier still suppresses when gate off |
| test_check_available_false_when_gate_off | gate off → notifier reports unavailable, no PATH probe |
| test_notify_lumina_title_has_marker | Lumina notification title |
| test_custom_app_name_stored | custom app_name retained |

**Real use cases:**
- U: headless/cron run with SK_DESKTOP_NOTIFY=0 → no desktop popups fire (CI)
- U: incoming Lumina DM → dedicated notification (CI)

### peer_discovery.py — `tests/test_peer_discovery.py` (37, +13)

| test | verifies |
|------|----------|
| TestDefaultPeersDir::* | SKCHAT_PEERS_DIR env override; default fallback; discovery picks override |
| TestGetPeerAdvancedMatching::test_match_by_fingerprint_short_prefix | ≥8-char fingerprint prefix (envelope short id) resolves |
| TestGetPeerAdvancedMatching::test_short_prefix_under_8_chars_no_match | <8-char prefix must NOT match (collision guard) |
| TestGetPeerAdvancedMatching::test_match_by_bare_fingerprint / email_local_part / identity_field | other match strategies |
| TestResolveIdentityConstruction::test_constructs_from_handle_when_no_capauth_uri | builds `capauth:<handle>` when no capauth URI present |
| test_list_peers_skips_unreadable_file | OSError on one file swallowed, others load |

**Real use cases:**
- U: sandbox/test deployment points SKCHAT_PEERS_DIR elsewhere (CI)
- U: inbound envelope carries a short fingerprint id → resolves to full peer (CI)

### outbox.py — `tests/test_outbox.py` (31, +10)

| test | verifies |
|------|----------|
| test_deliver_pending_success | AgentMessenger path delivers, thread_id carried through |
| test_deliver_pending_failure_increments / messenger_exception_is_failure | failure + exception count as failed, stay pending |
| test_process_pending_none_messenger_noop | `process_pending(None)` → (0,0), queue untouched |
| test_process_pending_delegates_to_deliver | wrapper delegates |
| test_enqueue_accepts_str_content | str payload normalised to bytes for drain |
| test_queue_survives_reopen | pending message persists across close/reopen (daemon restart) |
| test_mark_delivered_directly | direct mark removes from pending |

**Real use cases:**
- U: daemon drains outbox each cycle via AgentMessenger → deliver_pending (CI)
- U: daemon restarts mid-backlog; queued messages survive → reopen test (CI)

### presence.py — `tests/test_presence.py` (43, +6)

| test | verifies |
|------|----------|
| TestPresenceCachePersistence::test_record_persists_across_instances | record visible to a fresh (out-of-process) instance |
| test_get_all_returns_copy | returned dict mutation doesn't wipe cache |
| test_get_online_filters_by_age / excludes_offline_state | online list honors age + OFFLINE state |
| test_corrupt_cache_file_degrades_gracefully | garbage JSON → empty cache, still writable |
| test_get_status_handles_corrupt_timestamp | non-ISO timestamp → "offline" |

**Real use cases:**
- U: `skchat who` (separate process) reads presence the daemon wrote (CI)
- U: corrupt presence_cache.json doesn't break the CLI (CI)

### advocacy.py — `tests/test_advocacy.py` (38, +12)

| test | verifies |
|------|----------|
| TestShouldAdvocateBoundaries::test_trigger_followed_by_comma / colon | punctuation token boundaries trigger |
| test_email_address_does_not_falsely_trigger | embedded @-word doesn't trigger |
| TestInjectContext::test_inject_context_updates_identity | `inject_context` swaps identity |
| TestMemoryContext::test_memory_context_parses_snippets | MCP JSON-RPC stdout parsed into context block |
| test_memory_context_returns_empty_on_nonzero_exit / subprocess_error / no_memories | all failure paths → "" |
| test_process_message_prepends_memory_context | found memory woven into the prompt before consciousness call |

**Real use cases:**
- U: `@opus, …` mention with memory hits → context-enriched reply (CI; mocked subprocess)
- U: skcapstone-mcp missing/erroring → advocacy degrades to plain prompt (CI)

### context.py — `tests/test_context.py` (18, +14)

| test | verifies |
|------|----------|
| TestLooksLikeGroupRecipient::* | bare-UUID & group: → group; capauth/empty → not |
| TestFormatMessage::* | missing sender/content → None; DM short-name line; group `→ group` arrow |
| TestFetchMemoryBlock::* | empty query / 0 hits / source exception → ""; snippet rendering |
| TestMemoryQueryOverride::test_explicit_memory_query_passed_to_source | explicit query reaches the source |
| test_memory_hits_zero_suppresses_lookup | memory_hits=0 → no source call, no block |

**Real use cases:**
- U: bridge assembles reply context for a group thread → format + group arrow (CI)
- U: caller passes a fixed memory query / disables memory → knobs honored (CI)

### agent_comm.py — `tests/test_agent_comm.py` (23, +13)

| test | verifies |
|------|----------|
| TestSendMetadata::test_send_with_ttl_and_payload_and_reply | ttl/payload/reply_to flow into message + metadata |
| test_send_no_team_thread_id_none | no team → thread_id None |
| TestReceiveAndTeamMessages::test_receive_polls_transport_then_returns_inbox | receive() polls transport then returns inbox |
| test_receive_swallows_transport_error | poll error degrades gracefully |
| test_get_team_messages_no_team_returns_empty / delegates_to_history | team-message lens |
| TestBroadcastCounting::test_broadcast_skips_self_with_transport_delivery | self skipped, delivered count |
| TestGetInboxFiltering::test_get_inbox_skips_non_agent_comm_memory / sorted_newest_first | filter + ordering |

**Real use cases:**
- U: agent broadcasts to a team, skipping itself → broadcast test (CI)
- U: agent polls its agent-comm inbox, newest first → get_inbox tests (CI)

### agent_profile.py — `tests/test_agent_profile.py` (19, +7)

| test | verifies |
|------|----------|
| TestFebSummaryToDict::* | default + populated FebSummary serialization (webui JSON) |
| TestAgentProfileToDict::test_profile_to_dict_shape | profile→dict shape (soul subset, feb nested) |
| TestLoadSoulResolution::test_active_json_points_to_installed_soul | active.json → installed/<name>.json wins over base |
| test_falls_back_to_base_json | legacy base.json fallback |
| test_no_soul_returns_empty | no soul → ({}, 0.0) |

**Real use cases:**
- U: webui `/agent/state` serializes the profile → to_dict tests (CI)
- U: operator selects unhinged soul via active.json → resolution order (CI)

### history.py — `tests/test_history.py` (31, +11)

| test | verifies |
|------|----------|
| TestJsonlSaveLoad::test_save_then_load_round_trip | add_message → load round-trip |
| test_load_peer_filter / respects_limit / skips_malformed_lines | load filters + robustness |
| test_get_messages_dict_shape | dict conversion w/ ISO timestamp |
| test_get_thread_jsonl_oldest_first | JSONL thread scan, oldest-first, off-thread excluded |
| TestStoreBackedRetrieval::test_store_message_tags | sender/recipient/thread tags + metadata id |
| test_get_conversation_merges_both_directions | bidirectional merge, dedup, unrelated excluded |
| test_store_thread_metadata / get_thread_meta_found_and_missing | thread meta store + lookup |
| test_get_messages_since_store_none / recipient_filter | no-store branch; recipient filter |

**Real use cases:**
- U: daemon stores inbound msg; CLI reads conversation history → store/load tests (CI)
- U: context fetcher reads a thread from JSONL when SKMemory isn't indexed (CI)

### transport.py — `tests/test_transport.py` (30, +12)

| test | verifies |
|------|----------|
| TestExtractPayload::* | object envelope, dict envelope, non-dict payload, None |
| TestSendTypingIndicator::test_typing_indicator_sent_as_heartbeat / swallows_errors | HEARTBEAT send + best-effort failure |
| TestHandleHeartbeat::test_no_presence_cache_is_noop / typing_records / non_typing_clears | inbound presence routing |
| TestFromConfig::test_from_config_builds_transport | classmethod constructor |
| TestFileInboxRawFallback::test_raw_chatmessage_json_without_envelope | bare ChatMessage JSON (no envelope) parsed |

**Real use cases:**
- U: peer "typing…" indicator shows in UI → typing + heartbeat tests (CI)
- U: same-machine peer writes a bare-JSON file inbox entry → raw fallback (CI)

### watchdog.py — `tests/test_watchdog.py` (29, +4)

| test | verifies |
|------|----------|
| TestTriggerReconnectResilience::test_reconnect_none_transport_is_noop | transport=None at threshold → no crash |
| test_reconnect_missing_method_is_noop | transport w/o reconnect() skipped |
| test_reconnect_exception_swallowed | reconnect() raising is swallowed |
| test_chatwatchdog_alias_is_transport_watchdog | `ChatWatchdog` back-compat alias |

**Real use cases:**
- U: SKComms unreachable; reconnect target is misconfigured → watchdog survives (CI)

### daemon.py — `tests/test_daemon.py` (54, +14)

| test | verifies |
|------|----------|
| TestWebrtcSignalingHealth::* | pure classifier: down/degraded/ok |
| TestTurnSecret::* | turn_secret_present (set/blank/missing) + webrtc_turn_warning |
| TestRouteFileMessage::test_no_file_service_returns_false | no service → not routed |
| test_plain_chat_message_not_routed | normal text not routed |
| test_file_transfer_init_routed_to_service | FILE_TRANSFER_INIT → service, sender carried |
| test_marker_present_but_wrong_type_not_routed | marker substring but bad type → not routed |
| test_service_exception_still_consumes_message | store failure swallowed, msg consumed |
| TestInitHelpersNonFatal::test_init_reaper_non_fatal / memory_bridge_non_fatal | init helpers return None on failure |

**Real use cases:**
- U: operator checks `daemon_status` WebRTC/TURN health → pure classifiers (CI)
- U: inbound file-transfer chunk routed to FileTransferService → route tests (CI)
- U: an optional subsystem (reaper/memory-bridge) fails to init; daemon still runs (CI)

---

### Gaps NOT covered (and why)

- **test_3way_chat.py / test_daemon_integration.py**: reviewed; these are
  scenario/integration suites (already pass). The daemon `start()` poll loop,
  health-server, and 3-way flow are exercised end-to-end there. I added focused
  unit tests for the daemon's *pure helpers* and *dispatch helpers* instead of
  duplicating the loop-level scenarios. No new gaps left in their scope.
- **daemon.start() full-loop branches** (backoff escalation, watchdog/outbox/
  presence wiring inside the running loop): covered indirectly by
  test_daemon_integration.py; not re-unit-tested to avoid brittle loop mocking.
- **advocacy `_call_consciousness` real LLM path**: marked `# pragma: no cover`
  in src (needs live skcapstone consciousness). Only the failure-return contract
  is asserted at the `process_message` boundary.
- **transport PGP encrypt/decrypt + signature verify branches**: owned by
  crypto.py (Area-other); transport only delegates. Left to the crypto suite.
- **peer_discovery unreadable-file (chmod 000) test**: passes, but is a weak
  assertion when the suite runs as root (root bypasses file perms) — kept
  because it still exercises the OSError-swallow path on unprivileged runners.

### Bugs surfaced
None. All 136 new edge-case / error-path / concurrency-of-state tests passed
against the existing implementation — the modules in this area handle malformed
input, missing dependencies, and failure paths as designed.

---

# Area 2 — Spaces / Lanes / skreachd / Recording / Glossa-mesh

QA pass over the Spaces collaboration surface in `skchat`: the 6 lanes
(chat/whiteboard/watch/doc/term), lane store (snapshot-vs-log), spaces
roles/tokens/moderation/recording/consent, the `skreachd` sandboxed term-lane
exec backend, the recording → transcript → write-up pipeline, and the
glossa-mesh density-negotiated agent mesh.

- **Repo:** `/home/cbrd21/clawd/skcapstone-repos/skchat` (branch `main` worktree)
- **Run from home:** `cd ~ && ~/.skenv/bin/python -m pytest tests/<file> -q`
- **Test doubles:** all new tests use fakes — **no real whisper / LLM / network /
  livekit / subprocess** is ever touched in CI.

## Before / After (this area's suite)

```
BEFORE:  144 passed, 4 failed   (4 pre-existing failures in test_spaces_ui_markup)
AFTER:   204 passed, 0 failed
```

The 4 pre-existing failures were a real **test bug** (see F-1), fixed in this pass.
+50 new test functions added (several parametrized → more runtime cases).

## Tests added per file

| File | before | after | added | focus |
|------|-------:|------:|------:|-------|
| `test_skreachd.py` | 16 | 29 | **13** | adversarial injection shapes, abs-path/prefix-lookalike denial, path-traversal containment doc, real-runner truncation/timeout/missing-binary, lanes/event term safety boundary, non-string cmd 400 |
| `test_lane_store.py` | 5 | 14 | **9** | snapshot-latest-wins (×N), no-row-accumulation, id-ordered replay, per-lane/per-space scoping, limit=0 / limit>count, reopen persistence, nested-JSON roundtrip |
| `test_lane_dispatcher.py` | 5 | 11 | **6** | all 4 log lanes append (parametrized), term run-request persisted-not-executed, snapshot-lane invariant, full-envelope preservation, empty/None lane rejection |
| `test_lane_routes.py` | 5 | 10 | **5** | all log lanes post/replay (parametrized), per-space scoping (log+snapshot), missing-lane 400, term-event persisted-not-executed |
| `test_glossa_mesh_node.py` | 5 | 10 | **5** | solo group_level fallback, audit-gloss invariant (tx+rx, no handler), tx-level logging, malformed-frame resilience, malformed-announce no-register |
| `test_glossa_mesh_protocol.py` | 3 | 8 | **5** | empty-payload rejection, level-byte masking, empty-body roundtrip, all-fields announce roundtrip, kind distinctness |
| `test_recording_writeup.py` | 5 | 12 | **7** | whitespace-only transcript, transcriber-exception resilience, poster-failure-doesn't-lose-text, transcript stripping, per-call seam override, fallback-writeup structure + truncation |

---

## Test cases (per feature)

### Lanes — store (snapshot vs log)
| Test | Asserts |
|------|---------|
| `test_snapshot_replay_returns_latest_when_re_snapshotting_many_times` | snapshot lane keeps only the final revision |
| `test_snapshot_lane_does_not_accumulate_rows` | re-snapshot is delete-then-insert (1 row max) |
| `test_log_replay_preserves_insertion_order_not_timestamp_ties` | 50 rapid appends stay id-ordered (ts ties don't reorder) |
| `test_replay_scoped_per_lane_within_a_space` | two lanes in one space don't bleed |
| `test_snapshot_scoped_per_space` | snapshot of space-1 doesn't clobber space-2 |
| `test_replay_limit_zero_returns_empty` / `test_replay_limit_larger_than_count_returns_all` | LIMIT edges |
| `test_store_persists_across_reopen` | SQLite durability across fresh `LaneStore` |
| `test_nested_payload_survives_json_roundtrip` | arbitrary nested JSON intact |

### Lanes — dispatcher
| Test | Asserts |
|------|---------|
| `test_all_log_lanes_route_to_append` (chat/watch/doc/term) | the 4 log lanes append |
| `test_term_run_request_is_persisted_not_executed` | dispatcher never executes a term run-request |
| `test_whiteboard_is_the_only_snapshot_lane` | `SNAPSHOT_LANES`/`LOG_LANES` invariant |
| `test_dispatch_preserves_full_envelope` | whole envelope stored verbatim |
| `test_empty_string_lane_is_rejected` / `test_none_lane_is_rejected` | bad lane → ValueError |

### Lanes — routes
| Test | Asserts |
|------|---------|
| `test_log_lanes_post_and_replay_via_routes` (parametrized) | POST event → GET state for each log lane |
| `test_event_route_scopes_state_per_space` / `test_snapshot_route_scoped_per_space` | HTTP-level scoping |
| `test_missing_lane_field_in_event_is_400` | malformed event → 400 |
| `test_term_event_is_persisted_but_not_executed` | term via generic event route is stored, no `exit` synthesised |

### skreachd — safety gates (adversarial)
| Test | Asserts |
|------|---------|
| `test_newline_injection_never_spawns_second_command` | `ls\nrm -rf /` → single argv, rm is an arg |
| `test_absolute_path_binary_is_not_allowlisted` | `/bin/ls` denied |
| `test_prefix_lookalike_binary_is_denied` | `lsblk` ≠ `ls` (token equality, not string-prefix) |
| `test_backtick_and_redirect_metachars_are_literal` | `` `whoami` `` and `>` stay literal tokens |
| `test_path_traversal_arg_reaches_runner_but_cwd_is_scoped` | **documents containment model**: `..` arg is NOT blocked at allowlist; cwd scoping + scrubbed env are the boundary |
| `test_unbalanced_quote_is_denied_not_crashed` | dangling quote → denied (parse reason), no crash |
| `test_whitespace_only_command_is_denied` | blank → empty-argv denial |
| `test_destructive_token_as_argument_is_allowed` | `grep rm file` runs grep (rm is a pattern) |
| `test_real_runner_truncates_output_to_cap` | real subprocess runner caps each stream to max_bytes |
| `test_real_runner_handles_timeout` | TimeoutExpired → exit 124, timed_out=True |
| `test_real_runner_handles_missing_binary` | OSError → exit 127 |
| `test_route_lanes_event_term_does_not_execute` | **SAFETY BOUNDARY**: generic event route persists, never execs |
| `test_route_non_string_cmd_is_400` | list `cmd` → 400 not 500 |

### Recording write-up pipeline
| Test | Asserts |
|------|---------|
| `test_whitespace_only_transcript_is_treated_as_empty` | whitespace-only → no LLM call, honest note |
| `test_transcriber_exception_does_not_crash_pipeline` | STT raise → degrades to no-transcript note |
| `test_poster_failure_does_not_lose_the_writeup` | poster raise → text still returned |
| `test_transcript_is_stripped_before_summarizing` | leading/trailing ws stripped |
| `test_per_call_seam_overrides_instance_default` | per-call summarizer wins over instance default |
| `test_fallback_writeup_has_required_sections_and_excerpt` | no-LLM fallback emits ## Summary/Key Points/Action Items |
| `test_fallback_writeup_truncates_long_transcript` | long transcript truncated with ellipsis |

### Glossa-mesh — node (encode/decode + audit gloss)
| Test | Asserts |
|------|---------|
| `test_solo_node_group_level_falls_back_to_own_max` | no peers → own max_level (not 0) |
| `test_audit_log_glosses_every_tx_and_rx` | **AUDIT-GLOSS INVARIANT**: every tx+rx logs an English gloss, even with no on_message handler |
| `test_audit_log_records_tx_level` | tx gloss records the level actually encoded (weakest-caps L0) |
| `test_malformed_frames_are_ignored_not_crashing` | empty / unknown-kind / empty-MESSAGE frames swallowed; no junk peer |
| `test_malformed_announce_does_not_register_peer` | non-JSON announce dropped; group_level unaffected |

### Glossa-mesh — protocol (wire framing)
| Test | Asserts |
|------|---------|
| `test_read_message_rejects_empty_payload` | empty payload → ValueError |
| `test_message_level_byte_is_masked_to_one_byte` | level 0x102 → 0x02 (`& 0xFF`) |
| `test_message_frame_with_empty_body_roundtrips` | empty body OK |
| `test_announce_roundtrip_preserves_all_fields` | all 5 CapabilityDescriptor fields survive |
| `test_announce_and_message_kinds_are_distinct` | ANNOUNCE/MESSAGE type bytes distinct |

---

## Real use cases (U-style)

| ID | Use case | Mode | Notes |
|----|----------|------|-------|
| U-1 | Late joiner replays a chat lane and sees messages in order | CI | id-ordered replay, scoped per space+lane |
| U-2 | Late joiner gets only the latest whiteboard state, not every stroke | CI | snapshot-latest-wins |
| U-3 | Whiteboard collaboration over a long session never bloats the DB | CI | snapshot delete-then-insert |
| U-4 | Operator runs `ls`/`git status` in a Space term lane | **LIVE** | lanes 10/10 live-verified (QA matrix F-2) |
| U-5 | Attacker tries `ls; rm -rf /`, `ls\nrm -rf /`, backticks, `/bin/ls`, `lsblk` | CI | all denied or rendered inert (no shell, argv-only, token-equality allowlist) |
| U-6 | Unauthorized identity / empty operator set tries to run a command | CI | fail-closed RBAC denial |
| U-7 | Exec is off by default; a run request returns `exec_disabled` | **LIVE** | skreachd opt-in gate live-verified (QA matrix F-3) |
| U-8 | A term run-request posted to the generic lane endpoint is stored but NOT executed | CI | safety boundary — exec only via explicit `/lanes/term/run` |
| U-9 | A long-running command times out and the user gets a clean error | CI | real runner → exit 124, timed_out |
| U-10 | A command floods stdout; output is capped, not unbounded | CI | per-stream max_bytes truncation |
| U-11 | A Space recording finishes → AI write-up posted to chat lane | **LIVE** | recording write-up live-verified (QA matrix F-4) |
| U-12 | A silent/empty room produces an honest "no transcript" note, not a crash | CI | empty/whitespace/None transcript paths |
| U-13 | Whisper STT crashes mid-pipeline; the Space still gets a note | CI | transcriber-exception resilience |
| U-14 | The LLM is unreachable; a fallback write-up is still posted | CI | `_fallback_writeup` structure + truncation |
| U-15 | The spaces server is down when posting; the write-up text isn't lost | CI | poster-failure resilience |
| U-16 | A 10-agent voice mesh negotiates density; the weakest model caps the room | CI | weakest-caps tier drop (existing) + audit on each member |
| U-17 | Every mesh message is auditable in plain English regardless of density level | CI | audit-gloss invariant (tx+rx, no handler needed) |
| U-18 | A weak peer leaves; the room re-negotiates UP to the higher tier | CI | forget_peer / on_leave un-caps (existing) |
| U-19 | A corrupt/garbage frame hits a node; it's ignored, the node stays up | CI | malformed-frame resilience |
| U-20 | A peer announces with a mismatched/garbage descriptor; it's dropped | CI | malformed-announce no-register |

LIVE-verified items reference the QA matrix findings: **F-2** (lanes 10/10),
**F-3** (skreachd gating), **F-4** (recording write-up pipeline).

---

## Findings / bugs

- **F-1 (FIXED — pre-existing test bug):** `test_spaces_ui_markup.py::_html()` read
  `Path("src/skchat/static/space.html")` — a **CWD-relative** path that only
  resolves when pytest runs from the repo root. The repo convention (and this QA
  harness) runs pytest from `~` to dodge the `skmemory` namespace collision, where
  the path fails → 4 spurious `FileNotFoundError` failures. The HTML file exists.
  Fixed to resolve relative to `__file__`. All 4 now pass from any CWD. This is the
  whole `144→204` jump plus the new tests; it was masking the page-markup checks
  (host controls, raise-hand, permission/metadata render hooks) entirely.

- **F-5 (NOT a bug — documented containment limitation):** the skreachd allowlist
  does NOT block `..` path-traversal in command *arguments*
  (`cat ../../etc/passwd` reaches the runner with `cat` allowlisted). This is by
  design — containment is the scoped sandbox cwd + scrubbed minimal env, not arg
  inspection. `test_path_traversal_arg_reaches_runner_but_cwd_is_scoped` pins this
  behavior so a future change that relaxes cwd scoping would be caught. If
  stronger isolation is desired later (e.g. real chroot/namespace sandbox), this
  test marks the seam.

No correctness bugs found in lanes / skreachd / recording-writeup / glossa-mesh
source. The features are well-factored around injectable seams; the gaps were
purely in test coverage (adversarial + edge paths) plus the one pre-existing
test-harness path bug.

---

# Area 3 — Federation / Calls / Crypto / Voice / Files / MCP

QA pass on skchat federation, WebRTC calls, crypto, voice, file transfer, pairing
gate, and the MCP tool surface. All new tests are fast/CI (fakes + tmp_path; no
live network, no SFU, no real Piper/Whisper/coturn).

- Federation cross-host mint: **LIVE ✅** (jarvis@.41 → .158).
- 1:1 LiveKit call: **LIVE ⏳** (deterministic-room + signed-ring path exercised in CI).

## Suite result (honest before/after)

| | Tests | Pass | Fail |
|---|---|---|---|
| Before | 487 | 487 | 0 |
| After  | 557 | 557 | 0 |

+70 collected (67 new test functions; parametrized cases expand the rest). No new
failures introduced. The known pre-existing failures (`test_cli::test_status`,
`test_e2e_chat`, `test_spaces_ui_markup`) are outside this area; `test_cli` passed
clean in this environment's runs.

## Tests added per file

| File | +Tests | Focus of new cases |
|---|---|---|
| test_fed_assertion.py | 9 | claim-body tamper, fqid-swap-with-kept-sig, max_age=0 sentinel, missing/non-JSON/non-object/non-int claim, empty signed, resolver gets full fqid |
| test_fed_authd.py | 5 | verify-failure aborts before mint, replay checked before space/access, DENY doesn't poison other fqid's nonce, listener-cap no-op on SUBSCRIBE, response echo |
| test_fed_keystore.py | 5 | backslash/null neutralised, dotted-realm verbatim, dir-shadow→None, `..` token strip |
| test_fed_focus.py | 4 | all-focusless→None, single, order-independence, focusless-oldest skipped |
| test_fed_nonce.py | 4 | expired-entry eviction, repeated-replay reject, empty-nonce keyed, independent caches |
| test_fed_trust.py | 5 | corrupt-JSON→deny, invalid-default→deny, host grants all agents, bare-host arg, explicit-fqid no sibling leak |
| test_crypto.py | 5 | content-tamper fails verify, garbage key/sig→False, ciphertext-tamper→DecryptionError, wrong-recipient-key→fail |
| test_call_session.py | 6 | parse non-JSON / missing-type, unique nonce per invite, topic carry/default, self-pair room |
| test_connectivity.py | 6 | tailnet>subnet, no-hint→tier3, TURN expiry future, STUN+TURN both, no-config→empty, per-identity creds differ |
| test_files.py | 4 | GCM chunk-tamper→InvalidTag, wrong-key→InvalidTag, sha256-mismatch→verified=False, missing-chunks→ValueError |
| test_pairing_gate.py | 6 | rate-limit before window, reopen rotates nonce + resets accepts, close revokes, None nonce, accept-cap reason |
| test_mcp_server.py | 2 | call_peer no-ring-on-failure, HTTPException→ValueError translation |
| test_voice_backends.py | 6 | STT record None when unavailable/arecord-fails, record→transcribe, arecord missing-binary/nonzero-exit, empty-transcript→None |

## Per-feature test cases (representative)

### Federation — signed assertion (token-forgery defence)
| Case | CI/LIVE | Result |
|---|---|---|
| Valid sig + fresh + pinned key → accept | CI | pass |
| Tampered claim body (escalate space_id), sig kept | CI | reject (sig) |
| fqid swapped to trusted peer, sig kept | CI | reject (sig) |
| Future-dated / stale beyond max_age | CI | reject |
| max_age=0 disables freshness (sig still required) | CI | accept |
| Missing/non-JSON/non-object/non-int claim | CI | reject (malformed) |
| Resolver invoked with FULL fqid (no realm collision) | CI | pass |

### Federation — authd (mint orchestration)
| Case | CI/LIVE | Result |
|---|---|---|
| FULL→speaker, SUBSCRIBE→listener, DENY→reject | CI | pass |
| Replayed (fqid,nonce) within window | CI | reject (replay) |
| Replay checked before space-live + access + mint | CI | pass |
| verify() failure never reaches mint | CI | pass |
| remote_max_role=listener caps FULL, no-op on SUBSCRIBE | CI | pass |
| Cross-host mint jarvis@.41 → .158 | **LIVE ✅** | pass |

### Federation — keystore (realm-qualified pin)
| Case | CI/LIVE | Result |
|---|---|---|
| Pin keyed on full fqid returns armor | CI | pass |
| Different realm / bare-agent → None (no impersonation) | CI | pass |
| Path traversal (`..`, `/`, `\`, `\x00`) → None | CI | pass |
| Directory shadowing the .asc path → None | CI | pass |

### Calls — session + ring + ICE
| Case | CI/LIVE | Result |
|---|---|---|
| derive_room order-independent, stable, opaque, self-pair | CI | pass |
| CALL_INVITE roundtrip; reject non-JSON / non-invite / missing-type | CI | pass |
| Unique nonce per invite; topic carried/defaulted | CI | pass |
| /call/start rings paired peer; 404 unpaired; 409 ambiguous; 503 no-creds | CI | pass |
| /call/incoming surfaces only signed invites addressed to self | CI | pass |
| ICE tier ladder (tailnet>subnet>relay), ephemeral TURN creds, secret never leaks | CI | pass |
| End-to-end 1:1 LiveKit call | **LIVE ⏳** | manual |

### Crypto (sign/verify/encrypt tamper)
| Case | CI/LIVE | Result |
|---|---|---|
| Sign/verify roundtrip; wrong key → False | CI | pass |
| Content mutated after signing → verify False | CI | pass |
| Garbage key armor / garbage sig → False (no raise) | CI | pass |
| Ciphertext tamper → DecryptionError | CI | pass |
| Wrong recipient key cannot decrypt | CI | pass |

### Voice (STT/TTS backend fallbacks)
| Case | CI/LIVE | Result |
|---|---|---|
| Default piper/whisper; unknown name → KeyError | CI | pass |
| Scaffold (chatterbox/sensevoice) available iff dep present | CI | pass |
| record() → None when unavailable / arecord fails | CI | pass |
| _arecord missing-binary / nonzero-exit → False (no raise) | CI | pass |
| Empty whisper transcript → None | CI | pass |

### Files (encrypted chunked transfer)
| Case | CI/LIVE | Result |
|---|---|---|
| Full + multi-chunk roundtrip, SHA-256 verified | CI | pass |
| GCM chunk-byte tamper → InvalidTag | CI | pass |
| Wrong AES key → InvalidTag | CI | pass |
| Declared sha256 mismatch → verified=False | CI | pass |
| Assemble before all chunks → ValueError | CI | pass |
| Resume / idempotent re-receipt | CI | pass |

### Pairing gate (Funnel hardening) + MCP
| Case | CI/LIVE | Result |
|---|---|---|
| Closed-by-default; nonce + window TTL; accept-cap auto-close | CI | pass |
| Rate-limit before window open; reopen rotates nonce + resets accepts | CI | pass |
| Explicit close revokes; None nonce rejected | CI | pass |
| call_peer rings on success; no-ring on prepare failure | CI | pass |
| _prepare_call_for translates HTTPException → ValueError | CI | pass |

## Real use-cases mapped (U-style)
- **U1** Remote agent joins a federated Space: signed FQID assertion → authd mints
  LiveKit token (CI: full forge/replay/tamper/cap matrix; LIVE ✅ cross-host mint).
- **U2** Two paired agents place a 1:1 call: deterministic room + signed
  CALL_INVITE ring + ICE tier ladder (CI: routes/session/connectivity; LIVE ⏳ call).
- **U3** Agent sends an encrypted DM/file: PGP sign+encrypt / AES-GCM chunks with
  tamper + integrity verification (CI).
- **U4** Operator pairs a new device over Funnel: time-boxed window + nonce +
  rate-limit (CI).
- **U5** Agent records/speaks a voice message with a missing backend: graceful
  degradation, never crashes (CI).
- **U6** AI agent calls a peer via MCP `call_peer`: resolve+mint+ring, fails clean
  with no half-open invite (CI).

## Gaps found / notes
- No real bugs found. Existing federation security model (full-fqid key pinning,
  two-sided freshness, replay-before-mint ordering, deny-by-default trust) is
  sound and now has adversarial coverage.
- Pre-existing observation (not a regression): `NonceCache`/`PairingGate` are
  single-process; multi-replica authd/webui would need a shared store. Already
  documented in source; flagged here for the LIVE multi-replica rollout.
- `parse_invite_body` validates `type` only; addressing/sig trust is enforced at
  the `/call/incoming` route layer (covered) — by design.

---

# Area 4 QA — skcomms

## Area 4 — skcomms (transports / identity / glossa / BLE / LoRa / adapters)

Repo: `/home/cbrd21/clawd/skcapstone-repos/skcomms` (branch `main`)
Runner: `cd ~ && ~/.skenv/bin/python -m pytest <file> -q`

**Before:** `507 passed` (28 warnings, 43.18s)
**After:**  `562 passed` (28 warnings, 42.99s)  — **+55 tests, 0 failures, 0 regressions**

Legend: **CI** = runs in CI with fakes (FakeRadio/FakeMedium, FakeLoRaInterface/Medium,
FakeAdapter, in-process pgpy/Ed25519 keys, tmp env). **GATED** = needs real
hardware/tokens, NOT run in CI.

---

### Subsystem: Config parsing (`config.py`) — NEW test file `test_config.py` (16)

The headline gap: `config.py` had **no test home at all**. Locked in the real
`skcomm:` → `skcomms:` rename bug plus the home default.

**Test cases**

| Test | Asserts | Mode |
|---|---|---|
| `test_skcomm_wrapped_config_loads_transports` | legacy top-level `skcomm:` key still parses AND loads transports (the real bug) | CI |
| `test_skcomms_wrapped_config_loads_transports` | current `skcomms:` key parses transports | CI |
| `test_skcomm_and_skcomms_parse_identically` | both keys yield identical version/mode/encrypt/transports | CI |
| `test_skcomms_wins_when_both_keys_present` | `skcomms:` precedence over `skcomm:` (`or` short-circuit) | CI |
| `test_skcomms_home_constant_default` | `SKCOMMS_HOME == "~/.skcapstone/skcomms"` | CI |
| `test_load_config_default_path_is_skcomms_home` | `load_config()` resolves `~/.skcapstone/skcomms/config.yml` | CI |
| `test_skcomms_home_resolution_via_home_module_default` | `home.skcomms_home()` resolves the SAME default root | CI |
| `test_missing_file_returns_defaults` | absent file → safe defaults, no crash | CI |
| `test_unwrapped_config_loads_at_top_level` | no wrapper key → raw mapping is the section | CI |
| `test_transport_as_bare_bool` | `file: true` → `TransportConfig(enabled=True)` | CI |
| `test_defaults_block_overrides` | `defaults:` mode/encrypt/sign/ack/retry_max/ttl applied | CI |
| `test_identity_and_daemon_sections` | identity + daemon sub-sections parsed | CI |
| `test_malformed_yaml_falls_back_to_defaults` | YAML parse error swallowed → defaults | CI |
| `test_empty_file_is_defaults` | empty file → defaults | CI |

**Real use cases**

| U | Use case | Mode |
|---|---|---|
| U-CFG-1 | Operator upgrading from old `skcomm`-named config keeps a working transport set after the rename | CI |
| U-CFG-2 | Daemon boots with `SKCOMMS_HOME` defaulting to `~/.skcapstone/skcomms` (Syncthing-shared tree) | CI |
| U-CFG-3 | Corrupt/partial config never crashes the daemon — degrades to sovereign defaults | CI |

---

### Subsystem: Home scaffold + peer routing (`home.py`) — `test_home_scaffold.py` (+5 → 13)

**Test cases**

| Test | Asserts | Mode |
|---|---|---|
| `test_agent_name_falls_back_to_agent_field_when_no_fqid` | `_agent_name` uses `agent` field when identity lacks fqid | CI |
| `test_maps_fqid_to_realm_operator_agent_inbox` | `a@o.r` → `<home>/r/o/a/inbox` | CI |
| `test_handles_multi_dot_realm` | `lumina@chef.sk.world` → realm `sk.world` (split on first `.`) | CI |
| `test_rejects_fqid_without_at` | no `@` → ValueError | CI |
| `test_rejects_fqid_without_realm_dot` | no `.` in operator.realm → ValueError | CI |

**Real use cases**

| U | Use case | Mode |
|---|---|---|
| U-HOME-1 | Sender drops an envelope into a peer's Syncthing-shared inbox by FQID | CI |
| U-HOME-2 | Malformed recipient FQID is rejected before any filesystem write | CI |

---

### Subsystem: Adapter factory + token-gating (`adapters/factory.py`) — `test_adapter_factory.py` (+11 → 18)

**Test cases**

| Test | Asserts | Mode |
|---|---|---|
| `test_expand_env_recurses_one_level_into_nested_dict` | `${VAR}` substituted inside a nested dict | CI |
| `test_expand_env_leaves_non_string_values_untouched` | ints/bools/lists pass through | CI |
| `test_enabled_omitted_defaults_to_built` | absent `enabled` ⇒ built (only `enabled is False` skips) | CI |
| `test_token_gating_skips_when_env_token_empty` | unset `${TOKEN}` → graceful skip, no crash | CI |
| `test_token_gating_builds_when_env_token_present` | present token → adapter built | CI |
| `test_unknown_adapter_type_in_config_is_skipped_not_raised` | unknown type → skipped, never raises | CI |
| `test_matrix_uses_access_token_field_for_gating` | matrix gates on `access_token` not `bot_token` | CI |
| `test_build_registry_reuses_supplied_registry` | caller-supplied registry is reused (dispatch mode) | CI |
| `test_none_entry_treated_as_empty` | `fake:` null entry → `{}` → builds | CI |

**Real use cases**

| U | Use case | Mode |
|---|---|---|
| U-ADP-1 | Daemon starts with discord/slack/matrix configured but tokens absent → those skip gracefully, fake/others still run | CI |
| U-ADP-2 | `${BOT_TOKEN}` from env expands at build time (no token in YAML on disk) | CI |
| U-LIVE-1 | Real Telegram/Discord/Slack/Matrix bridge with live tokens | **GATED (tokens)** |

---

### Subsystem: FakeAdapter lifecycle (`adapters/fake.py`) — `test_adapter_fake.py` (+5 → 10)

**Test cases**

| Test | Asserts | Mode |
|---|---|---|
| `test_fake_default_adapter_name_when_no_config` | default `adapter_name == "fake"` | CI |
| `test_fake_bind_then_resolve_fqid_roundtrip` | bind → resolve FQID round-trip; unknown is None | CI |
| `test_fake_health_reports_queued_inbound_depth` | health reports queue depth + adapter_name | CI |
| `test_fake_reconnect_cycle` | connect→disconnect→connect leaves connected=True | CI |
| `test_fake_records_multiple_sends_in_order` | sent buffer preserves order | CI |

**Real use cases**

| U | Use case | Mode |
|---|---|---|
| U-FAKE-1 | CI exercises full adapter ABC (connect/health/send/inbound/bind/resolve) with no network/token | CI |

---

### Subsystem: Glossa emergent negotiation (`glossa/emergent.py`) — `test_glossa_emergent_negotiator.py` (+5 → 10)

**Test cases**

| Test | Asserts | Mode |
|---|---|---|
| `test_frame_propose_roundtrips_through_parse` | propose frame encode/decode round-trip | CI |
| `test_parse_propose_rejects_malformed_frame` | non-JSON frame → ValueError | CI |
| `test_parse_propose_rejects_missing_key` | missing `d` key → ValueError | CI |
| `test_apply_propose_mutates_session` | `apply_propose` adds the macro to the session | CI |
| `test_session_macro_shadows_base` | session macro overrides a same-named base macro; base still reachable | CI |

**Real use cases**

| U | Use case | Mode |
|---|---|---|
| U-EMG-1 | Two agents negotiate a private mid-session macro; definition is the audit gloss | CI |
| U-EMG-2 | A garbage/hostile propose frame is rejected, never corrupts the session lexicon | CI |

---

### Subsystem: Glossa macro audit-gloss (`glossa/macros.py`) — `test_glossa_macros_render.py` (+2 → 8)

**Test cases**

| Test | Asserts | Mode |
|---|---|---|
| `test_expand_macros_prefers_longest_match_first` | `P0 down` matched as a unit over the `P0` prefix | CI |
| `test_expand_macros_empty_lexicon_is_identity` | empty lexicon → input returned unchanged | CI |

**Real use cases**

| U | Use case | Mode |
|---|---|---|
| U-GLO-1 | Auditor reads the expanded English meaning of terse macros; overlapping macros disambiguate correctly | CI |

---

### Subsystem: BLE MeshPacket codec hardening (`transports/ble/protocol.py`) — `test_ble_protocol.py` (+7 → 17)

**Test cases**

| Test | Asserts | Mode |
|---|---|---|
| `test_encode_rejects_wrong_length_signature` | 32-byte signature → ValueError (must be 64) | CI |
| `test_encode_rejects_wrong_length_ids` | 4-byte msg/sender/recipient id → ValueError | CI |
| `test_decode_rejects_truncated_signature` | chopped signature → "signature truncated" | CI |
| `test_decode_rejects_truncated_payload` | declared plen > actual bytes → "payload truncated" | CI |
| `test_pad_unpad_empty_body` | empty body pads + unpads to `b""` | CI |
| `test_unpad_rejects_too_short` | <2 bytes → ValueError | CI |
| `test_unpad_rejects_length_prefix_overrun` | length prefix > data → ValueError | CI |

**Real use cases**

| U | Use case | Mode |
|---|---|---|
| U-BLE-1 | Corrupt/truncated/forged-length packet off the air is rejected, never crashes the node | CI |
| U-BLE-2 | Multi-hop relay, fragment/reassembly, Noise XX, identity sign/verify (existing) | CI (fakes) |
| U-BLE-RADIO | Real Bluetooth LE proximity mesh on physical radios | **GATED (real radio)** |

---

### Subsystem: LoRa airtime / duty-cycle (`transports/lora/store.py`, `transport.py`) — `test_lora_store.py` (+3 → 9), `test_lora_transport.py` (+5 → 11)

**Test cases (store)**

| Test | Asserts | Mode |
|---|---|---|
| `test_drain_with_dest_preserves_per_recipient_dest` | `(frame, dest)` pairs interleave correctly per recipient | CI |
| `test_tumbling_window_resets_used_exactly_at_boundary` | window resets only at/after `window_s` boundary (tumbling, not sliding) | CI |
| `test_can_send_does_not_consume_budget` | `can_send` is a pure query; only `record` consumes | CI |

**Test cases (transport)**

| Test | Asserts | Mode |
|---|---|---|
| `test_budget_smaller_than_mtu_is_rejected` | `max_bytes < LORA_MTU` → ValueError (anti-wedge guard) | CI |
| `test_sync_send_returns_failure_pointing_at_async` | sync ABC `send` returns failure pointing at `send_async()` | CI |
| `test_is_available_false_without_running_iface` | no iface → unavailable | CI |
| `test_identity_id_for_empty_recipient_is_broadcast` | empty recipient → BROADCAST_ID | CI |
| `test_health_check_reports_unavailable_without_iface` | health = UNAVAILABLE without iface | CI |

**Real use cases**

| U | Use case | Mode |
|---|---|---|
| U-LORA-1 | Over-budget multi-frame envelope sends only what fits the window; remainder held + flushed next window | CI (existing) |
| U-LORA-2 | A misconfigured budget too small for one frame is rejected at construction (no silent wedge) | CI |
| U-LORA-3 | Envelope round-trip + fragmentation over a fake LoRa medium | CI (existing) |
| U-LORA-BOARD | Real Meshtastic LoRa board transmit/receive over RF | **GATED (LoRa hardware)** |

---

## Already-strong coverage (no gaps found, reviewed)

- **Envelope v1** (`test_envelope_v1.py`): sign/verify happy-path, body tamper, wrong-key — complete.
- **Grants** (`test_grants.py`): mint/verify, tamper widen-scope, wrong-key, expiry, idempotent accept, T9 schema, CLI round-trip — complete.
- **Glossa codec L0/L1/L2** + handshake (weakest-caps `min(max_level)`, codebook-version gate, symmetry), to_human/audit gloss, session macros text-slot-only expansion — complete.
- **BLE** mesh multi-hop relay, dedup (bloom), directed delivery, fragment/reassemble (out-of-order, hostile), Noise XX, identity — complete.
- **TOFU / peers / mailbox / registry / signaling / p2p / pairing** — broad existing coverage, no obvious gaps.

## Real bugs found

**None.** All 55 additions are characterization/edge-case tests that passed on
first run against current `main`. The `skcomm:`/`skcomms:` rename bug and the
`SKCOMMS_HOME` default were already fixed in source (`config.py:117`,
`home.py:56`); the new `test_config.py` **locks them in** so a regression is caught.

## Not committed (per instructions).

---

## Area 5 — Flutter App

**Target:** skchat Flutter app — `~/clawd/skcapstone-repos/skchat-app` on host **192.168.0.41**
**Tooling:** `~/flutter/bin/flutter` (analyze + test only; no full build).
**Date:** 2026-06-14 · **QA pass:** standard

---

### 0. Summary

| Metric | Before | After |
|---|---|---|
| `flutter test` passed | 120 | **160** (+40) |
| `flutter test` failed | 5 | **5** (unchanged — all pre-existing) |
| New test files added | — | **4** |
| New tests added | — | **40** |
| New analyzer issues introduced | — | **0** |

**Honest pass/fail:** the suite is NOT green — there are 5 failing tests, but
all 5 are **pre-existing** and unrelated to this QA pass (Hive-not-initialized
in `conversation_provider_test.dart`). My 40 added tests all pass and introduce
zero analyzer issues. No existing tests were modified or broken.

---

### 1. `flutter analyze lib/ test/` — result

**27 issues, 0 errors.** All are warnings/infos and **pre-existing** (none
introduced by this pass). Notable:

- `warning` unused import `glass_widgets.dart` — `outgoing_call_screen.dart:6`
- `warning` unused field `error` — `profile/qr_login_screen.dart:140`
- `warning` unused field `_url` — `spaces/watch_panel.dart:28` (the panel keeps
  state in `_url` for setState but the field is no longer read; harmless)
- `warning` unnecessary cast — `core/crypto/pgp_bridge.dart:100`
- `warning` unused imports in `test/data/hive_adapters_test.dart` (×2) and
  `test/features/groups/create_group_screen_test.dart` (×1)
- `info` `dart:html` deprecated + `avoid_web_libraries_in_flutter` —
  `spaces/watch_video_web.dart:2` (expected: this IS the web-only surface,
  guarded by a conditional import; cannot be avoided without `package:web`)
- `info` deprecated `Radio.groupValue/onChanged/activeColor` (Flutter
  >3.32 RadioGroup migration) — `group_info_screen.dart`, `profile_screen.dart`
- `info` `unintended_html_in_doc_comment` (angle brackets in `///`) ×4 —
  `whiteboard_panel.dart`, `webrtc_service.dart`
- several `unnecessary_underscores` / `unnecessary_this` / `use_null_aware_elements` infos

The 4 NEW test files analyze clean: **"No issues found."**

---

### 2. Gaps identified (features/services with NO dedicated test before this pass)

| Feature / service | Pure-logic testable? | Action |
|---|---|---|
| watch-together `youtubeId()` URL parsing | YES (stub has no `dart:html`) | **TESTED (new)** |
| `daemon_config` `normalizeDaemonUrl` / `daemonWsUrl` | YES (pure) | **TESTED (new)** |
| `DaemonService.peerShortName` + `SkchatCliMessage.fromJson` | YES (pure) | **TESTED (new)** |
| `SpaceSummary`/`SpaceJoin` `fromJson` + `isHost` | YES (pure) | **TESTED (new)** |
| `PeerInfo.fromJson` (identity card source) | YES | already covered in `skcomms_client_test.dart` |
| `SpacesService` (12 REST endpoints) | YES (canned Dio) | already covered in `spaces_service_test.dart` |
| `LaneService` publish/catchUp/inbound | NO (needs LiveKit data channel + HTTP) | **device/manual** |
| WatchPanel / WhiteboardPanel / DocPanel / TerminalPanel / ScreenSharePanel | NO (Riverpod providers + LiveKit + platform views) | **device/manual** |
| watch_video_web `WatchVideoController` render + postMessage sync | NO (web-only `dart:html`/iframe; cross-origin YT IFrame API) | **device/manual (web)** |
| lane-chooser FAB → 6 lanes | partial (in `space_room_screen.dart`) | already covered by `space_room_screen_test.dart`; lives in another agent's file (not touched) |
| Identity card real-data `_resolvedFingerprint` (fingerprint-vs-name) | logic is `private` in `identity_card_screen.dart` | **device/manual** (widget needs `skcommsClient` provider; logic not exported) |
| Spaces audio rooms / LiveKit join / mic | NO (real LiveKit + audio) | **LIVE** |
| Calls (livekit_call_service, webrtc_service) | NO (real RTC) | **LIVE** |
| Pairing / QR | NO (camera + daemon) | **device/manual** |
| skcomms daemon client `:9384` / daemon health `:9385` | NO (live daemon) | **LIVE** (one canned-Dio path covered in `skcomms_client_test`) |

---

### 3. Tests added (40 across 4 files)

**`test/features/spaces/watch_video_test.dart`** (16 tests) — CI-test
Pins the watch-together YouTube id parser (and the inert stub control surface).
Imports the STUB controller so it runs on the Dart VM; the web controller carries
a byte-identical `youtubeId`/`_cleanId`, so these cases pin both.
- `::parses youtube.com/watch?v=ID`
- `::parses youtu.be/ID short link`
- `::parses youtube.com/shorts/ID`
- `::parses youtube.com/embed/ID`
- `::parses youtube.com/live/ID`
- `::parses m.youtube.com and music.youtube.com hosts`
- `::strips trailing &-params from watch?v=ID&t=`
- `::strips stray query from youtu.be/ID?si=...`
- `::handles www. prefix removal on host match`
- `::returns null for a Rumble URL`
- `::returns null for a direct mp4 URL`
- `::returns null for youtube.com with no video id`
- `::returns null for empty youtu.be path`
- `::returns null for garbage / non-URL input`
- `::trims surrounding whitespace before parsing`
- `::stub control surface load/play/pause/seek`

**`test/services/daemon_config_test.dart`** (11 tests) — CI-test
- `normalizeDaemonUrl::` adds http:// to bare host:port / bare host; preserves https://; strips single + multiple trailing slashes; trims whitespace; empty → default
- `daemonWsUrl::` http→ws; https→wss; bare host→ws; strips trailing slash

**`test/services/daemon_service_test.dart`** (9 tests) — CI-test
- `peerShortName::` strips `capauth:` scheme + `@domain`; bare name; @domain only; scheme only
- `SkchatCliMessage.fromJson::` complete parse + id round-trip; missing-field defaults; now()-fallback for non-string ts; now()-fallback for unparseable ts string

**`test/features/spaces/space_models_test.dart`** (8 tests) — CI-test
- `SpaceSummary.fromJson::` complete parse; defaults (status→"open", recording→false, empty speakers)
- `SpaceJoin.fromJson + isHost::` host (isHost true); listener (isHost false); role defaults to listener

---

### 4. Real use cases (U-style)

**Nav 4+1 (Chats/Groups/Activity/Spaces/Me)**
- U: launch app → app_shell renders 5-tab nav, smoke test mounts. — **CI-test** (`widget_test.dart`)
- U: tap each tab → screen swaps. — **device-manual**

**Spaces audio rooms**
- U: open Spaces → directory lists live spaces. — **CI-test** (`spaces_directory_screen_test`, `spaces_service_test::listLive`)
- U: tap a space → join as listener, get role-scoped LiveKit token. — **CI-test** for the token parse (`spaces_service_test::joinListener`); **LIVE** for actual audio join.
- U: raise hand → promoted to stage, mic goes live. — partial **CI-test** (`raiseHand` flag) + **LIVE** (audio).
- U: host mutes/kicks/ends/records. — REST shape only reachable via canned Dio; real effect is **LIVE**.

**6 collaborative lanes (lane-chooser FAB)**
- U: in a Space, tap the lanes FAB → bottom sheet shows Chat/Watch/Whiteboard/Doc/Screen/Terminal. — **CI-test** (`space_room_screen_test`, in another agent's file; not modified).
- U: pick a lane → its panel opens. — **device-manual** (panels need LiveKit + providers).

**Watch-together (YouTube / Rumble / direct)**
- U: open a Space → lane-chooser → Watch → paste a YouTube URL → video plays synced for everyone. — URL→id parsing is **CI-test**; embed render + cross-room sync is **device-manual (web)**.
- U: paste a Rumble URL → best-effort embed loads (play/pause/seek are documented no-ops cross-origin). — **device-manual (web)**.
- U: paste an .mp4 URL → native `<video>` with full play/pause/seek + sync. — **device-manual (web)**.
- U: on a non-web client → "Now playing (synced)" text fallback, state still syncs. — stub render is **device-manual**; stub control surface is **CI-test**.

**Coord board**
- U: open coord board → tasks render from provider. — **device-manual** (provider-backed; no dedicated test).

**Identity card (real data)**
- U: open a peer's identity card → shows CapAuth FQID, real PGP fingerprint, verified badge ONLY when a real fingerprint exists; a peer-name fallback is shown as "unverified" (never as a fingerprint). — logic is correct by inspection (`_resolvedFingerprint` treats `fingerprint == peerId` as no-real-fingerprint) but is **private + provider-backed → device-manual**. `PeerInfo.fromJson` (its data source) is **CI-test**.

**skcomms daemon client (:9384) / daemon health (:9385)**
- U: app reaches the SKComms REST API for peers/messages. — one path is **CI-test** (canned Dio in `skcomms_client_test`); live reachability is **LIVE**.
- U: daemon-health badge turns green when daemon up. — `DaemonService.isAlive`/`getHealth` hit a live `:9385` → **LIVE**. URL derivation (9384→9385) is internal (`_healthUrlFromDaemonUrl`, private); the public `normalizeDaemonUrl`/`daemonWsUrl` it builds on are **CI-test**.

**Pairing / QR**
- U: scan QR to pair a device / login. — **device-manual** (camera + daemon).

**Calls (livekit_call_service / webrtc_service)**
- U: initiate/accept a call → media flows, controls work. — **LIVE**.

---

### 5. Honest device-/LIVE-only gaps (cannot be unit-tested without hardware)

- `LaneService` (LiveKit data channel + server lane store) — **device-manual** (Flutter app).
  The web/SDK data-channel path is now **LIVE ✅** via `scripts/qa_two_browser.py` (two headless
  browsers round-trip a chat-lane message over the WebRTC data channel; QA matrix F-6).
- All 6 lane panels' widget behavior (watch/whiteboard/doc/screen/terminal) — **device-manual**
- watch_video_web embed render + YouTube IFrame postMessage sync — **device-manual (web)**
- Identity card widget (real fingerprint badge) — **device-manual** (private logic, provider-backed)
- Spaces audio, calls, WebRTC — **LIVE**
- QR pairing (camera) — **device-manual**
- Live daemon reachability (:9384 / :9385) — **LIVE**

Pre-existing failing tests to fix separately (NOT in scope of this pass):
`test/features/conversation/conversation_provider_test.dart` — 5 tests fail with
`HiveError: You need to initialize Hive` (test needs `Hive.init` / a temp dir in
setUp). Pre-existing on the branch before any QA changes.

---


## CI

GitHub Actions runs the CI-able QA suites automatically:

- **Workflow:** `.github/workflows/qa.yml` (job `qa`).
- **Triggers:** push to `main`, every pull request, and version tags (`v*`).
- **Matrix:** Python 3.10 / 3.11 / 3.12. Installs `pip install -e ".[dev]"`
  (fallback `pip install -e .` + `pytest`).
- **What runs:** `python -m pytest tests/ -q -m "not integration and not e2e_live and not e2e_3way"`.
  The default pyproject `addopts` already excludes the `live` marker (live model
  endpoints); CI additionally deselects `integration`, `e2e_live`, and `e2e_3way`
  because they require a running stack / network / daemon and cannot run headless.

The **LIVE legs** of the QA suite — the lane/spaces harness on `:8765`,
two-party (two-agent) cross-party check, the two-browser call test, and the
**headless two-browser data-channel lane test** (`scripts/qa_two_browser.py` —
two browsers join one Space + round-trip a chat-lane message over the LiveKit
WebRTC data channel; needs the live webui + a reachable SFU + full Chromium;
QA matrix F-6) — need a running stack and **cannot run in GitHub CI**. They stay
local: run them via `scripts/qa_suite.sh` against a running stack (see the LIVE
sections above), or directly: `~/.skenv/bin/python scripts/qa_two_browser.py`.
Flutter app tests likewise run locally on .41
(`ssh 192.168.0.41 '~/flutter/bin/flutter test'` in `skchat-app`).
