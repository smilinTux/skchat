# Browser QA Lane (skwatchdog WD-10)

A scripted, **report-only** walk of skchat web in a real Chrome, capturing a
screenshot plus console diagnostics per step, grading the **image**, and writing
one result artifact the skos watchdog folds into the daily digest.

## Why it exists

A share-link fix shipped after verifying the route was present in the compiled
bundle. The page still rendered a blank grey screen, because the route did an
unguarded cast on a router extra that is null for a shared link. Unit tests were
green, the bundle was correct, and the bug was caught only when a human finally
loaded the page.

This lane is the machine that loads the page.

---

## Safety rules. Read these before adding a step.

**1. It never navigates to an existing Space.** `/app/#/spaces/{id}` does not
"look at" a Space, it **joins** it, inserting a participant into a live call that
real humans can see. Joining can also publish: two separate hot-mic bugs were
fixed where the interface showed muted while the track was live.
`lane.assert_safe_url()` refuses the whole room family (`space`, `spaces`, `room`,
`join`, `conf`, `call`, `livekit`, `sfu`, `facetime`) as a path, query, or
fragment segment, and every navigation in the lane goes through it. It refuses
the bare directory route too: the difference between "list Spaces" and "join a
Space" is one URL segment, and the guard does not gamble on getting that right.

**2. The default walk touches no room at all.** Fetching the `/spaces` JSON list
over plain HTTP and rendering the app shell at `/app/` is safe and is most of
the smoke value, so it is the default.

**3. If a run needs a room, it creates its own and ends it in the same run.**
Spaces do **not** self-expire: there is no LiveKit webhook subscriber in skchat,
so anything created stays listed as LIVE forever until a human ends it, and the
directory already carries residue from earlier testing (`sp1`, `sp2`,
`debug-hang-test`). Ending what we create is mandatory **including on the failure
path**. Three layers enforce it:

  a. create, verify, and end live inside one `try/finally`, so a crashed
     assertion still tears the Space down;
  b. the id is written to `pending-spaces.json` **before** the create call
     (`derive_space_id` is deterministic over host plus slug, so the id is
     knowable in advance), so a process killed outright still leaves a record;
  c. every run drains that record first (`reap_pending_spaces`).

  The whole sequence is plain HTTP. No browser ever joins the room, so no
  participant appears and no track can publish. It is opt-in via `--with-space`.

**4. It does not seize port 9229.** That is the daily chrome-cdp instance a human
drives, and 9222/9223 are the agent instances. The default here is **9232**,
overridable with `--cdp-port` or `SKCHAT_BROWSER_QA_CDP_PORT`.

---

## Running it

```bash
# The safe default walk: /spaces JSON + the app shell.
skchat browser-qa run

# Against a specific instance, full artifact on stdout.
skchat browser-qa run --base-url http://127.0.0.1:8765 --json

# Also render an extra in-app route (room routes are refused).
skchat browser-qa run --route '/app/#/settings'

# Also create a Space over HTTP and end it in the same run.
skchat browser-qa run --with-space

# Print the newest result without running anything.
skchat browser-qa show
```

If nothing is listening on the CDP port, the lane launches its own dedicated
headless Chrome with its own profile directory and terminates exactly what it
launched. It never terminates a Chrome it merely attached to. Set
`SKCHAT_BROWSER_QA_LAUNCH=0` to require an already-running endpoint instead.

There is no cross-run lock, which matters in exactly one case: if you run the
lane by hand on the same port while the scheduled run is mid-flight, the second
run attaches to the first run's Chrome, and when the first finishes it takes that
Chrome with it. The second then reports `capture_failed`, which is a `problem`.
Pass a different `--cdp-port` for a manual run alongside a scheduled one.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SKCHAT_BROWSER_QA_BASE` | `http://127.0.0.1:8765` | skchat web base URL under test |
| `SKCHAT_BROWSER_QA_DIR` | `~/.skchat/browser-qa` | artifact root |
| `SKCHAT_BROWSER_QA_CDP_PORT` | `9232` | CDP port (never 9229/9222/9223) |
| `SKCHAT_BROWSER_QA_SETTLE_S` | `16` | seconds to wait for the shell to boot |
| `SKCHAT_BROWSER_QA_WITH_SPACE` | `0` | opt into the Space lifecycle step |
| `SKCHAT_BROWSER_QA_HOST_FQID` | `browser-qa@skworld.io` | host identity for a created Space |
| `SKCHAT_BROWSER_QA_LAUNCH` | `1` | launch a dedicated Chrome when none is listening |
| `SKCHAT_BROWSER_QA_CHROME` | autodetected | Chrome binary |
| `SKGATEWAY_URL` | `http://localhost:18780/v1` | grading endpoint |
| `SKGATEWAY_MODEL` | `sk-default` | router alias, never a concrete model |
| `SKCHAT_BROWSER_QA_VERIFY_VISION` | `1` | probe that the grader can actually see before trusting it |

