"""F0-fqid (84fb38da): canonical FQID realm sign->verify round-trip.

The canonical federation FQID is ``<agent>@<operator>.<realm>`` (e.g.
``lumina@chef.skworld``) — the same form ``resolve_agent_identity().fqid``
emits, ``federation-trust.json`` lists, and the pinned-key filename uses. This
proves the FULL chain (real capauth signing -> keystore pin resolution ->
real verification) closes when the emitted FQID equals the pinned filename.

The capauth crypto round-trip is exercised only when the lumina capauth key is
present on the box; otherwise that one test is skipped (keeps CI green on boxes
without a populated ``~/.skcapstone``). The wiring tests below run everywhere.
"""

from __future__ import annotations

import time
import uuid
import warnings
from pathlib import Path

import pytest

from skchat.spaces.federation.assertion import (
    Assertion,
    build_signed,
    verify_signed,
)
from skchat.spaces.federation.keystore import federation_pubkey

CANONICAL_FQID = "lumina@chef.skworld"
_LUMINA_KEYS = Path.home() / ".skcapstone" / "agents" / "lumina" / "capauth" / "identity"


def test_emitted_fqid_matches_pin_filename(tmp_path):
    """The string an agent signs MUST be the string used to key its pin.

    Pin lumina's pubkey under the canonical fqid, build an assertion carrying
    that exact fqid, and confirm ``federation_pubkey`` resolves the pin via the
    fqid taken straight off the claim — no realm rewriting in between.
    """
    armor = "-----BEGIN PGP PUBLIC KEY BLOCK-----\nPUBKEY\n-----END-----\n"
    (tmp_path / f"{CANONICAL_FQID}.asc").write_text(armor)

    a = Assertion(
        fqid=CANONICAL_FQID,
        space_id="space-x",
        issued_at=int(time.time()),
        nonce=uuid.uuid4().hex,
    )
    signed = build_signed(a, sign=lambda p: "SIG(" + p.decode() + ")")

    # resolver = the real keystore, pointed at our tmp pin dir
    out = verify_signed(
        signed,
        resolve_pubkey=lambda fqid: federation_pubkey(fqid, base=tmp_path),
        verify=lambda payload, sig, pub: pub == armor
        and sig == "SIG(" + payload.decode() + ")",
    )
    assert out.fqid == CANONICAL_FQID


def test_wire_uri_form_is_not_a_valid_federation_fqid(tmp_path):
    """The capauth wire URI (``capauth:lumina@skworld.io``) must NOT be accepted
    as a federation fqid — it has the ``capauth:`` scheme and a second ``@`` is
    absent but the ``:`` + wrong realm means it would never match a pin. Guards
    against a caller accidentally passing ``capauth_uri`` instead of ``fqid``.
    """
    (tmp_path / f"{CANONICAL_FQID}.asc").write_text("KEY")
    # capauth_uri form -> different realm, no pin -> deny
    a = Assertion(
        fqid="lumina@skworld.io",
        space_id="s",
        issued_at=int(time.time()),
        nonce="n",
    )
    signed = build_signed(a, sign=lambda p: "SIG")
    with pytest.raises(Exception, match="pubkey"):
        verify_signed(
            signed,
            resolve_pubkey=lambda fqid: federation_pubkey(fqid, base=tmp_path),
            verify=lambda *a, **k: True,
        )


@pytest.mark.skipif(
    not (_LUMINA_KEYS / "private.asc").is_file()
    or not (_LUMINA_KEYS / "public.asc").is_file(),
    reason="lumina capauth keys not present on this box",
)
def test_real_capauth_roundtrip_lumina(tmp_path):
    """End-to-end with REAL capauth crypto: lumina signs the canonical fqid and
    it verifies against lumina's pubkey pinned under that same fqid.
    """
    from skchat.spaces.federation.assertion import _default_sign, _default_verify

    # pin lumina's REAL pubkey under the canonical fqid
    pub = (_LUMINA_KEYS / "public.asc").read_text()
    (tmp_path / f"{CANONICAL_FQID}.asc").write_text(pub)

    a = Assertion(
        fqid=CANONICAL_FQID,
        space_id="space-real",
        issued_at=int(time.time()),
        nonce=uuid.uuid4().hex,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # pgpy self-sig / passphrase noise
        signed = build_signed(a, sign=_default_sign)
        out = verify_signed(
            signed,
            resolve_pubkey=lambda fqid: federation_pubkey(fqid, base=tmp_path),
            verify=_default_verify,
        )
    assert out.fqid == CANONICAL_FQID
    assert out.space_id == "space-real"
