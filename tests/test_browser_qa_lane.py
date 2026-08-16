"""Tests for the WD-10 browser QA lane (skchat.browser_qa.lane).

NOTHING HERE DRIVES A BROWSER OR TOUCHES A LIVE INSTANCE. The lane takes
every external dependency as an argument (``page_factory``, ``http_json``,
``grade_fn``), and the autouse ``sealed`` fixture below replaces each module
default with a raiser, so a test that forgets to inject one FAILS LOUDLY
instead of quietly opening a socket to whatever happens to be running on the
developer's machine. ``test_no_test_reaches_a_browser_or_the_network`` proves
the seal by also breaking ``socket`` and ``subprocess`` underneath a full
run.
"""

from __future__ import annotations

import io
import json
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skchat.browser_qa import cdp as cdp_mod
from skchat.browser_qa import grade as grade_mod
from skchat.browser_qa import lane as lane_mod
from skchat.browser_qa.lane import (
    LaneResult,
    StepResult,
    UnsafeNavigation,
    assert_safe_url,
    compute_severity,
    reap_pending_spaces,
    render_step,
    run_lane,
    space_lifecycle_step,
    spaces_list_step,
)

# ------------------------------------------------------------- the seal ----


def _boom(*_args, **_kwargs):
    raise AssertionError("a test tried to reach the real browser/app/model seam")


@pytest.fixture(autouse=True)
def sealed(monkeypatch, tmp_path):
    """Every real boundary defaults to raising. Same pattern the rest of the
    skwatchdog epic uses, and it works: a forgotten injection is an immediate,
    obvious failure rather than a live request."""
    monkeypatch.setattr(lane_mod, "default_http_json", _boom)
    monkeypatch.setattr(grade_mod, "grade_screenshot", _boom)
    monkeypatch.setattr(cdp_mod, "connect_page", _boom)
    monkeypatch.setattr(cdp_mod, "launch_chrome", _boom)
    monkeypatch.setattr(cdp_mod, "version", _boom)
    # Never resolve a real artifact root, base URL, or CDP port from the env.
    monkeypatch.setenv("SKCHAT_BROWSER_QA_DIR", str(tmp_path / "artifacts"))
    monkeypatch.delenv("SKCHAT_BROWSER_QA_BASE", raising=False)
    monkeypatch.delenv("SKCHAT_BROWSER_QA_CDP_PORT", raising=False)
    monkeypatch.delenv("SKCHAT_BROWSER_QA_WITH_SPACE", raising=False)
    monkeypatch.setenv("SKCHAT_BROWSER_QA_LAUNCH", "0")
    yield


# ------------------------------------------------------------- test doubles --


def _png(color=(30, 90, 180), size=(64, 48), stripe=False) -> bytes:
    from PIL import Image

    img = Image.new("RGB", size, color)
    if stripe:
        for x in range(size[0]):
            for y in range(size[1]):
                img.putpixel((x, y), ((x * 7) % 256, (y * 11) % 256, (x + y) % 256))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


BUSY_PNG = _png(stripe=True)
BLANK_PNG = _png(color=(128, 128, 128))


class FakePage:
    """Stands in for a CDP-attached tab. Records what it was asked to do so
    a test can assert the lane never navigated somewhere it should not."""

    def __init__(self, *, png=BUSY_PNG, console=(), navigate_error="", screenshot_error=""):
        self.png = png
        self._console = list(console)
        self.navigate_error = navigate_error
        self.screenshot_error = screenshot_error
        self.visited: list[str] = []
        self.settled = 0.0
        self.closed = False

    def navigate(self, url):
        self.visited.append(url)
        if self.navigate_error:
            raise cdp_mod.CdpError(self.navigate_error)

    def settle(self, seconds):
        self.settled += seconds

    def screenshot(self):
        if self.screenshot_error:
            raise cdp_mod.CdpError(self.screenshot_error)
        return self.png

    def evaluate(self, expression):
        return True

    def console(self):
        return list(self._console)

    def close(self):
        self.closed = True