Schedule it where skchat web is **expected to be up**. An unreachable base URL is
a `problem`, which is the correct signal on a host that serves the app and the
wrong one on a host that does not.

---

## Hard-won technical facts

Each of these cost someone real time. Do not "simplify" them away.

- **Playwright's `connect_over_cdp` times out against Chrome 151 on this fleet.**
  It presents as a hung test, not as an incompatibility, so you can lose an hour
  before suspecting it. The lane speaks raw CDP over a hand-rolled websocket.
- **`/json/new` requires `PUT` on newer Chrome.** A `GET` returns 405, and it
  does not fail there: it surfaces later as a confusing `StopIteration` when
  something looks up the new target id.
- **Flutter web renders to canvas.** `document.body.innerText` is **empty on a
  fully working page**, so any DOM-text assertion reports a broken app that is
  fine, and reports a genuinely blank page identically. `Page.captureScreenshot`
  is the only reliable inspection, which is why grading looks at an image.
- **The shell needs 12 to 20 seconds after navigation** before capture. It boots
  slower than you expect, and capturing early manufactures the exact false
  positive this lane exists to avoid.
- **A screenshot response is megabytes of base64**, so the 8-byte extended
  websocket length is the normal path, not an edge case.

---

## How grading works

Two layers, and only one of them can file work.

**Layer 1, deterministic pixels** (`browser_qa/screenshot.py`). The captured PNG
is measured: distinct colour count and the fraction of the frame covered by the
single most common colour. A frame where one colour covers 99.5% or more, or
which has two or fewer distinct colours at all, is the blank/grey-screen
signature. That is a measurement, not an opinion, so it is the only thing allowed
to raise a run to `problem`.

**Layer 2, an independent model pass** (`browser_qa/grade.py`). The same image
(downscaled) plus the console log goes to skgateway as an `image_url` content
part, model `sk-default`. It reuses the WD-7 discipline: an independent pass, a
1-to-5 integer scale on three dimensions, a required literal `PASS`/`FAIL` token
as the parse gate, and a verdict **recomputed in code** from the parsed scores
against the threshold, so a model claiming PASS below threshold cannot smuggle
one through. If skgateway is unreachable or the reply does not parse, the run
carries a noted **gap** and no verdict is invented.

### The vision capability gate

**A blind grader is a fabricated verdict**, so the model must prove it can see
before it is trusted to judge a picture. Each run first sends a generated white
image with three random digits on it and asks the model to read them. Only if it
gets them right does the real grade proceed; otherwise the run skips with
`vision_unavailable` and says so in the digest.

This is not hypothetical. On this fleet today `sk-default` routes to
`openai/gpt-oss-20b`, whose model card reads `modality: text->text`. Sent a
screenshot of a correctly rendered onboarding screen, it did **not** say it
could not see the image. It scored the screen 1 out of 5 with the note "the page
shows no usable UI", having judged the console log alone. Without the gate,
every single run would have carried that line.

Set `SKCHAT_BROWSER_QA_VERIFY_VISION=0` to skip the probe once the router
reliably serves a multimodal model, which saves one call per run. Until then,
expect `vision_unavailable` in the gaps: that is the lane telling the truth
rather than printing a number.

### What earns which severity

A `problem` files a GTD item and can escalate to a staged card, so a flaky run
must not manufacture work every morning.

| Severity | Earned by |
|---|---|
| `problem` | Only deterministic evidence: the `/spaces` route was unreachable or answered the wrong shape, navigation failed, the frame could not be captured, the frame was blank/uniform, or a Space this lane created could not be ended. |
| `notable` | A model verdict of `fail`, console errors during boot, a boot slower than 25 s, a configured route refused by the safety guard, no CDP browser available, or an ungraded run. |
| ignored | Console errors that a fresh unauthenticated profile is supposed to produce (401/403 auth challenges). They stay in `console.log` and in the artifact; they just do not move the needle, or the digest would carry a `notable` line every morning for behaving correctly. Uncaught exceptions always count. |
| `info` | Every step clean. |

A model verdict can never on its own reach `problem`. Neither can a missing
browser: the lane's own tooling being absent is not the application being broken.

---

## The artifact

