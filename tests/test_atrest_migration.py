"""Tests for Q4 encrypted_store refactor — DEK source, back-compat, migration.

Proves:
  * the DEK is no longer fingerprint-derived (DekManager → random + hybrid-wrapped),
  * old-format (legacy fingerprint-keyed) stores still decrypt,
  * migrate old→new preserves plaintext exactly (no data loss),
  * the at-rest self-report reflects the hybrid wrap + drops the fingerprint note.
"""

from __future__ import annotations

import importlib

import pytest

pqkem = importlib.import_module("skcomms.pqkem")

pytestmark = pytest.mark.skipif(
    not pqkem.is_available(),
    reason="liboqs/oqs backend unavailable — hybrid KEM cannot run",
)

_es = importlib.import_module("skchat.encrypted_store")
ContentEncryptor = _es.ContentEncryptor
DekManager = _es.DekManager
EncryptedChatHistory = _es.EncryptedChatHistory
StorageKeyDeriver = _es.StorageKeyDeriver
ChatMessage = importlib.import_module("skchat.models").ChatMessage


class FakeHistory:
    """Minimal in-memory ChatHistory stand-in supporting the migration path."""

    def __init__(self):
        self.msgs: dict[str, ChatMessage] = {}

    def store_message(self, msg: ChatMessage) -> str:
        self.msgs[msg.id] = msg
        return msg.id

    def update_message(self, msg: ChatMessage) -> bool:
        if msg.id in self.msgs:
            self.msgs[msg.id] = msg
            return True
        return False

    def list_threads(self, limit: int = 50):
        return [{"thread_id": "t1"}]

    def get_thread_messages(self, thread_id: str, limit: int = 50):
        return [m.model_dump() for m in self.msgs.values()]


# ---------------------------------------------------------------------------
# DEK source — not fingerprint-derived
# ---------------------------------------------------------------------------


class TestDekSource:
    def test_dek_manager_creates_random_hybrid_wrapped_dek(self, tmp_path):
        mgr = DekManager(base_dir=tmp_path)
        dek = mgr.load_or_create_dek()
        assert len(dek) == 32
        # Persisted as a hybrid-wrapped blob, not a derived/plaintext key.
        assert (tmp_path / "atrest_dek.wrap").exists()
        assert (tmp_path / "atrest_recipient.key").exists()
        assert mgr.wrap_suite() == "x25519-mlkem768"

    def test_dek_stable_across_reopen(self, tmp_path):
        d1 = DekManager(base_dir=tmp_path).load_or_create_dek()
        d2 = DekManager(base_dir=tmp_path).load_or_create_dek()
        assert d1 == d2  # unwrap is deterministic

    def test_dek_not_equal_fingerprint_key(self, tmp_path):
        dek = DekManager(base_dir=tmp_path).load_or_create_dek()
        fp_key = StorageKeyDeriver.derive_key("DEADBEEF" * 5, salt=b"s" * 16)
        assert dek != fp_key

    def test_two_stores_different_deks(self, tmp_path):
        a = DekManager(base_dir=tmp_path / "a").load_or_create_dek()
        b = DekManager(base_dir=tmp_path / "b").load_or_create_dek()
        assert a != b


# ---------------------------------------------------------------------------
# Back-compat + migration (no data loss)
# ---------------------------------------------------------------------------


