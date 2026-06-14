#!/usr/bin/env python3
"""Tier-5 LIVE verification harness for the running skchat / SK Spaces stack.

Runs a battery of LIVE checks against a running skchat-webui (default
``http://localhost:8765``) and prints a PASS/FAIL table. Exits non-zero if any
check fails. This is *verification only* — it does not import or modify any app
code; it speaks to the stack the same way a browser/agent does, over HTTP.

What it checks (all live):
  * ``/health`` responds 200.
  * Spaces directory: ``GET /spaces`` (JSON list) + ``GET /spaces/live`` (HTML).
  * Lane persist + replay for every lane:
      - log lanes (chat/watch/doc/term): POST an event, GET state, assert the
        appended event round-trips as the most-recent entry.
      - snapshot lane (whiteboard): POST two snapshots, GET state, assert only
        the latest snapshot is returned (latest-wins).
  * Unknown-lane rejection: POST ``{"lane":"bogus"}`` → 400;
    GET ``/lanes/bogus/state`` → 400.

Uses a throwaway, timestamped ``space_id`` so it never collides with live data.
Stdlib only (urllib) so it runs anywhere the package runs.

Usage::
    scripts/tier5_verify.py [BASE_URL]
    BASE_URL env var also honoured (default http://localhost:8765).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_BASE = os.environ.get("TIER5_BASE", "http://localhost:8765")
TIMEOUT = float(os.environ.get("TIER5_TIMEOUT", "10"))

# Lanes and their semantics (mirrors skchat.spaces.lanes; not imported on purpose).
LOG_LANES = ["chat", "watch", "doc", "term"]
SNAPSHOT_LANES = ["whiteboard"]


@dataclass
class Result:
    name: str
    passed: bool
    detail: str


@dataclass
class Harness:
    base: str
    space_id: str
    results: list[Result] = field(default_factory=list)

    # --- low-level HTTP helpers -------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None):
        url = f"{self.base}{path}"
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
            return exc.code, raw
        except urllib.error.URLError as exc:
            return None, f"URLError: {exc.reason}"
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, body):
        return self._request("POST", path, body)

    def record(self, name: str, passed: bool, detail: str):
        self.results.append(Result(name, passed, detail))

    # --- checks ------------------------------------------------------------
    def check_health(self):
        code, raw = self.get("/health")
        ok = code == 200
        detail = f"HTTP {code}"
        if ok:
            try:
                j = json.loads(raw)
                detail += f" status={j.get('status')} svc={j.get('service')}"
            except Exception:  # noqa: BLE001
                pass
        else:
            detail += f" body={raw[:120]!r}"
        self.record("health endpoint", ok, detail)

    def check_spaces_directory(self):
        code, raw = self.get("/spaces")
        ok = code == 200
        detail = f"HTTP {code}"
        if ok:
            try:
                j = json.loads(raw)
                n = len(j.get("spaces", []))
                ok = "spaces" in j
                detail += f" spaces={n}"
            except Exception as exc:  # noqa: BLE001
                ok = False
                detail += f" json-error={exc}"
        else:
            detail += f" body={raw[:120]!r}"
        self.record("spaces directory (GET /spaces)", ok, detail)

        code2, _ = self.get("/spaces/live")
        ok2 = code2 == 200
        self.record("spaces live page (GET /spaces/live)", ok2, f"HTTP {code2}")

    def check_log_lane(self, lane: str):
        """Log lane: append an event, assert it round-trips as latest entry."""
        marker = f"tier5-{lane}-{int(time.time() * 1000)}"
        envelope = {"lane": lane, "from": "tier5", "text": marker}
        code, raw = self.post(f"/spaces/{self.space_id}/lanes/event", envelope)
        if code != 200:
            self.record(
                f"lane '{lane}' persist+replay (log)", False,
                f"POST event HTTP {code} body={raw[:120]!r}")
            return
        gcode, graw = self.get(f"/spaces/{self.space_id}/lanes/{lane}/state")
        if gcode != 200:
            self.record(
                f"lane '{lane}' persist+replay (log)", False,
                f"GET state HTTP {gcode} body={graw[:120]!r}")
            return
        try:
            events = json.loads(graw).get("events", [])
        except Exception as exc:  # noqa: BLE001
            self.record(f"lane '{lane}' persist+replay (log)", False,
                        f"state json-error={exc}")
            return
        # replay returns oldest->newest; our event must be the most recent.
        found = events and events[-1].get("text") == marker
        detail = (f"POST 200, state has {len(events)} event(s), "
                  f"latest text {'==' if found else '!='} marker")
        self.record(f"lane '{lane}' persist+replay (log)", bool(found), detail)

    def check_snapshot_lane(self, lane: str):
        """Snapshot lane: POST two snapshots, assert only the latest is returned."""
        m1 = f"tier5-{lane}-snap1-{int(time.time() * 1000)}"
        m2 = f"tier5-{lane}-snap2-{int(time.time() * 1000)}"
        c1, _ = self.post(f"/spaces/{self.space_id}/lanes/event",
                          {"lane": lane, "from": "tier5", "state": m1})
        c2, r2 = self.post(f"/spaces/{self.space_id}/lanes/event",
                           {"lane": lane, "from": "tier5", "state": m2})
        if c1 != 200 or c2 != 200:
            self.record(
                f"lane '{lane}' persist+replay (snapshot latest-wins)", False,
                f"POST snapshots HTTP {c1}/{c2} body={r2[:120]!r}")
            return
        gcode, graw = self.get(f"/spaces/{self.space_id}/lanes/{lane}/state")
        if gcode != 200:
            self.record(
                f"lane '{lane}' persist+replay (snapshot latest-wins)", False,
                f"GET state HTTP {gcode} body={graw[:120]!r}")
            return
        try:
            events = json.loads(graw).get("events", [])
        except Exception as exc:  # noqa: BLE001
            self.record(f"lane '{lane}' persist+replay (snapshot latest-wins)",
                        False, f"state json-error={exc}")
            return
        # snapshot lane keeps only the latest envelope for this space+lane.
        ok = len(events) == 1 and events[0].get("state") == m2
        detail = (f"2 snapshots posted, state has {len(events)} event(s), "
                  f"latest-wins={'yes' if ok else 'no'}")
        self.record(f"lane '{lane}' persist+replay (snapshot latest-wins)",
                    ok, detail)

    def check_unknown_lane(self):
        code, raw = self.post(f"/spaces/{self.space_id}/lanes/event",
                              {"lane": "bogus", "from": "tier5"})
        ok_post = code == 400
        self.record("unknown-lane POST rejected (400)", ok_post,
                    f"HTTP {code} body={raw[:80]!r}")

        gcode, graw = self.get(f"/spaces/{self.space_id}/lanes/bogus/state")
        ok_get = gcode == 400
        self.record("unknown-lane GET rejected (400)", ok_get,
                    f"HTTP {gcode} body={graw[:80]!r}")

    # --- driver ------------------------------------------------------------
    def run(self):
        self.check_health()
        self.check_spaces_directory()
        for lane in LOG_LANES:
            self.check_log_lane(lane)
        for lane in SNAPSHOT_LANES:
            self.check_snapshot_lane(lane)
        self.check_unknown_lane()

    def report(self) -> int:
        width = max(len(r.name) for r in self.results) + 2
        print()
        print(f"Tier-5 live verification — {self.base}")
        print(f"throwaway space_id: {self.space_id}")
        print("=" * (width + 50))
        print(f"{'CHECK':<{width}} RESULT  DETAIL")
        print("-" * (width + 50))
        passed = 0
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            if r.passed:
                passed += 1
            print(f"{r.name:<{width}} [{status}]  {r.detail}")
        print("-" * (width + 50))
        total = len(self.results)
        print(f"{passed}/{total} checks PASSED"
              + ("" if passed == total else f"  ({total - passed} FAILED)"))
        return 0 if passed == total else 1


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else DEFAULT_BASE
    base = base.rstrip("/")
    space_id = f"tier5-{int(time.time())}"
    h = Harness(base=base, space_id=space_id)
    h.run()
    return h.report()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