class FakeHttp:
    """Stands in for the skchat daemon. ``routes`` maps ``(method, path)``
    to ``(status, body)``; anything unmapped 404s."""

    def __init__(self, routes=None, raise_on=()):
        self.routes = dict(routes or {})
        self.raise_on = set(raise_on)
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, url, body=None, **_kw):
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        path = "/" + path
        self.calls.append((method, path, body))
        if (method, path) in self.raise_on:
            raise ConnectionRefusedError("connection refused")
        return self.routes.get((method, path), (404, {"detail": "not found"}))


def _ok_grade(*_args, **kwargs):
    return grade_mod.BrowserGrade(
        graded=True,
        subject_ref=kwargs.get("subject_ref", ""),
        scores={"rendered": 5, "coherent": 5, "clean_console": 5},
        overall=5,
        verdict="pass",
        notes="Looks fine.",
        model="sk-default",
    )


def _skipped_grade(*_args, **kwargs):
    return grade_mod.BrowserGrade(
        graded=False,
        subject_ref=kwargs.get("subject_ref", ""),
        skip_reason=grade_mod.SkipReason.GATEWAY_UNREACHABLE,
        model="sk-default",
    )


def _spaces_ok(items=()):
    return {("GET", "/spaces"): (200, {"spaces": list(items)})}


# ------------------------------------------------------- the safety guard ---


@pytest.mark.parametrize(
    "url",
    [
        "http://h/app/#/spaces/space-abc123",
        "http://h/app/#/spaces",
        "http://h/space/space-abc123",
        "http://h/spaces/space-abc/join",
        "http://h/app/#/conf?room=space-abc",
        "http://h/app/#/call/someone",
        "http://h/livekit.html?room=x",
        "http://h/join/abcd",
        "http://h/sfu/get",
        "http://h/app/#/facetime",
    ],
)
def test_room_routes_are_refused(url):
    """Navigating to a Space JOINS it, inserting a participant into a live
    call humans can see, and can publish audio. The guard is deliberately
    over-broad: the whole room family is refused, directory route included."""
    with pytest.raises(UnsafeNavigation):
        assert_safe_url(url)


@pytest.mark.parametrize(
    "url",
    ["http://h/app/", "http://h/app/#/settings", "http://h/health", "http://h/app/#/contacts"],
)
def test_ordinary_app_routes_are_allowed(url):
    assert assert_safe_url(url) == url


def test_render_step_refuses_a_room_route_before_navigating(tmp_path):
    page = FakePage()
    with pytest.raises(UnsafeNavigation):
        render_step(
            page,
            name="app.route.spaces",
            url="http://h/app/#/spaces/space-abc",
            settle_s=0.0,
            out_dir=tmp_path,
            slug="spaces",
        )
    assert page.visited == []  # refused BEFORE the navigation, not after


def test_a_configured_room_route_becomes_a_gap_not_a_navigation(tmp_path):
    page = FakePage()
    result = run_lane(
        base_url="http://h",
        root=tmp_path,
        settle_s=0.0,
        page_factory=lambda: page,
        http_json=FakeHttp(_spaces_ok()),
        grade_fn=_ok_grade,
        extra_routes=("/app/#/spaces/space-abc",),
    )
    assert page.visited == ["http://h/app/"]
    assert any("can join a live room" in gap for gap in result.gaps)
    assert result.severity == "notable"


# ------------------------------------------------------------ the JSON list --


def test_spaces_list_step_reads_the_directory_without_a_browser():
    http = FakeHttp(_spaces_ok([{"space_id": "space-a"}]))
    step = spaces_list_step("http://h", http)
    assert step.ok and step.meta["live_count"] == 1
    assert http.calls == [("GET", "/spaces", None)]


def test_unreachable_daemon_is_a_problem():
    http = FakeHttp({}, raise_on={("GET", "/spaces")})
    step = spaces_list_step("http://h", http)
    assert not step.ok and step.failure_class == "api_unreachable"
    assert step.failure_class in lane_mod.PROBLEM_CLASSES


def test_wrong_shape_from_the_spaces_route_is_a_problem():
    step = spaces_list_step("http://h", FakeHttp({("GET", "/spaces"): (200, {"nope": 1})}))
    assert not step.ok and step.failure_class == "api_bad_shape"


# ------------------------------------------------------- rendering + pixels --