class TestBackCompatAndMigration:
    def _legacy_store(self, history, fingerprint, salt):
        """An EncryptedChatHistory whose CURRENT key is the legacy fingerprint key
        (simulates a store written by the old fingerprint-keyed scheme)."""
        legacy_key = StorageKeyDeriver.derive_key(fingerprint, salt=salt)
        return EncryptedChatHistory(history=history, storage_key=legacy_key)

    def test_old_format_store_still_decrypts(self, tmp_path):
        fp = "AABBCCDD" * 5
        salt = b"legacy-salt-1234"
        history = FakeHistory()

        # Write under the OLD scheme (current key == legacy fingerprint key).
        old = self._legacy_store(history, fp, salt)
        old.store_message(ChatMessage(sender="a@x", recipient="b@x", content="hello old world"))

        # Open under the NEW scheme: current DEK is random+hybrid-wrapped, but the
        # legacy key is supplied so old content is still readable.
        legacy_key = StorageKeyDeriver.derive_key(fp, salt=salt)
        new_store = EncryptedChatHistory(
            history=history,
            storage_key=DekManager(base_dir=tmp_path).load_or_create_dek(),
            legacy_key=legacy_key,
            wrap_suite="x25519-mlkem768",
        )
        msgs = new_store.get_thread_messages("t1")
        assert msgs[0]["content"] == "hello old world"
        assert "decryption_error" not in msgs[0]

    def test_migrate_preserves_plaintext_exactly(self, tmp_path):
        fp = "11223344" * 5
        salt = b"legacy-salt-5678"
        history = FakeHistory()

        plaintexts = [
            "Top secret sovereign data",
            "Sovereignty is key! staycuriousANDkeepsmilin",
            "a",  # ChatMessage forbids empty content; min non-empty case
            "unicode ✅ 🔐 ‖ test",
        ]
        old = self._legacy_store(history, fp, salt)
        for pt in plaintexts:
            old.store_message(ChatMessage(sender="a@x", recipient="b@x", content=pt))

        legacy_key = StorageKeyDeriver.derive_key(fp, salt=salt)
        new_dek = DekManager(base_dir=tmp_path).load_or_create_dek()
        store = EncryptedChatHistory(
            history=history,
            storage_key=new_dek,
            legacy_key=legacy_key,
            wrap_suite="x25519-mlkem768",
        )

        # Before migration: readable via legacy fallback.
        before = sorted(m["content"] for m in store.get_thread_messages("t1"))
        assert before == sorted(plaintexts)

        # Migrate: re-wrap every message under the new (hybrid-wrapped) DEK.
        result = store.migrate_store()
        assert result["failed"] == 0
        assert result["migrated"] == len(plaintexts)

        # After migration: content decrypts under the NEW DEK alone (no legacy).
        new_only = EncryptedChatHistory(
            history=history, storage_key=new_dek, legacy_key=None
        )
        after = sorted(m["content"] for m in new_only.get_thread_messages("t1"))
        assert after == sorted(plaintexts)  # identical plaintext, no data loss

        # And the raw stored ciphertext is NOT decryptable by the legacy key now.
        raw = list(history.msgs.values())[0].content
        b64 = raw[len(EncryptedChatHistory.ENCRYPTED_MARKER) :]
        with pytest.raises(ValueError):
            ContentEncryptor.decrypt(b64, legacy_key)

    def test_full_old_to_new_roundtrip_identical(self, tmp_path):
        """Write under old scheme → migrate → read back identical (the headline)."""
        fp = "CAFEBABE" * 5
        salt = b"roundtrip-salt00"
        history = FakeHistory()
        secret = "intimate AI-LIFE content — harvest-now-decrypt-later target"

        old = self._legacy_store(history, fp, salt)
        old.store_message(ChatMessage(sender="chef@x", recipient="lumina@x", content=secret))

        legacy_key = StorageKeyDeriver.derive_key(fp, salt=salt)
        dek = DekManager(base_dir=tmp_path).load_or_create_dek()
        store = EncryptedChatHistory(
            history=history, storage_key=dek, legacy_key=legacy_key,
            wrap_suite="x25519-mlkem768",
        )
        store.migrate_store()

        reopened = EncryptedChatHistory(history=history, storage_key=dek)
        assert reopened.get_thread_messages("t1")[0]["content"] == secret


# ---------------------------------------------------------------------------
# Self-report
# ---------------------------------------------------------------------------


class TestSelfReport:
    def test_hybrid_self_report(self, tmp_path):
        dek = DekManager(base_dir=tmp_path).load_or_create_dek()
        store = EncryptedChatHistory(
            history=FakeHistory(), storage_key=dek, wrap_suite="x25519-mlkem768"
        )
        rpt = store.crypto_self_report()
        assert rpt["surface"] == "at-rest"
        assert rpt["wrap_suite"] == "x25519-mlkem768"
        assert rpt["quantum_resistant"] is True
        assert "FIPS 203" in rpt["fips_refs"]
        # The fingerprint-keying weakness is gone — report says DEK is random.
        assert "random" in rpt["dek_source"]
        assert "fingerprint" in rpt["note"].lower()  # documents the fix
