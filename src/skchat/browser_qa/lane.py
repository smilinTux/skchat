"""The browser QA lane: a scripted, report-only walk of skchat web.

WHY THIS EXISTS. A share-link fix shipped after verifying the route was
present in the compiled bundle. The page still rendered a blank grey screen,
because the route did an unguarded cast on a router extra that is null for a
shared link. Unit tests were green, the bundle was correct, and the bug was
caught only when a human finally loaded the page. This lane is the machine
that loads the page.

WHAT IT DOES. Every run walks a fixed list of steps, captures evidence
(screenshots plus console diagnostics), grades the evidence, and writes ONE
result artifact. It is REPORT-ONLY: it never opens a card, never writes a
GTD item, never restarts anything. The skos side reads the artifact and
turns it into ordinary ``WatchdogEvent`` records so it renders through the
existing digest renderer with the existing deep links.

=============================================================================
SAFETY. Read this before adding a step.
=============================================================================

1. NEVER NAVIGATE TO AN EXISTING SPACE. ``/app/#/spaces/{id}`` does not
   "look at" a Space, it JOINS it, inserting a participant into a live call
   that real humans can see. Joining can also PUBLISH: two separate hot-mic
   bugs were fixed where the interface showed muted while the track was
   live. :func:`assert_safe_url` refuses the entire Space, call, conf, join,
   and livekit route family, and EVERY navigation in this module goes
   through it. It is not advisory and it is not a lint. Do not add a step
   that bypasses it.

2. WHAT IS SAFE WITHOUT JOINING ANYTHING: fetching the ``/spaces`` JSON list
   over plain HTTP, and rendering the app shell at ``/app/``. That is most
   of the smoke value, so it is the DEFAULT walk and it touches no room at
   all.

3. IF A RUN NEEDS A ROOM, IT CREATES ITS OWN AND ENDS IT IN THE SAME RUN.
   Spaces do NOT self-expire: there is no LiveKit webhook subscriber in
   skchat, so anything created stays listed as LIVE forever until a human
   ends it, and the directory already carries residue from earlier testing
   (``sp1``, ``sp2``, ``debug-hang-test``). Ending what we create is
   therefore mandatory INCLUDING ON THE FAILURE PATH. Three layers enforce
   it: (a) the create/verify/end sequence lives inside one ``try/finally``
   so a crashed assertion still tears the Space down, (b) the id is written
   to a pending file BEFORE creation and removed only after a confirmed
   end, so a process that dies outright leaves a reap record, and (c) every
   run reaps that record first (:func:`reap_pending_spaces`). This step is
   OPT-IN (``--with-space``) precisely because rule 2 already gets most of
   the value. Note the whole sequence is plain HTTP: it exercises the real
   create and end routes without any browser ever joining the room.

4. IT DOES NOT SEIZE PORT 9229. That is the daily chrome-cdp instance a
   human drives, and 9222/9223 are the agent instances; taking any of them
   means fighting another session for tabs. The default here is
   :data:`skchat.browser_qa.cdp.DEFAULT_CDP_PORT` (9232) and it is
   configurable.

=============================================================================
SEVERITY. Read this before changing a threshold.
=============================================================================

A ``problem`` files a GTD item and can escalate to a staged card, so a flaky
run must not manufacture work every morning. Therefore:

  * ``problem`` requires DETERMINISTIC evidence, and only these five things
    qualify (:data:`PROBLEM_CLASSES`): the app HTTP surface was unreachable
    or answered the wrong shape, navigation failed, the frame could not be
    captured, the captured frame was blank/uniform, or a Space this lane
    created could not be ended.
  * A model verdict of ``fail`` is worth ``notable``. It can never reach
    ``problem`` on its own. Neither can a console error, a slow boot, a
    missing CDP endpoint, or an ungraded run. The tooling being absent is
    not the application being broken, exactly as a watchdog source being
    unavailable is never itself an emergency.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import cdp as cdp_mod
from . import grade as grade_mod
from . import screenshot as shot_mod

#: Where every run writes its evidence and its result artifact.
DEFAULT_ROOT = Path(os.path.expanduser("~/.skchat/browser-qa"))

#: The skchat web base URL under test.
DEFAULT_BASE_URL = "http://127.0.0.1:8765"

#: Seconds to wait after navigation before capturing. The Flutter shell
#: boots slower than anyone expects, and capturing early manufactures a
#: blank-screen false positive, which is the single worst outcome for a lane
#: whose whole job is to detect blank screens.
DEFAULT_SETTLE_S = 16.0

#: Above this, boot is "slow". Worth a ``notable`` line, never a problem.
SLOW_BOOT_MS = 25_000

#: Runs kept on disk. Older run directories are pruned at the end of a run.
KEEP_RUNS = 30

#: The artifact schema version. The skos adapter must refuse a shape it does
#: not understand rather than guess at it.
ARTIFACT_VERSION = 1

#: Deterministic failure classes that justify ``problem``. Everything else
#: tops out at ``notable``; see the module docstring.
PROBLEM_CLASSES = frozenset(
    {
        "api_unreachable",
        "api_bad_shape",
        "navigation_failed",
        "capture_failed",
        "blank_screen",
        "space_not_ended",
    }
)

#: Route words that mean "this URL joins or touches a live room". Matched
#: against every path/query/fragment SEGMENT of a navigation target, so
#: ``/app/#/spaces/abc``, ``/space/abc``, ``/app/#/conf?room=x`` and
#: ``/livekit.html`` are all refused. The bare directory route is refused
#: too: the difference between "list Spaces" and "join a Space" is one
#: segment, and this guard does not gamble on getting that right.
FORBIDDEN_SEGMENTS = frozenset(
    {
        "space",
        "spaces",
        "room",
        "rooms",
        "join",
        "join-host",
        "join-guest",
        "conf",
        "call",
        "calls",
        "livekit",
        "livekit.html",
        "sfu",
        "facetime",
    }
)

_SEGMENT_SPLIT = re.compile(r"[/?&=#]+")

#: Console errors that a fresh, unauthenticated browser profile is SUPPOSED
#: to produce. The lane deliberately runs with its own throwaway profile and
#: no operator session, so the app's first calls are answered with an auth
#: challenge; counting those toward severity would put a `notable` line in
#: the digest every single morning for behaving correctly. They stay in the
#: artifact and in console.log, they just do not move the needle.
_EXPECTED_AUTH_NOISE = re.compile(r"\b(401|403)\b.*\b(unauthorized|forbidden)\b", re.I)


def significant_console_errors(errors: list[str]) -> list[str]:
    """The console errors worth a human's attention.

    Uncaught exceptions always count: the bug class this lane exists for (an
    unguarded cast on a null router extra) surfaces as exactly that. Expected
    auth challenges never do (see :data:`_EXPECTED_AUTH_NOISE`).
    """
    return [e for e in errors if not _EXPECTED_AUTH_NOISE.search(e)]


class UnsafeNavigation(RuntimeError):
    """A navigation target was refused by :func:`assert_safe_url`."""


def assert_safe_url(url: str) -> str:
    """Refuse any URL that could join or touch a live room.

    This is the hard guard behind safety rule 1. It is intentionally
    over-broad: it rejects the Space directory route as readily as a Space
    id, because joining a real call that humans can see, possibly with a
    live microphone track, is not a risk worth trading for one extra smoke
    assertion. If you genuinely need a room, create your own (see
    :func:`space_lifecycle_step`), do not navigate to someone else's.
    """
    parsed = urllib.parse.urlsplit(url)
    haystack = "/".join(part for part in (parsed.path, parsed.query, parsed.fragment) if part)
    for raw in _SEGMENT_SPLIT.split(haystack):
        if raw.lower() in FORBIDDEN_SEGMENTS:
            raise UnsafeNavigation(
                f"refusing to navigate to {url!r}: segment {raw!r} can join a live room"
            )
    return url


# ------------------------------------------------------------------ types ---


@dataclass
class StepResult:
    """One step of the walk."""

    name: str
    ok: bool
    detail: str = ""
    failure_class: str = ""
    duration_ms: int = 0
    evidence: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "failure_class": self.failure_class,
            "duration_ms": self.duration_ms,
            "evidence": dict(self.evidence),
            "meta": dict(self.meta),
        }


@dataclass
class LaneResult:
    """The whole run. Serialized to ``result.json``, which is the entire
    contract the skos adapter reads."""

    run_id: str
    started_at: str
    finished_at: str = ""
    base_url: str = ""
    cdp_port: int = 0
    severity: str = "info"
    summary: str = ""
    steps: list[StepResult] = field(default_factory=list)
    #: Things that did NOT happen, and why. These reach the digest summary.
    gaps: list[str] = field(default_factory=list)
    #: Routine provenance about how the run was set up. Deliberately kept out
    #: of the summary: "launched a browser" is not something a human needs to
    #: read at 06:00, and putting it in `gaps` made the digest line trail off
    #: into operational trivia.
    notes: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    grade: Optional[grade_mod.BrowserGrade] = None
    artifact_dir: str = ""
    artifact_version: int = ARTIFACT_VERSION

    def step(self, name: str) -> Optional[StepResult]:
        for item in self.steps:
            if item.name == name:
                return item
        return None

    def to_dict(self) -> dict:
        return {
            "artifact_version": self.artifact_version,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "base_url": self.base_url,
            "cdp_port": self.cdp_port,
            "severity": self.severity,
            "summary": self.summary,
            "steps": [s.to_dict() for s in self.steps],
            "gaps": list(self.gaps),
            "notes": list(self.notes),
            "console_errors": list(self.console_errors),
            "grade": self.grade.to_dict() if self.grade else None,
            "artifact_dir": self.artifact_dir,
        }


# ------------------------------------------------------------------ seams ---


def default_http_json(
    method: str, url: str, body: Optional[dict] = None, *, timeout: float = 10.0
) -> tuple[int, Any]:
    """The real HTTP boundary: returns ``(status, decoded_body)``. Raises on
    a transport failure so the caller can classify it; an HTTP error status
    is RETURNED, not raised, because a 503 from an unconfigured LiveKit is a
    gap while a refused connection is an outage.

    Monkeypatched wholesale in tests; nothing else in this module opens a
    socket to the application.
    """
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local daemon
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


HttpJson = Callable[..., tuple[int, Any]]
PageFactory = Callable[[], cdp_mod.BrowserPage]
GradeFn = Callable[..., grade_mod.BrowserGrade]


# ------------------------------------------------------- pending-space reap --


def _pending_path(root: Path) -> Path:
    return root / "pending-spaces.json"


def note_pending_space(root: Path, base_url: str, space_id: str, host: str) -> None:
    """Record a Space BEFORE creating it. Layer (b) of the teardown
    guarantee: if this process is killed between create and end, the record
    survives and the next run reaps it."""
    path = _pending_path(root)
    entries = _read_pending(root)
    entries.append({"base_url": base_url, "space_id": space_id, "host_fqid": host})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2))


def clear_pending_space(root: Path, space_id: str) -> None:
    entries = [e for e in _read_pending(root) if e.get("space_id") != space_id]
    path = _pending_path(root)
    if entries:
        path.write_text(json.dumps(entries, indent=2))
    elif path.exists():
        path.unlink()


def _read_pending(root: Path) -> list[dict]:
    path = _pending_path(root)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def reap_pending_spaces(root: Path, http_json: HttpJson) -> list[str]:
    """End every Space a previous run recorded but never confirmed ended.
    Layer (c) of the teardown guarantee. Returns the ids reaped."""
    reaped: list[str] = []
    for entry in _read_pending(root):
        space_id = str(entry.get("space_id") or "")
        if not space_id:
            continue
        try:
            status, _ = http_json(
                "POST",
                f"{entry.get('base_url', '')}/spaces/{space_id}/end",
                {"requester": entry.get("host_fqid", "")},
            )
        except Exception:  # noqa: BLE001 - a dead daemon leaves the record for next time
            continue
        # 404 means it is already gone, which is the outcome we wanted.
        if status in (200, 404):
            clear_pending_space(root, space_id)
            reaped.append(space_id)
    return reaped


# ------------------------------------------------------------------ steps ---


def spaces_list_step(base_url: str, http_json: HttpJson) -> StepResult:
    """Fetch the ``/spaces`` JSON directory. No browser, no room, no join
    (safety rule 2). Proves the daemon is up and the Spaces surface answers
    the right shape."""
    started = time.monotonic()
    try:
        status, body = http_json("GET", f"{base_url}/spaces")
    except Exception as exc:  # noqa: BLE001 - any transport failure is one answer
        return StepResult(
            name="api.spaces",
            ok=False,
            detail=f"GET /spaces did not answer: {exc}",
            failure_class="api_unreachable",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    duration = int((time.monotonic() - started) * 1000)
    if status != 200:
        return StepResult(
            name="api.spaces",
            ok=False,
            detail=f"GET /spaces returned HTTP {status}",
            failure_class="api_bad_shape",
            duration_ms=duration,
        )
    if not isinstance(body, dict) or not isinstance(body.get("spaces"), list):
        return StepResult(
            name="api.spaces",
            ok=False,
            detail="GET /spaces did not return a spaces list",
            failure_class="api_bad_shape",
            duration_ms=duration,
        )
    live = body["spaces"]
    return StepResult(
        name="api.spaces",
        ok=True,
        detail=f"{len(live)} live Space(s) listed.",
        duration_ms=duration,
        meta={"live_count": len(live)},
    )


def render_step(
    page: cdp_mod.BrowserPage,
    *,
    name: str,
    url: str,
    settle_s: float,
    out_dir: Path,
    slug: str,
) -> StepResult:
    """Navigate, wait for the shell to boot, capture the frame, measure it.

    The measurement is the point. A DOM-text assertion here would be worse
    than nothing: this app paints into a canvas, so ``innerText`` is empty
    on a healthy page and empty on a blank one.
    """
    assert_safe_url(url)  # safety rule 1, on every navigation, no exceptions
    started = time.monotonic()
    try:
        page.navigate(url)
    except Exception as exc:  # noqa: BLE001
        return StepResult(
            name=name,
            ok=False,
            detail=f"navigation to {url} failed: {exc}",
            failure_class="navigation_failed",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    page.settle(settle_s)
    duration = int((time.monotonic() - started) * 1000)

    try:
        png = page.screenshot()
    except Exception as exc:  # noqa: BLE001
        return StepResult(
            name=name,
            ok=False,
            detail=f"could not capture a frame at {url}: {exc}",
            failure_class="capture_failed",
            duration_ms=duration,
        )

    shot_path = out_dir / f"{slug}.png"
    out_dir.mkdir(parents=True, exist_ok=True)
    shot_path.write_bytes(png)
    evidence = {"screenshot": shot_path.name}

    try:
        stats = shot_mod.inspect_png(png)
    except shot_mod.ScreenshotError as exc:
        return StepResult(
            name=name,
            ok=False,
            detail=f"captured frame at {url} is not a usable image: {exc}",
            failure_class="capture_failed",
            duration_ms=duration,
            evidence=evidence,
        )

    meta: dict = {"url": url, "pixels": stats.to_dict()}
    # Advisory only, never load-bearing for severity: the canvas host element
    # is a useful breadcrumb but its tag name is a Flutter implementation
    # detail that has changed before.
    try:
        meta["canvas_host"] = bool(
            page.evaluate(
                "!!(document.querySelector('flt-glass-pane') "
                "|| document.querySelector('flutter-view') "
                "|| document.querySelector('canvas'))"
            )
        )
    except Exception:  # noqa: BLE001 - a breadcrumb is never worth failing a step
        meta["canvas_host"] = None

    if stats.is_uniform:
        return StepResult(
            name=name,
            ok=False,
            detail=(
                f"{url} rendered a blank screen: one colour covers "
                f"{stats.dominant_fraction:.1%} of the frame across "
                f"{stats.distinct_colors} distinct colour(s)."
            ),
            failure_class="blank_screen",
            duration_ms=duration,
            evidence=evidence,
            meta=meta,
        )

    detail = f"{url} rendered in {duration} ms."
    if duration > SLOW_BOOT_MS:
        detail = f"{url} rendered, but took {duration} ms to boot."
    return StepResult(
        name=name, ok=True, detail=detail, duration_ms=duration, evidence=evidence, meta=meta
    )


def space_lifecycle_step(
    base_url: str, http_json: HttpJson, *, root: Path, host_fqid: str
) -> StepResult:
    """Create a Space, confirm it lists, END IT, confirm it stops listing.

    Plain HTTP throughout. No browser ever joins the room, so no participant
    appears and no track can publish. Safety rule 3: the end call is in a
    ``finally``, so a crashed assertion above it still tears the Space down,
    and the id is recorded on disk BEFORE the create call so a process killed
    mid-create still leaves something for :func:`reap_pending_spaces`.

    The id is knowable before creation because ``derive_space_id`` is
    deterministic over host plus slug, which is what closes that window: we
    are never in the position of having created a room whose id we failed to
    write down.
    """
    from ..spaces.space import derive_space_id

    started = time.monotonic()
    slug = f"qa-lane-{uuid.uuid4().hex[:8]}"
    space_id = derive_space_id(host_fqid, slug)
    created = False
    verified = False
    detail_bits: list[str] = []
    note_pending_space(root, base_url, space_id, host_fqid)

    try:
        status, body = http_json(
            "POST",
            f"{base_url}/spaces/create",
            {"host_fqid": host_fqid, "title": "skchat browser QA lane", "slug": slug},
        )
        if status == 503:
            clear_pending_space(root, space_id)
            return StepResult(
                name="space.lifecycle",
                ok=True,
                detail="LiveKit is not configured on this instance, so no Space was created.",
                duration_ms=int((time.monotonic() - started) * 1000),
                meta={"skipped": True, "reason": "livekit_unconfigured"},
            )
        if status != 200 or not isinstance(body, dict):
            clear_pending_space(root, space_id)
            return StepResult(
                name="space.lifecycle",
                ok=False,
                detail=f"POST /spaces/create returned HTTP {status}",
                failure_class="api_bad_shape",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        created = True
        # Trust the server's own id over the locally derived one if they
        # ever diverge; the pre-recorded id stays on file either way, so a
        # divergence leaves two reap candidates rather than a leaked room.
        served_id = str(body.get("space_id") or body.get("room") or "")
        if served_id and served_id != space_id:
            note_pending_space(root, base_url, served_id, host_fqid)
            space_id = served_id

        status, listing = http_json("GET", f"{base_url}/spaces")
        ids = (
            [s.get("space_id") for s in listing.get("spaces", [])]
            if isinstance(listing, dict)
            else []
        )
        verified = space_id in ids
        detail_bits.append(
            "the new Space listed as live" if verified else "the new Space did NOT list as live"
        )
    finally:
        ended = False
        if created:
            try:
                status, _ = http_json(
                    "POST", f"{base_url}/spaces/{space_id}/end", {"requester": host_fqid}
                )
                ended = status in (200, 404)
            except Exception as exc:  # noqa: BLE001
                detail_bits.append(f"the end call raised: {exc}")
            if ended:
                clear_pending_space(root, space_id)

    duration = int((time.monotonic() - started) * 1000)
    if created and not ended:
        return StepResult(
            name="space.lifecycle",
            ok=False,
            detail=(
                f"Space {space_id} was created but could NOT be ended; it will stay "
                "listed as live until a human ends it. " + " ".join(detail_bits)
            ),
            failure_class="space_not_ended",
            duration_ms=duration,
            meta={"space_id": space_id, "leaked": True},
        )
    if not verified:
        return StepResult(
            name="space.lifecycle",
            ok=False,
            detail=f"Space {space_id} was created and ended, but " + " ".join(detail_bits) + ".",
            failure_class="api_bad_shape",
            duration_ms=duration,
            meta={"space_id": space_id, "leaked": False},
        )
    return StepResult(
        name="space.lifecycle",
        ok=True,
        detail=f"Space {space_id} was created, listed, and ended in this run.",
        duration_ms=duration,
        meta={"space_id": space_id, "leaked": False},
    )


# --------------------------------------------------------------- severity ---


def compute_severity(result: LaneResult) -> tuple[str, str]:
    """Turn a finished run into one severity and one human sentence.

    ``problem`` requires deterministic evidence (:data:`PROBLEM_CLASSES`).
    A model verdict, a console error, a slow boot, a missing CDP endpoint
    and an ungraded run all top out at ``notable``, so a bad morning at the
    gateway cannot file work. See the module docstring.
    """
    hard = [s for s in result.steps if not s.ok and s.failure_class in PROBLEM_CLASSES]
    if hard:
        first = hard[0]
        extra = f" (and {len(hard) - 1} more)" if len(hard) > 1 else ""
        return "problem", f"{first.detail}{extra}"

    soft = [s for s in result.steps if not s.ok]
    notes: list[str] = []
    if soft:
        notes.append(soft[0].detail)
    if result.grade is not None and result.grade.graded and result.grade.verdict == "fail":
        notes.append(
            f"the independent grader scored the shell "
            f"{result.grade.overall}/5 against {result.grade.rubric_ref}."
        )
    significant = significant_console_errors(result.console_errors)
    if significant:
        notes.append(f"{len(significant)} console error(s) during boot.")
    slow = [s for s in result.steps if s.ok and s.duration_ms > SLOW_BOOT_MS]
    if slow:
        notes.append(f"{slow[0].name} took {slow[0].duration_ms} ms to render.")
    if result.gaps:
        notes.append(result.gaps[0])

    if notes:
        return "notable", " ".join(notes)

    passed = sum(1 for s in result.steps if s.ok)
    return "info", f"skchat web QA lane walked {passed} step(s) clean."


# ------------------------------------------------------------------- run ----


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "route"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def prune_runs(root: Path, keep: int = KEEP_RUNS) -> None:
    """Keep the most recent ``keep`` run directories. Run ids sort
    chronologically because they start with a compact UTC timestamp."""
    if not root.exists():
        return
    dirs = sorted(
        (p for p in root.iterdir() if p.is_dir() and (p / "result.json").exists()),
        key=lambda p: p.name,
    )
    for stale in dirs[:-keep] if keep > 0 else dirs:
        shutil.rmtree(stale, ignore_errors=True)


def run_lane(
    *,
    base_url: str = "",
    root: Optional[Path] = None,
    cdp_port: Optional[int] = None,
    settle_s: Optional[float] = None,
    with_space: Optional[bool] = None,
    extra_routes: tuple[str, ...] = (),
    host_fqid: str = "",
    page_factory: Optional[PageFactory] = None,
    http_json: Optional[HttpJson] = None,
    grade_fn: Optional[GradeFn] = None,
    clock: Callable[[], datetime] = _now,
) -> LaneResult:
    """Walk the lane once and write the result artifact.

    Every external dependency arrives as an argument: ``page_factory`` is
    the browser, ``http_json`` is the application, ``grade_fn`` is the
    model. Tests inject all three and never touch a socket. The defaults are
    resolved lazily so importing this module opens nothing.
    """
    base_url = (base_url or os.environ.get("SKCHAT_BROWSER_QA_BASE") or DEFAULT_BASE_URL).rstrip(
        "/"
    )
    root = Path(root) if root else Path(os.environ.get("SKCHAT_BROWSER_QA_DIR") or DEFAULT_ROOT)
    cdp_port = (
        cdp_port
        if cdp_port is not None
        else int(os.environ.get("SKCHAT_BROWSER_QA_CDP_PORT", cdp_mod.DEFAULT_CDP_PORT))
    )
    settle_s = (
        settle_s
        if settle_s is not None
        else float(os.environ.get("SKCHAT_BROWSER_QA_SETTLE_S", DEFAULT_SETTLE_S))
    )
    with_space = (
        with_space if with_space is not None else _env_flag("SKCHAT_BROWSER_QA_WITH_SPACE", False)
    )
    host_fqid = host_fqid or os.environ.get("SKCHAT_BROWSER_QA_HOST_FQID", "browser-qa@skworld.io")
    http_json = http_json or default_http_json
    grade_fn = grade_fn or grade_mod.grade_screenshot

    started = clock()
    run_id = f"{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    out_dir = root / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    result = LaneResult(
        run_id=run_id,
        started_at=_iso(started),
        base_url=base_url,
        cdp_port=cdp_port,
        artifact_dir=str(out_dir),
    )

    # Safety rule 3, layer (c): clean up anything a previous run left behind
    # before creating anything new.
    try:
        reaped = reap_pending_spaces(root, http_json)
        if reaped:
            result.gaps.append(
                f"reaped {len(reaped)} Space(s) a previous run left live: {', '.join(reaped)}."
            )
    except Exception as exc:  # noqa: BLE001 - reaping must never abort a run
        result.gaps.append(f"could not reap previous Spaces: {exc}")

    # Step 1: the no-browser, no-join smoke check (safety rule 2).
    result.steps.append(spaces_list_step(base_url, http_json))

    # Step 2: render the app shell, plus any configured extra routes.
    page: Optional[cdp_mod.BrowserPage] = None
    launched = None
    try:
        if page_factory is None:
            launched = _maybe_launch(cdp_port, result)
            page_factory = cdp_mod.default_page_factory(cdp_port)
        page = page_factory()
    except Exception as exc:  # noqa: BLE001
        # The lane's own tooling being absent is NOT the application being
        # broken, so this is a gap, never a problem.
        result.gaps.append(f"no CDP browser was available on port {cdp_port}: {exc}")

    primary_png = b""
    primary_route = ""
    try:
        if page is not None:
            routes: list[tuple[str, str]] = [("app.shell", f"{base_url}/app/")]
            for route in extra_routes:
                target = route if route.startswith("http") else f"{base_url}{route}"
                routes.append((f"app.route.{_slugify(route)}", target))

            for name, url in routes:
                try:
                    step = render_step(
                        page,
                        name=name,
                        url=url,
                        settle_s=settle_s,
                        out_dir=out_dir,
                        slug=_slugify(name),
                    )
                except UnsafeNavigation as exc:
                    # A configured route tried to join a room. Refusing is
                    # the correct outcome and it is a configuration gap, not
                    # an application fault.
                    result.gaps.append(str(exc))
                    continue
                result.steps.append(step)
                if name == "app.shell" and step.evidence.get("screenshot"):
                    primary_route = url
                    primary_png = (out_dir / step.evidence["screenshot"]).read_bytes()

            entries = page.console()
            result.console_errors = [
                f"[{e.source}] {e.text}" for e in entries if e.level.lower() == "error"
            ][:50]
            if entries:
                (out_dir / "console.log").write_text(
                    "\n".join(f"{e.level}\t{e.source}\t{e.text}" for e in entries)
                )
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001 - a stuck tab must not fail the run
                pass
        if launched is not None:
            _terminate(launched)

    # Step 3: the optional Space lifecycle (safety rule 3).
    if with_space:
        try:
            result.steps.append(
                space_lifecycle_step(base_url, http_json, root=root, host_fqid=host_fqid)
            )
        except Exception as exc:  # noqa: BLE001
            # The create call itself never answered. Nothing was confirmed
            # created, and the pending record written before the call stays
            # on file, so the next run reaps it if a room did appear.
            result.steps.append(
                StepResult(
                    name="space.lifecycle",
                    ok=False,
                    detail=f"the Space create call did not answer: {exc}",
                    failure_class="api_unreachable",
                )
            )

    # Step 4: the independent model pass over the IMAGE. Note a FAILED shell
    # step still has its frame graded: a blank screen is exactly the picture
    # the digest wants a sentence about.
    if primary_png:
        try:
            graded = grade_fn(
                primary_png,
                route=primary_route,
                console_lines=result.console_errors,
                subject_ref=f"skchat:app-shell:{run_id}",
            )
        except Exception as exc:  # noqa: BLE001
            # grade_screenshot contracts never to raise, but a surprise here
            # must not cost us the artifact for a whole run.
            graded = grade_mod.BrowserGrade(
                graded=False,
                subject_ref=f"skchat:app-shell:{run_id}",
                skip_reason=f"grader_raised: {exc}",
            )
        result.grade = graded
        if not graded.graded:
            why = f" {graded.notes.rstrip('.')}." if graded.notes else ""
            result.gaps.append(
                f"the shell frame was not graded this run ({graded.skip_reason})."
                f"{why} No verdict was invented."
            )
    else:
        result.gaps.append("no shell frame was captured, so nothing was graded.")

    result.finished_at = _iso(clock())
    result.severity, result.summary = compute_severity(result)

    write_artifact(result, out_dir, root)
    prune_runs(root)
    return result


def write_artifact(result: LaneResult, out_dir: Path, root: Path) -> Path:
    """Write ``result.json`` in the run directory and refresh
    ``latest.json`` at the root. Both hold the identical document."""
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result.to_dict(), indent=2, sort_keys=True)
    path = out_dir / "result.json"
    path.write_text(payload)
    (root / "latest.json").write_text(payload)
    return path


def _maybe_launch(port: int, result: LaneResult):
    """Attach to a Chrome already listening on ``port``; launch a dedicated
    one only when told to. Never terminates a Chrome it did not start, and
    never touches 9229/9222/9223 unless someone explicitly configures it
    to (safety rule 4)."""
    try:
        cdp_mod.version(port)
        return None  # something is already listening; attach, do not manage it
    except cdp_mod.CdpError:
        pass
    if not _env_flag("SKCHAT_BROWSER_QA_LAUNCH", True):
        raise cdp_mod.CdpError(f"nothing listening on CDP port {port} and launching is disabled")
    proc = cdp_mod.launch_chrome(port)
    result.notes.append(f"launched a dedicated headless Chrome on port {port} for this run.")
    return proc


def _terminate(proc) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "ARTIFACT_VERSION",
    "DEFAULT_BASE_URL",
    "DEFAULT_ROOT",
    "DEFAULT_SETTLE_S",
    "FORBIDDEN_SEGMENTS",
    "LaneResult",
    "PROBLEM_CLASSES",
    "SLOW_BOOT_MS",
    "StepResult",
    "UnsafeNavigation",
    "assert_safe_url",
    "compute_severity",
    "default_http_json",
    "prune_runs",
    "reap_pending_spaces",
    "significant_console_errors",
    "render_step",
    "run_lane",
    "space_lifecycle_step",
    "spaces_list_step",
    "write_artifact",
]