```
$SKCHAT_BROWSER_QA_DIR/                      (default ~/.skchat/browser-qa)
├── latest.json                              identical copy of the newest result.json
├── pending-spaces.json                      reap record; absent when nothing is outstanding
└── 20260816T060000Z-1a2b3c4d/               run_id, sorts chronologically
    ├── result.json                          the contract
    ├── app-shell.png                        evidence
    ├── app-route-app-settings.png
    └── console.log
```

`result.json` shape:

```jsonc
{
  "artifact_version": 1,
  "run_id": "20260816T060000Z-1a2b3c4d",
  "started_at": "2026-08-16T06:00:00Z",
  "finished_at": "2026-08-16T06:00:41Z",
  "base_url": "http://127.0.0.1:8765",
  "cdp_port": 9232,
  "severity": "info",                        // info | notable | problem
  "summary": "one human sentence",
  "steps": [
    {
      "name": "app.shell",                   // api.spaces | app.shell | app.route.<slug> | space.lifecycle
      "ok": true,
      "detail": "one human sentence",
      "failure_class": "",                   // "" when ok
      "duration_ms": 14210,
      "evidence": {"screenshot": "app-shell.png"},
      "meta": {"url": "...", "pixels": {"is_uniform": false, "...": "..."}}
    }
  ],
  "gaps": ["things that did NOT happen, and why; these reach the summary"],
  "notes": ["routine setup provenance; deliberately NOT in the summary"],
  "console_errors": ["[exception] TypeError: ..."],
  "grade": {
    "graded": true,
    "rubric_ref": "skchat-browser-qa@v1",
    "subject_ref": "skchat:app-shell:<run_id>",
    "scores": {"rendered": 5, "coherent": 4, "clean_console": 4},
    "overall": 4,
    "verdict": "pass",                       // "" when graded is false
    "notes": "...",
    "skip_reason": "",
    "model": "sk-default"
  },
  "artifact_dir": "/home/.../20260816T060000Z-1a2b3c4d"
}
```

The 30 most recent run directories are kept; older ones are pruned at the end of
each run.

### What the skos adapter reads

The skos half lives in `skos/src/skos/watchdog/adapters/browser.py` and is
report-only like every other Phase 1/2 source. It should:

1. glob `<root>/*/result.json`, keep runs whose `finished_at` falls inside the
   digest window, and sort by `run_id`;
2. **refuse any document whose `artifact_version` it does not know**, degrading
   to `source_unavailable("browser", ...)` rather than guessing at the shape;
3. emit `WatchdogEvent`s with `source="browser"`:

| Emitted for | kind | severity | ref |
|---|---|---|---|
| every run | `BrowserQaRun` | the artifact's own `severity` | `browser:<run_id>` |
| each failing step | CamelCase of `failure_class` (`BlankScreen`, `NavigationFailed`, `CaptureFailed`, `ApiUnreachable`, `ApiBadShape`, `SpaceNotEnded`) | `problem` if `failure_class` is in the problem set, else `notable` | `browser:<run_id>:<step.name>` |
| a graded run | `BrowserQaGraded` | `info` when `verdict == "pass"`, else `notable` | `browser:<run_id>:grade` |
| a non-empty `gaps` | `BrowserQaGap` | `notable` | `browser:<run_id>:gap` |

`summary` comes straight from `result.summary` / `step.detail` / the joined
gaps; they are already complete human sentences and need no re-rendering.
`link.uri` is `skworld://skchat/browser-qa/<run_id>`; `link.http` is the
`file://` path to the run's `result.json`, or to that step's screenshot when it
has one, so a human clicks straight through to the evidence.

The adapter must **never re-derive severity from the grade**: the artifact
already applied the discipline that only deterministic evidence earns `problem`.
It must also emit **nothing** when no run finished inside the window; "the lane
has been silent for N days" is the `scheduler` adapter's job over the run ledger,
not a second opinion here.

**Privacy:** the screenshots are pictures of the operator's own app, so they can
contain thread names and contact names. The digest carries the **path**, never
the image, and the artifact inherits the privacy of `~/.skchat`.

---

## Tests

`tests/test_browser_qa_lane.py`, `tests/test_browser_qa_cdp.py`,
`tests/test_browser_qa_grade.py`.

**No test drives a real browser or touches a live instance.** Every external
dependency is a seam (`page_factory`, `http_json`, `grade_fn`, `_http`,
`_post_json`), and an autouse `sealed` fixture in each file replaces the real
default with a raiser, so a forgotten injection fails loudly instead of quietly
opening a socket.
`test_no_test_reaches_a_browser_or_the_network` proves it by breaking
`socket.create_connection`, `socket.socket.connect`, `subprocess.Popen` and
`subprocess.run` underneath a full lane run.
