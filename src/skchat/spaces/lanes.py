"""Server-side lane persistence + dispatch (Tier 2).

Lanes split into two kinds:
  - SNAPSHOT (whiteboard): the latest full-state envelope wins; replay returns it.
  - LOG (chat/watch/doc/term): append-only; replay returns recent envelopes in order.
The server is NOT in the LiveKit media path — clients mirror their data-channel
envelopes here for persistence + late-joiner catch-up."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SNAPSHOT_LANES = frozenset({"whiteboard"})
LOG_LANES = frozenset({"chat", "watch", "doc", "term"})
KNOWN_LANES = SNAPSHOT_LANES | LOG_LANES


class LaneStore:
    def __init__(self, *, db_path: Path | str) -> None:
        self._db = str(db_path)
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS lane_events (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       space_id TEXT NOT NULL,
                       lane TEXT NOT NULL,
                       payload TEXT NOT NULL,
                       ts REAL NOT NULL,
                       kind TEXT NOT NULL)"""
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_lane ON lane_events(space_id, lane, id)"
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db)

    def append(self, space_id: str, lane: str, payload: dict) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO lane_events(space_id, lane, payload, ts, kind) "
                "VALUES (?,?,?,?,?)",
                (space_id, lane, json.dumps(payload), time.time(), "log"),
            )

    def snapshot(self, space_id: str, lane: str, payload: dict) -> None:
        with self._conn() as c:
            c.execute(
                "DELETE FROM lane_events WHERE space_id=? AND lane=? AND kind='snapshot'",
                (space_id, lane),
            )
            c.execute(
                "INSERT INTO lane_events(space_id, lane, payload, ts, kind) "
                "VALUES (?,?,?,?,?)",
                (space_id, lane, json.dumps(payload), time.time(), "snapshot"),
            )

    def replay(self, space_id: str, lane: str, *, limit: int = 500) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT payload FROM lane_events WHERE space_id=? AND lane=? "
                "ORDER BY id DESC LIMIT ?",
                (space_id, lane, limit),
            ).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]