def test_a_painted_shell_passes(tmp_path):
    step = render_step(
        FakePage(),
        name="app.shell",
        url="http://h/app/",
        settle_s=0.0,
        out_dir=tmp_path,
        slug="app-shell",
    )
    assert step.ok
    assert (tmp_path / "app-shell.png").exists()
    assert step.meta["pixels"]["is_uniform"] is False


def test_a_blank_grey_screen_is_a_problem(tmp_path):
    """The motivating bug: the route was in the bundle, the suite was green,
    and the page rendered a flat grey rectangle."""
    step = render_step(
        FakePage(png=BLANK_PNG),
        name="app.shell",
        url="http://h/app/",
        settle_s=0.0,
        out_dir=tmp_path,
        slug="app-shell",
    )
    assert not step.ok and step.failure_class == "blank_screen"
    assert step.failure_class in lane_mod.PROBLEM_CLASSES
    assert (tmp_path / "app-shell.png").exists()  # evidence kept for the failure


def test_an_empty_dom_does_not_fail_the_step(tmp_path):
    """Flutter web paints into a canvas, so document.body.innerText is EMPTY
    on a perfectly healthy page. Nothing in the lane may assert on DOM text."""
    page = FakePage()
    page.evaluate = lambda expr: ""  # an empty DOM, as a real healthy shell reports
    step = render_step(
        page,
        name="app.shell",
        url="http://h/app/",
        settle_s=0.0,
        out_dir=tmp_path,
        slug="app-shell",
    )
    assert step.ok


def test_failed_navigation_is_a_problem(tmp_path):
    step = render_step(
        FakePage(navigate_error="net::ERR_CONNECTION_REFUSED"),
        name="app.shell",
        url="http://h/app/",
        settle_s=0.0,
        out_dir=tmp_path,
        slug="app-shell",
    )
    assert not step.ok and step.failure_class == "navigation_failed"


def test_failed_capture_is_a_problem(tmp_path):
    step = render_step(
        FakePage(screenshot_error="no data"),
        name="app.shell",
        url="http://h/app/",
        settle_s=0.0,
        out_dir=tmp_path,
        slug="app-shell",
    )
    assert not step.ok and step.failure_class == "capture_failed"


# ------------------------------------------------ the Space, and its teardown --


def _space_routes(space_id):
    return {
        ("POST", "/spaces/create"): (200, {"space_id": space_id}),
        ("GET", "/spaces"): (200, {"spaces": [{"space_id": space_id}]}),
        ("POST", f"/spaces/{space_id}/end"): (200, {"ok": True}),
    }


def test_the_space_it_creates_is_ended_in_the_same_run(tmp_path):
    http = FakeHttp()
    created = {}

    def routed(method, url, body=None, **kw):
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
        http.calls.append((method, path, body))
        if (method, path) == ("POST", "/spaces/create"):
            created["id"] = "space-derived"
            return 200, {"space_id": "space-derived"}
        if (method, path) == ("GET", "/spaces"):
            return 200, {"spaces": [{"space_id": "space-derived"}] if created else []}
        if path.endswith("/end"):
            created.clear()
            return 200, {"ok": True}
        return 404, {}

    step = space_lifecycle_step("http://h", routed, root=tmp_path, host_fqid="qa@skworld.io")
    assert step.ok and step.meta["leaked"] is False
    assert ("POST", "/spaces/space-derived/end", {"requester": "qa@skworld.io"}) in http.calls
    assert created == {}


def test_the_space_is_ended_even_when_the_verify_step_explodes(tmp_path):
    """The teardown must survive the failure path, which is the whole reason
    it is a try/finally and not a trailing statement."""
    ended = []

    def routed(method, url, body=None, **kw):
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
        if (method, path) == ("POST", "/spaces/create"):
            return 200, {"space_id": "space-x"}
        if (method, path) == ("GET", "/spaces"):
            raise RuntimeError("the listing route blew up mid-run")
        if path.endswith("/end"):
            ended.append(path)
            return 200, {"ok": True}
        return 404, {}

    with pytest.raises(RuntimeError):
        space_lifecycle_step("http://h", routed, root=tmp_path, host_fqid="qa@skworld.io")
    assert ended == ["/spaces/space-x/end"], "a crashed assertion must still end the Space"


