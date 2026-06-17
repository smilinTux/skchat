from skchat.spaces.federation.focus import Membership, select_focus


def test_oldest_membership_wins():
    ms = [
        Membership(fqid="b@h2", foci_preferred="sfu-2", issued_at=200),
        Membership(fqid="a@h1", foci_preferred="sfu-1", issued_at=100),  # oldest
        Membership(fqid="c@h3", foci_preferred="sfu-3", issued_at=300),
    ]
    assert select_focus(ms) == "sfu-1"


def test_tie_breaks_deterministically_by_fqid():
    ms = [
        Membership(fqid="b@h", foci_preferred="sfu-b", issued_at=100),
        Membership(fqid="a@h", foci_preferred="sfu-a", issued_at=100),
    ]
    assert select_focus(ms) == "sfu-a"  # same ts -> lowest fqid


def test_ignores_memberships_without_a_focus():
    ms = [
        Membership(fqid="a@h", foci_preferred="", issued_at=50),  # no focus
        Membership(fqid="b@h", foci_preferred="sfu-b", issued_at=80),
    ]
    assert select_focus(ms) == "sfu-b"


def test_empty_returns_none():
    assert select_focus([]) is None


# ── QA Area 3: focus selection edge cases ────────────────────────────────────


def test_all_memberships_without_focus_returns_none():
    ms = [
        Membership(fqid="a@h", foci_preferred="", issued_at=10),
        Membership(fqid="b@h", foci_preferred="", issued_at=20),
    ]
    assert select_focus(ms) is None


def test_single_membership_with_focus_wins():
    ms = [Membership(fqid="solo@h", foci_preferred="sfu-only", issued_at=500)]
    assert select_focus(ms) == "sfu-only"


def test_oldest_wins_even_when_listed_last():
    # ordering of the input list must not affect the deterministic winner
    ms = [
        Membership(fqid="z@h", foci_preferred="sfu-z", issued_at=999),
        Membership(fqid="y@h", foci_preferred="sfu-y", issued_at=999),
        Membership(fqid="a@h", foci_preferred="sfu-a", issued_at=1),  # oldest, last-ish
    ]
    assert select_focus(ms) == "sfu-a"


def test_focusless_oldest_is_skipped_for_younger_with_focus():
    # An older membership with NO focus must be ignored in favour of a younger
    # one that does declare a focus (the oldest VALID membership wins).
    ms = [
        Membership(fqid="a@h", foci_preferred="", issued_at=1),  # oldest, invalid
        Membership(fqid="b@h", foci_preferred="sfu-b", issued_at=50),
        Membership(fqid="c@h", foci_preferred="sfu-c", issued_at=60),
    ]
    assert select_focus(ms) == "sfu-b"
