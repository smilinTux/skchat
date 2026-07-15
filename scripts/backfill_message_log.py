#!/usr/bin/env python3
"""Backfill the authoritative MessageLog from the existing JSONL history (store A).

Run BEFORE the read-cutover (plan Task 3) so no history is lost when readers
start trusting the log. Idempotent: rerunnable, dedups the 1+N group fan-out
copies via MessageLog.record (metadata.group_id + dedup_key), so a message
already present is skipped (no second seq).

Usage:  SKCHAT_HOME=~/.skchat  python scripts/backfill_message_log.py [--limit N]
"""
from __future__ import annotations

import argparse
import sys

from skchat.history import ChatHistory
from skchat.message_log import MessageLog, conversation_id_for


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1_000_000, help="max messages to scan")
    args = ap.parse_args()

    hist = ChatHistory.from_config()
    # load() returns newest-first; reverse so we record in chronological order,
    # which keeps per-conversation seq roughly time-ordered (dedup makes exact
    # ordering non-critical, but this reads better).
    messages = list(reversed(hist.load(limit=args.limit)))
    log = MessageLog()

    recorded = 0
    deduped = 0
    conversations: set[str] = set()
    for msg in messages:
        try:
            res = log.record(msg)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip (record failed): {getattr(msg, 'id', '?')}: {exc}", file=sys.stderr)
            continue
        conversations.add(conversation_id_for(msg))
        if res.get("deduped"):
            deduped += 1
        else:
            recorded += 1

    print(
        f"backfill: scanned={len(messages)} recorded={recorded} deduped={deduped} "
        f"conversations={len(conversations)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