def test_a_space_that_cannot_be_ended_is_a_problem(tmp_path):
    def routed(method, url, body=None, **kw):
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
        if (method, path) == ("POST", "/spaces/create"):
            return 200, {"space_id": "space-y"}
        if (method, path) == ("GET", "/spaces"):
            return 200, {"spaces": [{"space_id": "space-y"}]}
        if path.endswith("/end"):
            raise ConnectionResetError("the end call never landed")
        return 404, {}

    step = space_lifecycle_step("http://h", routed, root=tmp_path, host_fqid="qa@skworld.io")
    assert not step.ok and step.failure_class == "space_not_ended"
    assert step.failure_class in lane_mod.PROBLEM_CLASSES
    assert step.meta["leaked"] is True


def test_a_killed_run_leaves_a_reap_record_the_next_run_drains(tmp_path):
    """Layer (b) and (c) of the teardown guarantee: the id is written before
    the create call, so a process killed outright still leaves something the
    next run ends."""

    def dies_after_create(method, url, body=None, **kw):
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
        if (method, path) == ("POST", "/spaces/create"):
            raise KeyboardInterrupt("the run was killed right here")
        return 404, {}

    with pytest.raises(KeyboardInterrupt):
        space_lifecycle_step("http://h", dies_after_create, root=tmp_path, host_fqid="qa@x")

    pending = json.loads((tmp_path / "pending-spaces.json").read_text())
    assert len(pending) == 1 and pending[0]["space_id"].startswith("space-")

    ended = []

    def next_run(method, url, body=None, **kw):
        ended.append(url)
        return 200, {"ok": True}

    reaped = reap_pending_spaces(tmp_path, next_run)
    assert reaped == [pending[0]["space_id"]]
    assert not (tmp_path / "pending-spaces.json").exists()


def test_reaping_a_space_that_is_already_gone_clears_the_record(tmp_path):
    lane_mod.note_pending_space(tmp_path, "http://h", "space-gone", "qa@x")
    reaped = reap_pending_spaces(tmp_path, lambda *a, **k: (404, {}))
    assert reaped == ["space-gone"]
    assert not (tmp_path / "pending-spaces.json").exists()


def test_a_dead_daemon_keeps_the_reap_record_for_next_time(tmp_path):
    lane_mod.note_pending_space(tmp_path, "http://h", "space-later", "qa@x")

    def refused(*_a, **_k):
        raise ConnectionRefusedError("daemon down")

    assert reap_pending_spaces(tmp_path, refused) == []
    assert (tmp_path / "pending-spaces.json").exists()


def test_unconfigured_livekit_is_a_skip_not_a_failure(tmp_path):
    step = space_lifecycle_step(
        "http://h",
        lambda *a, **k: (503, {"detail": "livekit not configured"}),
        root=tmp_path,
        host_fqid="qa@x",
    )
    assert step.ok and step.meta["skipped"] is True
    assert not (tmp_path / "pending-spaces.json").exists()


def test_the_default_walk_never_touches_a_space_route(tmp_path):
    """Safety rule 2: the default lane gets its smoke value from the JSON
    list and the app shell, and creates nothing."""
    http = FakeHttp(_spaces_ok())
    page = FakePage()
    run_lane(
        base_url="http://h",
        root=tmp_path,
        settle_s=0.0,
        page_factory=lambda: page,
        http_json=http,
        grade_fn=_ok_grade,
    )
    assert [c for c in http.calls if c[0] == "POST"] == []
    assert page.visited == ["http://h/app/"]


# ---------------------------------------------------------------- severity --


def _result(steps, *, grade=None, console=(), gaps=()):
    return LaneResult(
        run_id="r",
        started_at="2026-08-16T06:00:00Z",
        steps=list(steps),
        grade=grade,
        console_errors=list(console),
        gaps=list(gaps),
    )


def test_clean_run_is_info():
    sev, summary = compute_severity(_result([StepResult("app.shell", True, "ok")]))
    assert sev == "info" and "clean" in summary


