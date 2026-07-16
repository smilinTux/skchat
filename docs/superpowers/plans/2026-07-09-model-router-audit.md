# Model Router Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two independently-broken model-routing paths — (1) skchat's `GroupResponder` currently sends every group/DM reply to a model id (`reg:ornith`) that skgateway's registry proxy does not recognize as registry-routed, so it silently falls through to raw passthrough instead of registry resolution; (2) skcapstone's `ModelRouter` tag-rules never fire for 4 of its 5 real callers because the caller-supplied tags don't intersect any rule's keyword set, so every one of those calls silently falls through to the token-count fallback instead of the tier the caller intended.

**Architecture:** Two independent, separately-testable fixes in two different packages/repos. No shared code path between them — skchat's `GroupResponder.generate()` POSTs directly to skgateway's OpenAI-compatible `/v1/chat/completions` HTTP endpoint (bypassing skcapstone's `ModelRouter` entirely); skcapstone's `ModelRouter` is a pure in-process Python tag-matcher used by `emotion_tracker.py`, `context_window.py`, `memory_compressor.py`, `conversation_summarizer.py` (skcapstone's own `advocacy.py` sibling in skchat also uses it, unaffected by fix #1). Each fix is a single-file, additive, backward-compatible change with its own failing→passing test cycle and its own commit.

**Tech Stack:** Python 3.11/3.12, pytest, pydantic v2, skgateway (Node/`.mjs`, not modified by this plan — read-only verification only).

## Global Constraints

- Run all Python/pytest commands from `~` (`cd ~`), never from inside a repo checkout — `smilintux-org`/repo-local `skmemory/` shadows the installed `skmemory` package (documented namespace-collision gotcha in both repos' CLAUDE.md).
- skchat tests: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/clawd/skcapstone-repos/skchat/tests/ -q`
- skcapstone tests: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/clawd/skcapstone-repos/skcapstone/tests/ -q`
- **Known pre-existing baseline (unrelated to this audit):** `test_model_router.py::TestModelNameResolution::test_default_fast_model` and `::test_default_local_model` already fail on `main` (expect stale model name `"llama3.2"`, actual default is `"qwen3.5:4b"`). This plan's tag-rule changes touch `tag_rules` only, never `tier_models`, so this pre-existing pair must remain exactly as-is (still 2 failing, not more, not fewer) after Task 3 — do not "fix" them as a drive-by; they are out of scope.
- Scope is **exactly** the 3 items approved by Chef: (1) skchat responder → route through a registry-recognized role instead of `reg:ornith`; (2) skcapstone `ModelRouter` tag-rules made to actually fire for real callers; (3) verify alignment with `~/.skcapstone/models/registry.yaml`. This is NOT the full 13-mechanism model-routing consolidation — do not touch skgateway source, do not touch `registry.yaml`, do not touch `advocacy.py`'s routing path.
- **Finding, not a fix, for scope item (3):** `~/.skcapstone/models/registry.yaml` already defines `roles.sk-default: ornith` and `defaults.role: sk-auto` correctly — verified live (Task 2). No registry.yaml edit is needed; item (3) closes via the verification step in Task 2, not a code change.
- **Decision — why `sk-default` and not `reg:ornith` or `sk-auto`:** `skchat/docs/superpowers/specs/2026-07-07-skchat-async-generation-design.md:139` already names the intended fix as "`reg:ornith` → `sk-default`" from a prior design pass — this plan implements that already-agreed target rather than introducing a new one. `sk-auto` (difficulty-routing + semantic cache + empirical adjuster) is a reasonable future enhancement but is a behavior change beyond "fix the broken route"; leave it as a documented follow-up (`SKCHAT_GROUP_MODEL=sk-auto` env override already works today with zero code change, for whoever wants to pilot it).
- **Decision — `advocacy.py` left alone:** `skchat/src/skchat/advocacy.py:_call_consciousness` already routes through skcapstone's `ModelRouter`/`LLMBridge` (a *different* mechanism from `GroupResponder`'s direct skgateway POST) and is currently disabled in production via `SKCHAT_ADVOCACY_DISABLED` (referenced `skchat/src/skchat/daemon.py:310`). Unifying the two responder mechanisms is out of scope for this audit; note it as a follow-on if/when advocacy.py is re-enabled.
- **Decision — keyword-broadening over caller-fix:** For the `ModelRouter` tag-rule mismatch, broaden the FAST tier's rule keyword set in `skcapstone/src/skcapstone/model_router.py` (one file) rather than editing tag lists in 4 separate caller modules across the codebase. Evidence below shows every mismatching caller is a cheap/background task (sentiment classification, context-window compression, conversation summarization, memory compression) that *should* land on the cheapest tier anyway — broadening is the minimal, lowest-blast-radius fix that also happens to be the semantically correct one.

---

## Evidence (verified against the actual code on disk before writing this plan)

**Item 1 — skchat `reg:ornith` is a dead end:**
- `skchat/src/skchat/group_responder.py:20`: `_DEFAULT_MODEL = "reg:ornith"`.
- `skgateway/src/proxy/registry.mjs:111-113` (`isRegistryRouted`): a request is registry-routed only if `context`/`service`/`role` headers are set, **or** `model` is a string starting with `"sk-"`. `"reg:ornith"` matches none of those — it is NOT registry-routed, so it bypasses `resolve()` entirely (raw passthrough to whatever `model` string is on the wire, no role indirection, no fallback).
- Live-verified `sk-default` DOES resolve through the registry (ran 2026-07-09):
  ```
  curl -s http://localhost:18780/v1/chat/completions -H 'content-type: application/json' \
    -d '{"model":"sk-default","messages":[{"role":"user","content":"ping"}],"max_tokens":5}'
  ```
  returned a real completion with `"model":"ornith-1.0-9b"` in the response body — proving `sk-default` → `roles.sk-default: ornith` → the `ornith` backend, end to end.

**Item 2 — `ModelRouter` tag-rules don't match real callers:**
`skcapstone/src/skcapstone/model_router.py:120-153` (`ModelRouterConfig.default().tag_rules`) has 4 rules:
| Tier | Keywords |
|------|----------|
| CODE | code, refactor, debug, test, implement |
| REASON | architecture, design, analyze, research, plan |
| NUANCE | marketing, creative, email, copy, comms, writing |
| FAST | format, rename, lint, simple, trivial |

Real callers and the tags they actually pass (`TagRule._best_tag_rule` requires set-intersection — zero overlap = no match = falls through to the token-count fallback, `model_router.py:213-233`):
- `skcapstone/src/skcapstone/emotion_tracker.py:333`: `tags=["classification", "fast"]` — no overlap with any rule.
- `skcapstone/src/skcapstone/context_window.py:307`: `tags=["summary", "context"]` — no overlap.
- `skcapstone/src/skcapstone/conversation_summarizer.py:140`: `tags=["summary", "conversation"]` — no overlap.
- `skcapstone/src/skcapstone/memory_compressor.py:357`: `tags=["compression", "memory", tag]` — no overlap (`tag` is a dynamic group label, e.g. `group.tag`, can't be enumerated).
- `skcapstone/src/skcapstone/consciousness_loop.py:_classify_message` (line ~1194) tags are `"code"`/`"analyze"`/`"creative"`/`"simple"`/`"general"` — these **already match** the existing CODE/REASON/NUANCE/FAST rules correctly. This caller is NOT part of the bug and needs no change (confirmed by reading `_CODE_KEYWORDS`/`_REASON_KEYWORDS`/`_NUANCE_KEYWORDS`/`_SIMPLE_KEYWORDS` at `consciousness_loop.py:1155-1163`).

---

### Task 1: skchat — route `GroupResponder` through `sk-default` instead of dead `reg:ornith`

**Files:**
- Modify: `skchat/src/skchat/group_responder.py:5` (docstring), `:20` (`_DEFAULT_MODEL` constant)
- Modify: `skchat/tests/test_group_responder.py:13`, `:68` (assertions encoding the old default)
- Modify: `skchat/docs/WEBAPP-AND-API-ARCHITECTURE.md:212` (architecture doc default mention)
- Test: `skchat/tests/test_group_responder.py`

**Interfaces:**
- Consumes: nothing new — `GroupResponderConfig.model` (existing field, `str`), `load_group_config(agent, env)` (existing function), `generate(messages, cfg, http=None)` (existing function) are unchanged in shape.
- Produces: `GroupResponderConfig.model` now defaults to `"sk-default"` when `SKCHAT_GROUP_MODEL` is unset. No other module depends on the literal string value, so no downstream signature changes.

- [ ] **Step 1: Update the two existing tests to encode the new expected default (this is the "failing test" step — these assertions currently pass against the OLD value, so first prove they fail against the NEW value you're about to introduce)**

Edit `skchat/tests/test_group_responder.py` line 13:
```python
    assert cfg.model == "sk-default"
```
(was `assert cfg.model == "reg:ornith"`)

Edit `skchat/tests/test_group_responder.py` line 68:
```python
    assert payload["model"] == "sk-default"
```
(was `assert payload["model"] == "reg:ornith"`)

- [ ] **Step 2: Run the tests to verify they now fail against the unmodified source**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/clawd/skcapstone-repos/skchat/tests/test_group_responder.py -q -k "config_defaults_for_lumina or generate_ok"`

Expected: 2 FAILED — `assert 'reg:ornith' == 'sk-default'` in both.

- [ ] **Step 3: Flip the default in source**

Edit `skchat/src/skchat/group_responder.py` line 20:
```python
_DEFAULT_MODEL = "sk-default"
```
(was `_DEFAULT_MODEL = "reg:ornith"`)

Edit `skchat/src/skchat/group_responder.py` line 5 (module docstring), replace:
```
skgateway (reg:ornith), and return the reply. Talk-first (no tool-loop).
```
with:
```
skgateway (role sk-default — registry-routed; see registry.yaml roles.sk-default),
and return the reply. Talk-first (no tool-loop).
```

- [ ] **Step 4: Run the full skchat test file to verify it now passes with no other regressions**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/clawd/skcapstone-repos/skchat/tests/test_group_responder.py -q`

Expected: `13 passed` (all tests in the file, including the two you edited).

- [ ] **Step 5: Update the architecture doc mention (no test — doc-only, folds into this task's deliverable)**

Edit `skchat/docs/WEBAPP-AND-API-ARCHITECTURE.md` line 212, replace:
```
  │  (SKCHAT_GROUP_BACKEND_URL, default reg:ornith @ :18780).
```
with:
```
  │  (SKCHAT_GROUP_BACKEND_URL, default role sk-default @ :18780 — registry-routed).
```

- [ ] **Step 6: Run the full skchat suite once more to confirm no other file references the old default**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/clawd/skcapstone-repos/skchat/tests/ -q`

Expected: same pass/fail counts as the pre-task baseline, plus the 2 tests from Step 1 now passing (run `git stash && cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/clawd/skcapstone-repos/skchat/tests/ -q; git stash pop` first if you want an exact before/after diff).

- [ ] **Step 7: Commit**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skchat
git add src/skchat/group_responder.py tests/test_group_responder.py docs/WEBAPP-AND-API-ARCHITECTURE.md
git commit -m "fix(group_responder): route via sk-default role instead of dead reg:ornith model id

reg:ornith never matched skgateway's isRegistryRouted() (only 'sk-' prefixed
models or context/service/role headers are registry-routed), so every group
and DM reply silently bypassed registry resolution. sk-default -> ornith is
live-verified end to end."
```

---

### Task 2: Cross-package live verification — skgateway registry routing + registry.yaml alignment (no code change)

**Files:** none modified — verification only, plus one restart.

**Interfaces:** N/A (operational verification step).

- [ ] **Step 1: Confirm skgateway is up and `sk-default` resolves through the registry (re-run the same probe used to ground this plan)**

Run:
```bash
curl -s http://localhost:18780/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"sk-default","messages":[{"role":"user","content":"ping"}],"max_tokens":5}'
```
Expected: HTTP 200 JSON body with a `choices[0].message.content` string and `"model":"ornith-1.0-9b"` — proves `sk-default` → `roles.sk-default: ornith` → the live `ornith` backend at `192.168.0.100:8082`.

- [ ] **Step 2: Confirm `registry.yaml` needs no edit for this audit's scope item (3)**

Run: `grep -n "sk-default\|defaults:" -A1 ~/.skcapstone/models/registry.yaml`

Expected output includes:
```
  sk-default: ornith
...
defaults:
  role: sk-auto
```
This confirms `sk-default` is already a valid, correctly-wired role — no registry change needed. (If this ever drifts — e.g. `sk-default` gets removed or repointed — that's a registry-owner change, not a skchat/skcapstone code change; re-run Step 1 to re-verify before touching either package again.)

- [ ] **Step 3: Restart the skchat daemon so the code from Task 1 is live (editable install — code is live on next process start, but the long-running systemd daemon holds the old module in memory until restarted)**

Run:
```bash
systemctl --user restart skchat-daemon.service
systemctl --user status skchat-daemon.service
```
Expected: `active (running)`, fresh `MainPID`.

- [ ] **Step 4: Send a real `@mention` in a live group and confirm a reply lands (manual smoke test, not automated — this closes the loop end-to-end)**

Send a group message containing `@lumina` (or whichever agent's daemon you restarted) from another identity (e.g. `chef@skworld.io`), then:
```bash
journalctl --user -u skchat-daemon -n 30 --no-pager | grep -i "group generate\|skgateway"
```
Expected: no `"group generate: skgateway HTTP"` warning lines, and the reply is visible via `skchat inbox` or the group history — confirms `sk-default` is actually serving live traffic, not just the curl probe.

- [ ] **Step 5: No commit for this task** (verification-only; if Step 1 or Step 2 had failed, this task would instead produce a bug report / halt the plan rather than a commit).

---

### Task 3: skcapstone — make `ModelRouter` tag-rules actually fire for real callers

**Files:**
- Modify: `skcapstone/src/skcapstone/model_router.py:149-153` (FAST `TagRule` keywords)
- Test: `skcapstone/tests/test_model_router.py` (new test class, appended)

**Interfaces:**
- Consumes: existing `ModelRouter`, `ModelRouterConfig`, `TagRule`, `TaskSignal` (all unchanged signatures — see `model_router.py:31-155`).
- Produces: `ModelRouterConfig.default()`'s FAST `TagRule.keywords` now includes the exact tags used by `emotion_tracker.py`, `context_window.py`, `conversation_summarizer.py`, and `memory_compressor.py`. No other module needs to change — the fix is entirely inside `ModelRouterConfig.default()`.

- [ ] **Step 1: Establish the pre-existing baseline (so you can tell your change apart from the 2 known unrelated failures)**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/clawd/skcapstone-repos/skcapstone/tests/test_model_router.py -q`

Expected: `2 failed, 39 passed` — the 2 failures are `TestModelNameResolution::test_default_fast_model` and `::test_default_local_model` (stale `"llama3.2"` expectation vs actual `"qwen3.5:4b"`; unrelated to this task, do not touch).

- [ ] **Step 2: Write the failing tests — one per real caller's exact tag list, asserting the tier that caller actually needs (FAST — all four are cheap/background tasks)**

Append to `skcapstone/tests/test_model_router.py` (after the `TestTagRulePriority` class, before `TestConfigFromYaml`):

```python
# ---------------------------------------------------------------------------
# Caller-alignment: real production callers' exact tags must actually match
# a rule (regression coverage for the 2026-07-09 model-router audit — these
# tags previously fell through to the token-count fallback because no rule
# keyword overlapped them).
# ---------------------------------------------------------------------------


class TestRealCallerTagAlignment:
    """Each real ModelRouter caller's exact tag list must hit a tag rule.

    Evidence (verified 2026-07-09):
    - emotion_tracker.py:333            tags=["classification", "fast"]
    - context_window.py:307             tags=["summary", "context"]
    - conversation_summarizer.py:140    tags=["summary", "conversation"]
    - memory_compressor.py:357          tags=["compression", "memory", <dynamic tag>]
    All four are cheap background/housekeeping tasks and should land on FAST.
    """

    # NOTE: estimated_tokens is deliberately set to 20_000 (> the router's
    # 16_000 token-fallback threshold, model_router.py:_LARGE_TOKEN_THRESHOLD)
    # in every test below. With a SMALL token estimate, the pre-fix router
    # already "accidentally" lands on FAST via the token fallback (verified
    # by direct execution 2026-07-09), which would make a small-token test
    # pass before AND after the fix — proving nothing. A large token estimate
    # makes the bug visible: pre-fix it wrongly falls through to REASON
    # (token fallback), post-fix the tag rule correctly wins and returns FAST
    # regardless of token count. This also mirrors real usage —
    # memory_compressor.py estimates estimated_tokens=len(prompt)//4+512,
    # which crosses 16_000 for any prompt over ~62 KB.

    def test_emotion_tracker_sentiment_classification_tags(
        self, router: ModelRouter
    ) -> None:
        signal = TaskSignal(
            description="1-token sentiment classification",
            tags=["classification", "fast"],
            estimated_tokens=20_000,
        )
        decision = router.route(signal)
        assert decision.tier == ModelTier.FAST

    def test_context_window_compression_tags(self, router: ModelRouter) -> None:
        signal = TaskSignal(
            description="Compress conversation context window",
            tags=["summary", "context"],
            estimated_tokens=20_000,
        )
        decision = router.route(signal)
        assert decision.tier == ModelTier.FAST

    def test_conversation_summarizer_tags(self, router: ModelRouter) -> None:
        signal = TaskSignal(
            description="Summarize peer conversation",
            tags=["summary", "conversation"],
            estimated_tokens=20_000,
        )
        decision = router.route(signal)
        assert decision.tier == ModelTier.FAST

    def test_memory_compressor_tags_with_dynamic_group_tag(
        self, router: ModelRouter
    ) -> None:
        # `tag` in memory_compressor.py is a dynamic per-group label (e.g. "gtd",
        # "identity") that can't be enumerated — the static "compression"/"memory"
        # keywords must be sufficient on their own (set-intersection semantics
        # only need ONE overlapping keyword to fire).
        signal = TaskSignal(
            description="Compress 12 memories tagged 'gtd'",
            tags=["compression", "memory", "gtd"],
            estimated_tokens=20_000,
        )
        decision = router.route(signal)
        assert decision.tier == ModelTier.FAST
```

- [ ] **Step 3: Run the new tests to verify they fail against the unmodified router**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/clawd/skcapstone-repos/skcapstone/tests/test_model_router.py -q -k TestRealCallerTagAlignment`

Expected: `4 failed` — verified by direct execution against the unmodified router (2026-07-09), every one currently resolves to `REASON` instead of `FAST`:
```
['classification', 'fast']         20000 -> REASON | No tag rule matched; estimated_tokens=20000 exceeds 16000, using REASON tier.
['summary', 'context']             20000 -> REASON | No tag rule matched; estimated_tokens=20000 exceeds 16000, using REASON tier.
['summary', 'conversation']        20000 -> REASON | No tag rule matched; estimated_tokens=20000 exceeds 16000, using REASON tier.
['compression', 'memory', 'gtd']   20000 -> REASON | No tag rule matched; estimated_tokens=20000 exceeds 16000, using REASON tier.
```
This is the real proof of the bug: a caller whose prompt happens to be large gets silently escalated to REASON instead of the FAST tier its tags actually asked for.

- [ ] **Step 4: Implement — broaden the FAST tag rule's keywords**

Edit `skcapstone/src/skcapstone/model_router.py`, replace the FAST `TagRule` block (lines 149-153):
```python
                TagRule(
                    keywords=["format", "rename", "lint", "simple", "trivial"],
                    tier=ModelTier.FAST,
                    priority=10,
                ),
```
with:
```python
                TagRule(
                    keywords=[
                        "format", "rename", "lint", "simple", "trivial",
                        # Real-caller alignment (2026-07-09 model-router audit):
                        # these are the exact tags emotion_tracker.py,
                        # context_window.py, conversation_summarizer.py, and
                        # memory_compressor.py pass today. All four are cheap
                        # background/housekeeping calls (sentiment
                        # classification, context compression, conversation
                        # summarization, memory compression) that belong on
                        # the cheapest tier — this rule previously never fired
                        # for any of them, silently falling through to the
                        # token-count fallback instead.
                        "fast", "classification", "summary", "context",
                        "conversation", "compression", "memory",
                    ],
                    tier=ModelTier.FAST,
                    priority=10,
                ),
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/clawd/skcapstone-repos/skcapstone/tests/test_model_router.py -q -k TestRealCallerTagAlignment`

Expected: `4 passed`.

- [ ] **Step 6: Run the full file to confirm the pre-existing 2 failures are unchanged and nothing else broke**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/clawd/skcapstone-repos/skcapstone/tests/test_model_router.py -q`

Expected: `2 failed, 43 passed` (same 2 pre-existing failures as Step 1's baseline, plus the 4 new tests now passing on top of the original 39).

- [ ] **Step 7: Run skcapstone's full suite for the modules that actually call `ModelRouter` and have existing test coverage, to confirm no caller-side assumption broke**

Verified before writing this plan: `emotion_tracker.py`, `context_window.py`, and `memory_compressor.py` have **no dedicated test file today** (`find tests/ -iname '*emotion*' -o -iname '*context_window*' -o -iname '*memory_compress*'` returns nothing, and `grep -rl "emotion_tracker\|context_window\|memory_compressor" tests/` returns nothing) — that's a pre-existing coverage gap in the repo, not something this audit introduces or is scoped to fix. `conversation_summarizer.py` and `consciousness_loop.py` do have test files; run those:

```bash
cd ~ && ~/.skenv/bin/python -m pytest \
  /home/cbrd21/clawd/skcapstone-repos/skcapstone/tests/test_conversation_summarizer.py \
  /home/cbrd21/clawd/skcapstone-repos/skcapstone/tests/test_consciousness_loop.py \
  -q
```
Expected: no new failures relative to `main` (these callers only ever pass a `RouteDecision` value through, none of them assert on the *previous* wrong fallback tier — if any test does hard-assert a fallback-tier value, that test encoded the bug and must be updated the same way Task 1/Step 1 updated `test_group_responder.py`, not reverted). Since `emotion_tracker.py`/`context_window.py`/`memory_compressor.py` have zero test coverage, the `TestRealCallerTagAlignment` tests added in Step 2 above are the *only* regression coverage this fix gets for those three call sites — that's the honest current state, not an oversight.

- [ ] **Step 8: Commit**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skcapstone
git add src/skcapstone/model_router.py tests/test_model_router.py
git commit -m "fix(model_router): broaden FAST tag-rule keywords to match real callers

emotion_tracker, context_window, conversation_summarizer, and
memory_compressor all pass tags (classification/fast, summary/context,
summary/conversation, compression/memory) that never overlapped any
tag_rule keyword set, so every one of those calls silently fell through
to the token-count fallback instead of the FAST tier they actually needed.
tier_models is untouched; 2 pre-existing unrelated test failures
(test_default_fast_model, test_default_local_model — stale model-name
expectations) are unchanged."
```

---

## Post-plan summary of scope items

| Item | Status after this plan |
|------|------------------------|
| (1) skchat responder → registry-recognized role | Fixed in Task 1 — `sk-default` replaces dead `reg:ornith`. |
| (2) skcapstone `ModelRouter` tag-rules inert | Fixed in Task 3 — FAST rule broadened to match 4 real callers; `consciousness_loop.py`'s caller confirmed already-correct, untouched. |
| (3) registry.yaml role alignment | Verified in Task 2 — already correct, no edit needed. |
