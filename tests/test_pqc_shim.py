"""Tests for the optional sk_pqc re-export shim (coord 0a1f0a51).

The skchat ratchet modules (dm_ratchet, group_ratchet) carry a guarded shim:
when the published ``sk_pqc`` package is installed the module re-exports its
vetted primitives (``_SK_PQC_BACKED`` True); on boxes WITHOUT sk-pqc (e.g. .41)
the import fails and the UNCHANGED local definitions are used
(``_SK_PQC_BACKED`` False). The LIVE cross-node ratchet must be unaffected:
behaviour is byte-identical either way — these tests pin both legs.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys

import pytest


class _BlockSkPqc:
    """meta_path finder that makes ``import sk_pqc[...]`` fail (simulates .41)."""

    def find_spec(self, name, path=None, target=None):  # noqa: D401, ANN001
        if name == "sk_pqc" or name.startswith("sk_pqc."):
            raise ImportError("sk_pqc blocked for fallback test")
        return None


def _load_isolated(mod_dotted: str, *, block_skpqc: bool):
    real = sys.modules.get(mod_dotted) or importlib.import_module(mod_dotted)
    path = real.__file__
    name = "_iso_" + mod_dotted.replace(".", "_") + ("_no" if block_skpqc else "_yes")
    blocker = _BlockSkPqc() if block_skpqc else None
    saved: dict[str, object] = {}
    if blocker is not None:
        sys.meta_path.insert(0, blocker)
        for k in list(sys.modules):
            if k == "sk_pqc" or k.startswith("sk_pqc."):
                saved[k] = sys.modules.pop(k)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        # Preserve the real package so the module's relative imports resolve to
        # the installed package.
        m.__package__ = mod_dotted.rsplit(".", 1)[0]
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m
    finally:
        if blocker is not None:
            sys.meta_path.remove(blocker)
            sys.modules.update(saved)
        sys.modules.pop(name, None)


_MODS = {
    "dm_ratchet": "derive_dm_message_key",
    "group_ratchet": "derive_message_key",
}


@pytest.mark.parametrize("mod_name,symbol", list(_MODS.items()))
def test_backed_when_skpqc_present(mod_name, symbol):
    """With sk_pqc importable, the module IS the published lib (symbol identity)."""
    pytest.importorskip("sk_pqc")  # skip on boxes without sk-pqc (.41)
    pub = importlib.import_module(f"sk_pqc.{mod_name}")
    local = importlib.import_module(f"skchat.{mod_name}")
    assert getattr(local, "_SK_PQC_BACKED") is True
    assert getattr(local, symbol) is getattr(pub, symbol)


@pytest.mark.parametrize("mod_name,symbol", list(_MODS.items()))
def test_fallback_when_skpqc_absent(mod_name, symbol):
    """Simulated absence: local definitions are used and remain functional."""
    m = _load_isolated(f"skchat.{mod_name}", block_skpqc=True)
    assert getattr(m, "_SK_PQC_BACKED") is False
    assert callable(getattr(m, symbol))


def test_behavior_identical_dm_ratchet():
    """derive_dm_message_key is byte-identical backed vs fallback (LIVE ratchet)."""
    backed = importlib.import_module("skchat.dm_ratchet")
    fb = _load_isolated("skchat.dm_ratchet", block_skpqc=True)
    es = b"\x07" * 32
    assert backed.derive_dm_message_key(es, 1, 2) == fb.derive_dm_message_key(es, 1, 2)


def test_behavior_identical_group_ratchet():
    """derive_message_key is byte-identical backed vs fallback (LIVE ratchet)."""
    backed = importlib.import_module("skchat.group_ratchet")
    fb = _load_isolated("skchat.group_ratchet", block_skpqc=True)
    es = b"\x09" * 32
    assert backed.derive_message_key(es, 3, 4) == fb.derive_message_key(es, 3, 4)
