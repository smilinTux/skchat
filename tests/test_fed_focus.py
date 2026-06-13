import pytest

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
        Membership(fqid="a@h", foci_preferred="", issued_at=50),     # no focus
        Membership(fqid="b@h", foci_preferred="sfu-b", issued_at=80),
    ]
    assert select_focus(ms) == "sfu-b"


def test_empty_returns_none():
    assert select_focus([]) is None