def test_blank_screen_is_a_problem():
    sev, _ = compute_severity(
        _result([StepResult("app.shell", False, "blank", failure_class="blank_screen")])
    )
    assert sev == "problem"


def test_a_console_error_alone_is_only_notable():
    sev, _ = compute_severity(
        _result([StepResult("app.shell", True, "ok")], console=["[exception] boom"])
    )
    assert sev == "notable"


def test_expected_auth_challenges_do_not_move_the_needle():
    """The lane runs with its own throwaway profile and no operator session,
    so the app's first calls are answered with an auth challenge. Counting
    those would put a notable line in the digest every morning for behaving
    correctly."""
    noise = [
        "[network] Failed to load resource: the server responded with a status of 401 (Unauthorized)"
    ] * 7
    assert lane_mod.significant_console_errors(noise) == []
    assert compute_severity(_result([StepResult("app.shell", True, "ok")], console=noise))[0] == (
        "info"
    )


def test_an_uncaught_exception_still_counts_through_the_auth_noise():
    """The bug class this lane exists for surfaces as an uncaught exception,
    so the noise filter must never swallow one."""
    mixed = [
        "[network] Failed to load resource: the server responded with a status of 401 (Unauthorized)",
        "[exception] Uncaught: TypeError: null is not a subtype of String",
    ]
    assert len(lane_mod.significant_console_errors(mixed)) == 1
    sev, summary = compute_severity(_result([StepResult("app.shell", True, "ok")], console=mixed))
    assert sev == "notable" and "1 console error" in summary


def test_a_slow_boot_is_only_notable():
    slow = StepResult("app.shell", True, "ok", duration_ms=lane_mod.SLOW_BOOT_MS + 1)
    assert compute_severity(_result([slow]))[0] == "notable"


def test_a_model_fail_verdict_can_never_manufacture_a_problem():
    """A ``problem`` files a GTD item and can escalate to a staged card. A
    model having a bad morning must not do that."""
    failing = grade_mod.BrowserGrade(
        graded=True,
        scores={"rendered": 2, "coherent": 2, "clean_console": 2},
        overall=2,
        verdict="fail",
    )
    sev, summary = compute_severity(_result([StepResult("app.shell", True, "ok")], grade=failing))
    assert sev == "notable"
    assert "2/5" in summary


def test_a_missing_cdp_browser_is_only_notable(tmp_path):
    """The lane's own tooling being absent is not the application being
    broken, exactly like a watchdog source being unavailable."""

    def no_browser():
        raise cdp_mod.CdpError("nothing listening on 9232")

    result = run_lane(
        base_url="http://h",
        root=tmp_path,
        settle_s=0.0,
        page_factory=no_browser,
        http_json=FakeHttp(_spaces_ok()),
        grade_fn=_boom,
    )
    assert result.severity == "notable"
    assert any("no CDP browser" in gap for gap in result.gaps)


def test_an_ungraded_run_is_a_noted_gap_never_a_fabricated_verdict(tmp_path):
    result = run_lane(
        base_url="http://h",
        root=tmp_path,
        settle_s=0.0,
        page_factory=lambda: FakePage(),
        http_json=FakeHttp(_spaces_ok()),
        grade_fn=_skipped_grade,
    )
    assert result.grade is not None and result.grade.graded is False
    assert result.grade.verdict == "" and result.grade.overall is None
    assert any("No verdict was invented" in gap for gap in result.gaps)
    assert result.severity == "notable"


# ---------------------------------------------------------------- artifact --


