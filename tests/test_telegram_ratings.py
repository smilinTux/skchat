"""Tests for the Telegram answer-rating store (coord c87faa13, data half).

Covers record_send / record_rating / aggregate against a tmp JSONL via the
``SKMODELS_RATINGS`` env override, plus the shared-contract guarantees:
last-write-wins per (chat_id, msg_id), rating rows back-filling model from the
send row, score-range validation, windowing, and graceful degrade on a missing
file.
"""
from __future__ import annotations

import importlib
import json

import pytest

tr = importlib.import_module("skchat.telegram_ratings")


@pytest.fixture()
def ratings_file(tmp_path, monkeypatch):
    path = tmp_path / "ratings.jsonl"
    monkeypatch.setenv("SKMODELS_RATINGS", str(path))
    return path


def test_ratings_path_honours_env(ratings_file):
    assert tr.ratings_path() == ratings_file


def test_missing_file_degrades(ratings_file):
    # No file yet → empty aggregate, no crash.
    assert not ratings_file.exists()
    assert tr.aggregate() == {}


def test_record_send_writes_null_score_row(ratings_file):
    tr.record_send("chat1", "m1", "ornith", prompt_hash="abc", prompt_class="code")
    rows = [json.loads(line) for line in ratings_file.read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["chat_id"] == "chat1"
    assert r["msg_id"] == "m1"
    assert r["model"] == "ornith"
    assert r["prompt_class"] == "code"
    assert r["prompt_hash"] == "abc"
    assert r["score"] is None
    assert isinstance(r["ts"], float)


def test_record_rating_backfills_model_from_send(ratings_file):
    tr.record_send("chat1", "m1", "ornith", prompt_class="code")
    row = tr.record_rating("chat1", "m1", 5, note="great")
    assert row is not None
    assert row["model"] == "ornith"
    assert row["prompt_class"] == "code"
    assert row["score"] == 5
    assert row["note"] == "great"


def test_record_rating_rejects_out_of_range(ratings_file):
    tr.record_send("chat1", "m1", "ornith")
    assert tr.record_rating("chat1", "m1", 0) is None
    assert tr.record_rating("chat1", "m1", 6) is None
    # only the send row was written
    assert len(ratings_file.read_text().splitlines()) == 1


def test_aggregate_groups_by_model_and_class(ratings_file):
    tr.record_send("c", "a", "ornith", prompt_class="code")
    tr.record_send("c", "b", "ornith", prompt_class="code")
    tr.record_send("c", "x", "opus", prompt_class="code")
    tr.record_rating("c", "a", 1)
    tr.record_rating("c", "b", 2)
    tr.record_rating("c", "x", 5)

    agg = tr.aggregate()
    assert agg[("ornith", "code")] == {"n": 2, "mean": 1.5}
    assert agg[("opus", "code")] == {"n": 1, "mean": 5.0}


def test_aggregate_last_write_wins(ratings_file):
    tr.record_send("c", "a", "ornith", prompt_class="code")
    tr.record_rating("c", "a", 1)
    tr.record_rating("c", "a", 4)  # user changed their mind
    agg = tr.aggregate()
    assert agg[("ornith", "code")] == {"n": 1, "mean": 4.0}


def test_aggregate_filters(ratings_file):
    tr.record_send("c", "a", "ornith", prompt_class="code")
    tr.record_send("c", "b", "opus", prompt_class="reasoning")
    tr.record_rating("c", "a", 2)
    tr.record_rating("c", "b", 5)

    by_model = tr.aggregate(model="ornith")
    assert list(by_model.keys()) == [("ornith", "code")]

    by_class = tr.aggregate(prompt_class="reasoning")
    assert list(by_class.keys()) == [("opus", "reasoning")]


def test_aggregate_window_limits_rows(ratings_file):
    # 3 rated messages; window=1 keeps only the most recent.
    for i, score in enumerate([1, 1, 5]):
        tr.record_send("c", f"m{i}", "ornith", prompt_class="code")
        tr.record_rating("c", f"m{i}", score)
    agg = tr.aggregate(window=1)
    assert agg[("ornith", "code")] == {"n": 1, "mean": 5.0}


def test_unrated_sends_excluded(ratings_file):
    tr.record_send("c", "a", "ornith", prompt_class="code")  # never rated
    assert tr.aggregate() == {}
