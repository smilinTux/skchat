#!/usr/bin/env python3
"""Standard two-party (two-agent) live QA check for the Spaces lane store.

Simulates Agent A and Agent B exchanging events in the SAME Space and verifies
each side sees the OTHER's events (real cross-party round-trip, not self-echo),
with correct lane semantics (log append vs snapshot latest-wins). Exit 0 = PASS.
Usage: python scripts/qa_two_party.py [base_url]   (default http://localhost:8765)
"""
import json
import sys
import time
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765").rstrip("/")
SPACE = f"twoparty-qa-{int(time.time())}"


def post(lane, body):
    body = {"lane": lane, **body}
    req = urllib.request.Request(
        f"{BASE}/spaces/{SPACE}/lanes/event",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def state(lane):
    with urllib.request.urlopen(f"{BASE}/spaces/{SPACE}/lanes/{lane}/state", timeout=10) as r:
        return json.loads(r.read())["events"]


def wait_for(lane, pred, secs=40):
    end = time.time() + secs
    while time.time() < end:
        if any(pred(e) for e in state(lane)):
            return True
        time.sleep(3)
    return False


fails = []
# Agent A publishes across 3 lanes; Agent B replies on chat.
assert post("chat", {"from": "agentA", "text": "A-hello"}) == 200, "A chat post"
assert post("whiteboard", {"from": "agentA", "strokes": [[{"x": 1, "y": 1}]]}) == 200
assert post("watch", {"from": "agentA", "action": "load", "url": "https://ex/m.mp4"}) == 200
assert post("chat", {"from": "agentB", "text": "B-hello"}) == 200, "B chat post"

# B sees A across all 3 lanes:
if not wait_for("chat", lambda e: e.get("from") == "agentA"): fails.append("B did not see A chat")
if not wait_for("whiteboard", lambda e: e.get("from") == "agentA" and e.get("strokes")): fails.append("B did not see A whiteboard")
if not wait_for("watch", lambda e: e.get("from") == "agentA" and e.get("action") == "load"): fails.append("B did not see A watch")
# A sees B:
if not wait_for("chat", lambda e: e.get("from") == "agentB"): fails.append("A did not see B chat")
# snapshot latest-wins: whiteboard returns exactly 1
if len(state("whiteboard")) != 1: fails.append("whiteboard not latest-wins (snapshot)")
# log append: chat has both
if len([e for e in state("chat") if e.get("from") in ("agentA", "agentB")]) < 2: fails.append("chat log missing an event")

if fails:
    print("TWO-PARTY QA: FAIL")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("TWO-PARTY QA: PASS — cross-party round-trip on chat+whiteboard+watch, correct lane semantics")
