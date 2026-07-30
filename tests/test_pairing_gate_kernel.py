"""M2 flip: PairingGate delegating to the capauth.pairing kernel is byte-identical.

Mirrors the scenarios in test_pairing_gate.py, but against a kernel-backed gate,
so the flip to capauth.pairing is proven to preserve every (ok, reason) outcome.
"""

import time

from skchat.pairing_gate import PairingGate, get_gate, kernel_enabled


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _kernel_gate(
    *,
    window_ttl=300.0,
    max_accepts_per_window=3,
    throttle_window=60.0,
    max_attempts_per_throttle=10,
    now=time.time,
):
    from capauth.pairing import PairingWindow

    win = PairingWindow(
        window_ttl=window_ttl,
        max_accepts=max_accepts_per_window,
        throttle_window=throttle_window,
        max_attempts_per_throttle=max_attempts_per_throttle,
        now=now,
    )
    win.close()  # a fresh gate starts closed
    return PairingGate(kernel=win), win


def test_kernel_closed_by_default_rejects():
    g, _ = _kernel_gate()
    ok, reason = g.check("anything")
    assert not ok and "not open" in reason


def test_kernel_open_window_allows_matching_nonce():
    g, _ = _kernel_gate(now=_Clock())
    info = g.open_window()
    assert g.is_open()
    assert set(info) == {"nonce", "expires_at", "ttl"}  # legacy shape exactly
    assert g.check(info["nonce"])[0]


def test_kernel_wrong_and_none_nonce_rejected():
    g, _ = _kernel_gate()
    g.open_window()
    assert g.check("not-the-nonce") == (False, "invalid or missing pairing nonce")
    assert g.check(None) == (False, "invalid or missing pairing nonce")


def test_kernel_window_expires():
    clk = _Clock()
    g, _ = _kernel_gate(window_ttl=300, now=clk)
    info = g.open_window()
    clk.t += 301
    assert not g.is_open()
    assert g.check(info["nonce"]) == (False, "pairing window not open")


def test_kernel_accept_cap_auto_closes():
    g, _ = _kernel_gate(max_accepts_per_window=2, now=_Clock())
    nonce = g.open_window()["nonce"]
    assert g.check(nonce)[0]
    g.consume()
    assert g.check(nonce)[0]
    g.consume()  # hits cap -> auto-close
    assert not g.is_open()
    assert not g.check(nonce)[0]


def test_kernel_rate_limit_and_slide():
    clk = _Clock()
    g, _ = _kernel_gate(max_attempts_per_throttle=3, throttle_window=60, now=clk)
    g.open_window()
    for _ in range(5):
        g.check("x")
    assert g.check("x") == (False, "rate limited: too many pairing attempts")
    clk.t += 61
    assert "rate limited" not in g.check("x")[1]


def test_kernel_rate_limit_before_window_open():
    g, _ = _kernel_gate(max_attempts_per_throttle=3, throttle_window=60)
    for _ in range(4):
        g.check("guess")
    assert g.check("guess") == (False, "rate limited: too many pairing attempts")


def test_kernel_reopen_rotates_nonce():
    g, _ = _kernel_gate()
    n1 = g.open_window()["nonce"]
    n2 = g.open_window()["nonce"]
    assert n1 != n2
    assert g.check(n1)[0] is False
    assert g.check(n2)[0] is True


def test_kernel_explicit_close_revokes():
    g, _ = _kernel_gate()
    info = g.open_window()
    assert g.check(info["nonce"])[0] is True
    g.close()
    assert g.check(info["nonce"]) == (False, "pairing window not open")


def test_kernel_accept_cap_reason_when_not_auto_closed():
    g, win = _kernel_gate(max_accepts_per_window=2, now=_Clock())
    info = g.open_window()
    win._accepts = 2  # force the cap without closing (concurrent-accept case)
    assert g.check(info["nonce"]) == (False, "pairing window accept limit reached")


def test_get_gate_uses_kernel_by_default(monkeypatch):
    # The M2 flip: get_gate() is kernel-backed unless explicitly disabled.
    import skchat.pairing_gate as pg

    monkeypatch.delenv("SKCHAT_PAIRING_KERNEL", raising=False)
    monkeypatch.setattr(pg, "_gate", None)
    assert kernel_enabled() is True
    assert get_gate()._kernel is not None


def test_kernel_can_be_disabled(monkeypatch):
    import skchat.pairing_gate as pg

    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL", "0")
    monkeypatch.setattr(pg, "_gate", None)
    assert kernel_enabled() is False
    assert get_gate()._kernel is None
