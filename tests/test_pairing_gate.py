"""PairingGate — operator window + nonce + rate limit (Funnel hardening)."""
from skchat.pairing_gate import PairingGate


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_closed_by_default_rejects():
    g = PairingGate()
    ok, reason = g.check("anything")
    assert not ok and "not open" in reason


def test_open_window_allows_matching_nonce():
    clk = _Clock()
    g = PairingGate(now=clk)
    info = g.open_window()
    assert g.is_open()
    ok, _ = g.check(info["nonce"])
    assert ok


def test_wrong_nonce_rejected():
    g = PairingGate()
    g.open_window()
    ok, reason = g.check("not-the-nonce")
    assert not ok and "nonce" in reason


def test_window_expires():
    clk = _Clock()
    g = PairingGate(window_ttl=300, now=clk)
    info = g.open_window()
    clk.t += 301
    assert not g.is_open()
    ok, reason = g.check(info["nonce"])
    assert not ok and "not open" in reason


def test_accept_cap_auto_closes_window():
    clk = _Clock()
    g = PairingGate(max_accepts_per_window=2, now=clk)
    info = g.open_window()
    nonce = info["nonce"]
    assert g.check(nonce)[0]
    g.consume()
    assert g.check(nonce)[0]
    g.consume()  # hits cap → closes
    assert not g.is_open()
    assert not g.check(nonce)[0]


def test_rate_limit_throttles_attempts():
    clk = _Clock()
    g = PairingGate(max_attempts_per_throttle=5, throttle_window=60, now=clk)
    g.open_window()
    # 5 allowed, 6th+ throttled (all within the same throttle window)
    results = [g.check("x")[0] for _ in range(8)]  # wrong nonce, but attempts still counted
    # once throttled, reason flips to rate limited
    ok, reason = g.check("x")
    assert not ok and "rate limited" in reason


def test_rate_limit_window_slides():
    clk = _Clock()
    g = PairingGate(max_attempts_per_throttle=3, throttle_window=60, now=clk)
    g.open_window()
    for _ in range(5):
        g.check("x")
    assert "rate limited" in g.check("x")[1]
    clk.t += 61  # throttle window passes
    # not rate-limited anymore (though still wrong nonce)
    ok, reason = g.check("x")
    assert "rate limited" not in reason


# ── QA Area 3: pairing gate hardening ────────────────────────────────────────


def test_rate_limit_checked_before_window_open():
    # Brute-force attempts must be throttled even before any window exists, so an
    # attacker can't probe nonces freely while the gate is closed.
    g = PairingGate(max_attempts_per_throttle=3, throttle_window=60)
    for _ in range(4):
        g.check("guess")
    ok, reason = g.check("guess")
    assert not ok and "rate limited" in reason


def test_reopening_window_rotates_nonce():
    g = PairingGate()
    n1 = g.open_window()["nonce"]
    n2 = g.open_window()["nonce"]
    assert n1 != n2
    # the OLD nonce no longer works after reopen
    assert g.check(n1)[0] is False
    assert g.check(n2)[0] is True


def test_reopening_window_resets_accept_count():
    clk = _Clock()
    g = PairingGate(max_accepts_per_window=1, now=clk)
    g.open_window()
    g.consume()                 # hits cap → auto-close
    assert not g.is_open()
    info = g.open_window()      # fresh window resets accepts
    assert g.check(info["nonce"])[0] is True


def test_explicit_close_revokes_open_window():
    g = PairingGate()
    info = g.open_window()
    assert g.check(info["nonce"])[0] is True
    g.close()
    ok, reason = g.check(info["nonce"])
    assert not ok and "not open" in reason


def test_none_nonce_rejected_when_open():
    g = PairingGate()
    g.open_window()
    ok, reason = g.check(None)
    assert not ok and "nonce" in reason


def test_accept_cap_reached_reason_when_not_auto_closed():
    # If max_accepts is high enough that consume() hasn't auto-closed yet, an
    # over-cap check still returns the accept-limit reason (defence in depth).
    clk = _Clock()
    g = PairingGate(max_accepts_per_window=2, now=clk)
    info = g.open_window()
    # force the accept counter to the cap without closing (simulate concurrent)
    g._accepts = 2
    ok, reason = g.check(info["nonce"])
    assert not ok and "accept limit" in reason
