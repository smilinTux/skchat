"""Regression pin: skchat never enrolls a non-canonical subject in capauth.

Why this file exists
--------------------
``sk-standards/standards/IDENTITY_NAMING_STANDARD.md`` (ratified 2026-08-14)
defines ONE subject grammar, the fqid: ``<agent>@<operator>.<org-domain>`` for
humans/agents/services/nodes, and ``device:<hex fingerprint>`` as the single
permitted prefixed class for device seats. capauth enforces it in
``capauth.subject.canonical_subject``, which
``capauth.pairing.enroll_device`` calls at enrollment.

skchat's fixtures had drifted to a TLD-less ``<agent>@<operator>.skworld``
spelling, and to placeholder "fingerprints" (``peerfp1``, ``op@host``) that are
neither hex nor fqid-shaped, so no alias row and no shorthand could rescue
them. Renaming those strings alone
would leave the door open, because ``skchat.pairing_mirror`` is deliberately
best-effort: every capauth error is logged and swallowed so a mirror failure
can never break live guest admission. A refused subject therefore fails
SILENTLY, with an empty capauth store and no exception, which is precisely the
shape that let the drift sit unnoticed. This file pins that shape.

Each assertion carries its own negative control: the SAME call path with a
canonical subject must succeed, so a green bar proves the subject SHAPE was
refused rather than the test plumbing being broken.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from skchat.pairing_mirror import mirror_admission

#: Canonical device seat: the ONE permitted prefixed class.
CANONICAL_DEVICE = "device:" + "b7" * 20

#: The same fingerprint with the ``device:`` prefix missing, i.e. what
#: ``mirror_admission`` is handed today by ``guest_accept.record_admission``.
#: capauth resolves this ONE shape as shorthand at enrollment (see the test
#: below); it is not a second legal spelling of a subject.
BARE_FINGERPRINT = "b7" * 20

#: Canonical agent/operator fqid.
CANONICAL_FQID = "regress@example.skworld.io"

#: The same identity with the org-domain TLD missing. The standard's
#: legacy-shape table is explicit that a missing domain is not a spelling
#: variant of a valid identity, it is an invalid record.
TLD_LESS_FQID = "regress@example.skworld"


@pytest.fixture
def kernel_base(tmp_path, monkeypatch):
    """Point the pairing mirror at a tmp capauth root, kernel ON."""
    monkeypatch.delenv("SKCHAT_PAIRING_KERNEL", raising=False)  # default ON
    base = str(tmp_path / "capauth")
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL_BASE", base)
    return base


def _devices(subject: str, base: str):
    from capauth.pairing import list_devices

    return list_devices(subject=subject, base_dir=base, include_revoked=True)


def test_canonical_normalizer_refuses_a_tld_less_subject():
    """capauth refuses the TLD-less shape outright, and accepts the canonical one."""
    from capauth.exceptions import SubjectNamingError
    from capauth.subject import canonical_subject

    with pytest.raises(SubjectNamingError):
        canonical_subject(TLD_LESS_FQID)

    # Negative control: the only difference is the missing org-domain tail.
    assert canonical_subject(CANONICAL_FQID) == CANONICAL_FQID


def test_a_bare_fingerprint_is_shorthand_at_enrollment_not_a_subject():
    """Pin the ONE deliberate exception, so nobody "hardens" it away by accident.

    ``canonical_subject`` alone refuses a bare, unprefixed fingerprint: under
    the grammar a device seat is ``device:<hex>`` and nothing else. But
    ``enroll_device`` resolves that one shape before calling the normalizer
    (``capauth.pairing.kernel._resolve_subject``), on purpose and by name,
    because ``skchat.pairing_mirror.mirror_admission`` presents
    ``subject=peer_fp`` and a bare hex fingerprint is unambiguous under the
    grammar. The two behaviours differ deliberately; assert BOTH so a change
    to either is visible here rather than as a silent empty store.
    """
    from capauth.exceptions import SubjectNamingError
    from capauth.pairing.kernel import _resolve_subject
    from capauth.subject import canonical_subject

    with pytest.raises(SubjectNamingError):
        canonical_subject(BARE_FINGERPRINT)

    assert canonical_subject(CANONICAL_DEVICE) == CANONICAL_DEVICE
    assert _resolve_subject(BARE_FINGERPRINT, "") == CANONICAL_DEVICE


def test_mirror_admission_enrolls_nothing_for_a_tld_less_subject(kernel_base):
    """The mirror's swallow-everything contract must not become a silent back door.

    The call is a no-op rather than a raise (best-effort by design), so the
    assertion has to be on the STORE, not on an exception. A TLD-less fqid has
    no alias row and no shorthand, so nothing may be enrolled under any
    spelling of it.
    """
    mirror_admission(TLD_LESS_FQID, CANONICAL_FQID, "PUBKEY-ARMOR")

    assert _devices(TLD_LESS_FQID, kernel_base) == []
    # It must not have landed under a "helpfully repaired" spelling either.
    assert _devices(CANONICAL_FQID, kernel_base) == []


def test_mirror_admission_enrolls_a_canonical_subject(kernel_base):
    """Negative control for the test above: the same call path DOES enrol a
    canonical device seat, so an empty store there means the shape was refused."""
    mirror_admission(CANONICAL_DEVICE, CANONICAL_FQID, "PUBKEY-ARMOR")

    devices = _devices(CANONICAL_DEVICE, kernel_base)
    assert len(devices) == 1
    assert devices[0].subject == CANONICAL_DEVICE


def test_tld_less_fqid_literals_do_not_increase():
    """A migration ratchet, not a clean-slate guard.

    49 test files still carry a TLD-less `@<operator>.skworld` literal. They
    resolve today only because capauth aliases that one domain, which
    IDENTITY_NAMING_STANDARD section 2.6 now permits as a DATED migration alias
    with a removal date. When that alias is removed, every one of these breaks.

    Asserting zero today would land a gate that starts red, which
    DOCS_FRESHNESS_STANDARD section 2 forbids. So this pins the CURRENT count as
    a ceiling: the number may fall as the migration proceeds, and this test fails
    if anyone adds a new one. Lower the ceiling as you migrate; the target is 0.
    """
    root = pathlib.Path(__file__).resolve().parent
    hits = set()
    for f in sorted(root.glob("*.py")):
        if f.name == pathlib.Path(__file__).name:
            continue
        if re.search(
            r"@[a-z-]+\.skworld(?![.a-z])", f.read_text(encoding="utf-8", errors="replace")
        ):
            hits.add(f.name)
    assert len(hits) <= 49, (
        f"TLD-less fqid literals INCREASED to {len(hits)} (ceiling 49). "
        f"New offenders are not in the migration baseline: {sorted(hits)}. "
        "Write canonical `@<operator>.<org-domain>` in new tests; see "
        "IDENTITY_NAMING_STANDARD section 2.6."
    )