def test_the_run_writes_the_artifact_contract(tmp_path):
    page = FakePage(console=[cdp_mod.ConsoleEntry(level="error", text="boom", source="exception")])
    result = run_lane(
        base_url="http://h",
        root=tmp_path,
        settle_s=0.0,
        cdp_port=9232,
        page_factory=lambda: page,
        http_json=FakeHttp(_spaces_ok()),
        grade_fn=_ok_grade,
        clock=lambda: datetime(2026, 8, 16, 6, 0, 0, tzinfo=timezone.utc),
    )
    run_dir = Path(result.artifact_dir)
    doc = json.loads((run_dir / "result.json").read_text())

    assert doc["artifact_version"] == lane_mod.ARTIFACT_VERSION
    assert doc["run_id"].startswith("20260816T060000Z-")
    assert doc["finished_at"] == "2026-08-16T06:00:00Z"
    assert doc["severity"] in ("info", "notable", "problem")
    assert doc["base_url"] == "http://h" and doc["cdp_port"] == 9232
    assert doc["grade"]["graded"] is True and doc["grade"]["verdict"] == "pass"
    assert doc["grade"]["rubric_ref"] == grade_mod.RUBRIC_REF
    assert doc["console_errors"] == ["[exception] boom"]
    assert doc["notes"] == [] and doc["gaps"] == []

    shell = next(s for s in doc["steps"] if s["name"] == "app.shell")
    assert (run_dir / shell["evidence"]["screenshot"]).exists()
    assert (run_dir / "console.log").exists()

    # latest.json is the same document, so a reader that wants only the
    # newest run never has to sort directories.
    assert json.loads((tmp_path / "latest.json").read_text()) == doc


def test_run_ids_sort_chronologically(tmp_path):
    ids = []
    for hour in (7, 6, 8):
        result = run_lane(
            base_url="http://h",
            root=tmp_path,
            settle_s=0.0,
            page_factory=lambda: FakePage(),
            http_json=FakeHttp(_spaces_ok()),
            grade_fn=_ok_grade,
            clock=lambda h=hour: datetime(2026, 8, 16, h, 0, 0, tzinfo=timezone.utc),
        )
        ids.append(result.run_id)
    assert sorted(ids) == [ids[1], ids[0], ids[2]]


def test_old_runs_are_pruned(tmp_path):
    for i in range(5):
        d = tmp_path / f"20260816T0{i}0000Z-aaaaaaaa"
        d.mkdir(parents=True)
        (d / "result.json").write_text("{}")
    lane_mod.prune_runs(tmp_path, keep=2)
    remaining = sorted(p.parent.name for p in tmp_path.glob("*/result.json"))
    assert remaining == ["20260816T030000Z-aaaaaaaa", "20260816T040000Z-aaaaaaaa"]


def test_the_page_is_closed_even_when_a_step_explodes(tmp_path):
    page = FakePage()

    def explode():
        raise RuntimeError("reading the console blew up")

    page.console = explode

    with pytest.raises(RuntimeError):
        run_lane(
            base_url="http://h",
            root=tmp_path,
            settle_s=0.0,
            page_factory=lambda: page,
            http_json=FakeHttp(_spaces_ok()),
            grade_fn=_ok_grade,
        )
    assert page.closed is True


def test_a_grader_that_raises_still_leaves_an_artifact(tmp_path):
    """``grade_screenshot`` contracts never to raise, but a surprise there
    must not cost the whole run its artifact."""

    def explode(*_a, **_k):
        raise RuntimeError("grading blew up")

    result = run_lane(
        base_url="http://h",
        root=tmp_path,
        settle_s=0.0,
        page_factory=lambda: FakePage(),
        http_json=FakeHttp(_spaces_ok()),
        grade_fn=explode,
    )
    assert result.grade is not None and result.grade.graded is False
    assert "grader_raised" in result.grade.skip_reason
    assert result.severity == "notable"
    assert (Path(result.artifact_dir) / "result.json").exists()


# ------------------------------------------------------------- the proof ----


def test_no_test_reaches_a_browser_or_the_network(monkeypatch, tmp_path):
    """Break the network and process-spawn primitives underneath a FULL lane
    run. If any part of the lane reached for a real browser, a real daemon,
    or a real model, this run raises instead of returning a result."""

    def refuse(*_a, **_k):
        raise AssertionError("a test opened a socket")

    def no_spawn(*_a, **_k):
        raise AssertionError("a test spawned a process")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    monkeypatch.setattr(subprocess, "run", no_spawn)

    page = FakePage()
    result = run_lane(
        base_url="http://h",
        root=tmp_path,
        settle_s=0.0,
        with_space=True,
        page_factory=lambda: page,
        http_json=FakeHttp(_spaces_ok() | _space_routes("space-derived")),
        grade_fn=_ok_grade,
    )
    assert result.severity in ("info", "notable")
    assert page.visited == ["http://h/app/"]
