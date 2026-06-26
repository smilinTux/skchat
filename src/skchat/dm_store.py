"""Sealed persistence for 1:1 DM ratchet sessions (RFC-0001 P1).

A conversation's :class:`skchat.dm_session.DmSession` must survive daemon restarts
(otherwise the ratchet resets and forward secrecy / ordering are lost). Its state
includes the **epoch secrets** — live key material — so it is **sealed at rest** with
AES-256-GCM under a caller-supplied 32-byte key; the plaintext snapshot (and thus the
epoch secrets) never touches disk. The caller owns the key (typically the agent's
at-rest DEK), so this store inherits whatever key-management the encrypted store uses.

Schema: ``dm_sessions(peer TEXT PRIMARY KEY, sealed BLOB)`` where ``sealed`` is
``nonce(12) || AES-256-GCM(snapshot-json)`` with a fixed associated-data tag.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Optional, Union

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from skchat.dm_session import DmSession

_SEAL_NONCE_LEN = 12
_SEAL_AAD = b"skchat/dm-store/v1"


class DmSessionStore:
    """SQLite-backed, AES-256-GCM-sealed store of per-peer DM ratchet sessions."""

    def __init__(self, db_path: Union[str, Path]) -> None:
        self.db_path = str(db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS dm_sessions ("
                "peer TEXT PRIMARY KEY, sealed BLOB NOT NULL)"
            )

    def save(self, session: DmSession, key: bytes) -> None:
        """Seal ``session``'s snapshot under ``key`` (32 bytes) and persist it.

        Raises:
            ValueError: if ``key`` is not 32 bytes.
        """
        if len(key) != 32:
            raise ValueError(f"key must be 32 bytes, got {len(key)}")
        plaintext = json.dumps(session.snapshot(), sort_keys=True).encode("utf-8")
        nonce = os.urandom(_SEAL_NONCE_LEN)
        sealed = nonce + AESGCM(key).encrypt(nonce, plaintext, _SEAL_AAD)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO dm_sessions (peer, sealed) VALUES (?, ?)",
                (session.peer, sealed),
            )

    def load(self, peer: str, key: bytes) -> Optional[DmSession]:
        """Return the restored session for ``peer``, or ``None`` if absent.

        Raises:
            cryptography.exceptions.InvalidTag: if ``key`` is wrong or the row was
                tampered with (AEAD authentication failure — never a silent restore).
        """
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT sealed FROM dm_sessions WHERE peer = ?", (peer,)
            ).fetchone()
        if row is None:
            return None
        sealed = row[0]
        nonce, ct = sealed[:_SEAL_NONCE_LEN], sealed[_SEAL_NONCE_LEN:]
        plaintext = AESGCM(key).decrypt(nonce, ct, _SEAL_AAD)
        return DmSession.restore(json.loads(plaintext))
