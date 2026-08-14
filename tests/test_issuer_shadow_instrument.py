"""CR-3.4 shadow instrument hardening.

Two soak-blocking gaps the Aug 12 false-divergence exposed:

  * The ok-heartbeat logged at INFO, but the webui runs uvicorn at
    ``log_level="warning"``, so the heartbeat never reached the journal and the
    Phase-2 gate ("nonzero issuer-shadow ok heartbeats; silence never passes")
    could not be observed. It must be visible under a WARNING root.
  * ``_extract_subject`` swallowed a subject-resolution error with a bare
    ``except: pass``. A transient gpg failure (a verify child killed mid-shutdown)
    then looked identical to a real unresolved subject, with no log to tell them
    apart. The swallow must leave a diagnostic breadcrumb.

Both are observation-only: they never change a request or a response.
"""

from __future__ import annotations

import logging

from skchat import dataplane_auth as dpa


def test_shadow_ok_heartbeat_is_visible_under_warning_root(caplog):
    dpa._shadow_ok_count = 0
    with caplog.at_level(logging.WARNING, logger="skchat.dataplane_auth"):
        dpa._record_shadow_ok()  # first ok: count == 1 -> heartbeat fires
    beats = [r for r in caplog.records if "issuer-shadow ok" in r.getMessage()]
    assert len(beats) == 1
    assert beats[0].levelno >= logging.WARNING


def test_shadow_ok_heartbeat_fires_every_hundredth(caplog):
    dpa._shadow_ok_count = 0
    with caplog.at_level(logging.WARNING, logger="skchat.dataplane_auth"):
        for _ in range(101):
            dpa._record_shadow_ok()
    beats = [r for r in caplog.records if "issuer-shadow ok" in r.getMessage()]
    assert len(beats) == 2  # count 1 and count 101
    assert dpa._shadow_ok_count == 101


def test_extract_subject_logs_a_swallowed_operator_error(caplog, monkeypatch):
    import skchat.operator_auth as oa

    def boom(_token):
        raise RuntimeError("gpg verify died mid-shutdown")

    monkeypatch.setattr(oa, "verify_operator_session", boom)
    with caplog.at_level(logging.DEBUG, logger="skchat.dataplane_auth"):
        result = dpa._extract_subject("not-a-valid-token")
    assert result is None
    assert any("subject resolution" in r.getMessage().lower() for r in caplog.records), (
        "a swallowed subject-resolution error must be logged, not silent"
    )
