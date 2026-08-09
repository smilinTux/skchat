"""Shared fixtures for SKChat tests."""

from __future__ import annotations

# skmemory is resolved via the editable install in the project venv.
# No sys.path manipulation needed — see [tool.pytest.ini_options] pythonpath
# in pyproject.toml and `pip install -e` in the dev environment.
import builtins
import os

# Suppress REAL desktop notifications during the test suite. Several code paths
# (DesktopNotifier, the daemon's message loop, the CLI --notify watcher) shell
# out to notify-send/osascript and would otherwise pop notifications on the
# developer's desktop while tests run. Default them off for the whole session;
# a test (or a developer) can opt back in with SK_DESKTOP_NOTIFY=1 in the env.
os.environ.setdefault("SK_DESKTOP_NOTIFY", "0")

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pgpy
import pytest
from pgpy.constants import (
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    SymmetricKeyAlgorithm,
)

from skchat.models import ChatMessage, ContentType, Thread

PASSPHRASE = "test-passphrase-123"


def _generate_test_keypair(name: str, email: str) -> tuple[str, str]:
    """Generate a PGP keypair for testing.

    Args:
        name: Display name for the UID.
        email: Email for the UID.

    Returns:
        tuple[str, str]: (private_armor, public_armor).
    """
    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new(name, email=email)
    key.add_uid(
        uid,
        usage={KeyFlags.Sign, KeyFlags.Certify},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
    )

    enc_subkey = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    key.add_subkey(
        enc_subkey,
        usage={KeyFlags.EncryptCommunications, KeyFlags.EncryptStorage},
    )

    key.protect(PASSPHRASE, SymmetricKeyAlgorithm.AES256, HashAlgorithm.SHA256)
    return str(key), str(key.pubkey)


@pytest.fixture(scope="session")
def alice_keys() -> tuple[str, str]:
    """Generate Alice's PGP keypair (session-scoped for speed).

    Returns:
        tuple[str, str]: (private_armor, public_armor).
    """
    return _generate_test_keypair("Alice", "alice@skworld.io")


@pytest.fixture(scope="session")
def bob_keys() -> tuple[str, str]:
    """Generate Bob's PGP keypair (session-scoped for speed).

    Returns:
        tuple[str, str]: (private_armor, public_armor).
    """
    return _generate_test_keypair("Bob", "bob@skworld.io")


@pytest.fixture()
def sample_message() -> ChatMessage:
    """A basic ChatMessage for testing.

    Returns:
        ChatMessage: Message from Alice to Bob.
    """
    return ChatMessage(
        sender="capauth:alice@skworld.io",
        recipient="capauth:bob@skworld.io",
        content="Hello from the sovereign side!",
        content_type=ContentType.PLAIN,
    )


@pytest.fixture()
def sample_thread() -> Thread:
    """A basic Thread for testing.

    Returns:
        Thread: Thread with Alice and Bob.
    """
    return Thread(
        title="Project Discussion",
        participants=["capauth:alice@skworld.io", "capauth:bob@skworld.io"],
    )


@pytest.fixture(autouse=True)
def _isolate_capauth_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point capauth's storage root at tmp_path for every test.

    ``capauth.pairing.default_base_dir()`` is hardcoded to ``~/.skcapstone``:
    there is no env override, and callers are expected to inject ``base_dir``.
    skchat's enrollment path does not (``grant_operator_prekey_capability`` and
    the unlink revoke both call capauth without one), so every enrollment test
    wrote a real pairing record into the operator's live peer store. The
    live-state guard below is what surfaced it.
    """
    # NOTE the leading dot: tests that isolate capauth by pinning Path.home()
    # (e.g. test_operator_grants.py) resolve to tmp_path/".skcapstone". Using the
    # same path means the two strategies agree instead of writing to one dir and
    # reading from the other.
    root = tmp_path / ".skcapstone"
    root.mkdir(parents=True, exist_ok=True)
    # pq_prekeys._pqc_dir() resolves off SKCHAT_HOME. Unisolated, any test that
    # exercises the daemon's prekey sync rewrites the operator's REAL peer slots.
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "skchat-home"))
    for name in ("pairing", "authz"):
        try:
            mod = __import__(f"capauth.{name}", fromlist=["default_base_dir"])
        except Exception:  # pragma: no cover - capauth optional in some envs
            continue
        if hasattr(mod, "default_base_dir"):
            monkeypatch.setattr(mod, "default_base_dir", lambda root=root: root, raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_enrollment_pairing_gate():
    """Give every test a fresh operator-enrollment pairing gate.

    ``operator_auth_routes._pairing`` is a module-level singleton carrying a rate
    limiter (10 attempts per 60s) and a single-accept window. Enrollment tests
    therefore share one budget: once enough tests in a process have enrolled, the
    next one gets "enrollment window closed or invalid" for reasons that have
    nothing to do with what it is testing. It passes locally and fails in CI,
    purely because CI runs more of the suite in one process.
    """
    try:
        from skchat import operator_auth_routes as _oar
        from skchat.pairing_gate import PairingGate

        _oar._pairing = PairingGate(max_accepts_per_window=1)
    except Exception:  # pragma: no cover - module optional in some envs
        pass
    yield


@pytest.fixture(autouse=True)
def _isolate_import_time_home_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Repoint module-level paths that were bound to the real home at IMPORT time.

    ``daemon.DAEMON_PID_FILE`` and ``cli._READ_STATE_PATH`` are computed once when
    the module is imported, so setting ``SKCHAT_HOME`` from a fixture is too late:
    the constants already point at ``~/.skchat``. The result was measurable, not
    theoretical: every full suite run DELETED the operator's live
    ``~/.skchat/daemon.pid``, which is the file systemd's ``PIDFile=`` tracks for
    the running daemon.
    """
    home = tmp_path / "skchat-home"
    home.mkdir(parents=True, exist_ok=True)
    try:
        from skchat import daemon as _daemon

        monkeypatch.setattr(_daemon, "_DAEMON_HOME", home, raising=False)
        monkeypatch.setattr(_daemon, "DAEMON_PID_FILE", home / "daemon.pid", raising=False)
    except Exception:  # pragma: no cover - module optional in some envs
        pass
    try:
        from skchat import cli as _cli

        monkeypatch.setattr(_cli, "_READ_STATE_PATH", home / "read-state.json", raising=False)
    except Exception:  # pragma: no cover
        pass
    yield


# ---------------------------------------------------------------------------
# Live-state guard: no test may write to the operator's REAL home
# ---------------------------------------------------------------------------
#: Roots holding device, key and revocation state. A test that writes under one
#: of these is operating on the operator's live node instead of tmp_path.
#: Scoped to the DEVICE/KEY state that has actually been corrupted, not all of
#: ~/.skchat. The wider net catches ~79 pre-existing offenders (history, media,
#: outbox) whose isolation debt is real but is a separate piece of work; a guard
#: that reds the suite on day one gets reverted instead of fixed.
_LIVE_STATE_ROOTS = (
    "~/.skchat/state",
    "~/.skchat/pqc",
    "~/.skchat/daemon.pid",
    "~/.skchat/read-state.json",
    "~/.skcapstone/peers",
    "~/.skcapstone/pairing",
)


def _is_live_state(target: object) -> str | None:
    """The offending absolute path if *target* is under a guarded root, else None."""
    try:
        path = Path(os.fspath(target)).expanduser()
    except (TypeError, ValueError):
        return None
    if not path.is_absolute():
        path = Path.cwd() / path
    resolved = os.path.normpath(str(path))
    for root in _LIVE_STATE_ROOTS:
        base = os.path.normpath(str(Path(root).expanduser()))
        if resolved == base or resolved.startswith(base + os.sep):
            return resolved
    return None


@pytest.fixture(autouse=True)
def _no_writes_to_the_real_home(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Fail any test that WRITES into the operator's real ~/.skchat or ~/.skcapstone.

    Twice a new on-disk store shipped without a matching isolation fixture and
    the tests silently operated on the developer's real home: once overwriting a
    genuine signed prekey slot, once leaving a live auto-approve window on the
    box. Both were caught by a human noticing, not by the suite.

    This intercepts the write primitives IN THIS PROCESS rather than diffing the
    filesystem. That distinction matters on a developer box, where the real
    skchat daemon is running and writing to the same directories continuously: a
    before/after diff cannot tell a test's write from the daemon's and false-fails
    at random. Interception attributes the write to the code that made it.

    Per-store isolation fixtures remain the fix; this is the backstop that makes
    forgetting one loud and immediate.

    Opt out with ``@pytest.mark.touches_real_home`` (nothing does today).
    """
    if request.node.get_closest_marker("touches_real_home"):
        yield
        return

    def _refuse(path: str, how: str):
        raise AssertionError(
            f"this test tried to {how} the operator's REAL live state: {path}\n"
            "Point the relevant setting at tmp_path. Note pq_prekeys resolves off "
            "SKCHAT_HOME (there is no SKCHAT_PQC_DIR), the registry uses "
            "SKCHAT_DEVICE_REGISTRY, and capauth's base dir is patched by "
            "_isolate_capauth_store."
        )

    real_open = builtins.open
    real_replace = os.replace
    real_write_text = Path.write_text
    real_write_bytes = Path.write_bytes
    real_mkdir = Path.mkdir

    def guarded_open(file, mode="r", *a, **kw):
        if any(c in mode for c in ("w", "a", "x", "+")):
            hit = _is_live_state(file)
            if hit:
                _refuse(hit, "open for writing")
        return real_open(file, mode, *a, **kw)

    def guarded_replace(src, dst, *a, **kw):
        hit = _is_live_state(dst)
        if hit:
            _refuse(hit, "os.replace onto")
        return real_replace(src, dst, *a, **kw)

    def guarded_write_text(self, *a, **kw):
        hit = _is_live_state(self)
        if hit:
            _refuse(hit, "write_text")
        return real_write_text(self, *a, **kw)

    def guarded_write_bytes(self, *a, **kw):
        hit = _is_live_state(self)
        if hit:
            _refuse(hit, "write_bytes")
        return real_write_bytes(self, *a, **kw)

    def guarded_mkdir(self, *a, **kw):
        hit = _is_live_state(self)
        if hit:
            _refuse(hit, "mkdir under")
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "replace", guarded_replace)
    monkeypatch.setattr(Path, "write_text", guarded_write_text)
    monkeypatch.setattr(Path, "write_bytes", guarded_write_bytes)
    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)
    yield


# ---------------------------------------------------------------------------
# Guest revocation/single-use store isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_skcomms_peers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate the skcomms PeerStore from machine state.

    The ChatTransport federation branch consults the skcomms PeerStore (peers
    with an ``https-s2s`` inbox_url route via ``send_federated``). On a real node
    that store holds seeded federation peers (lumina/jarvis), which would make
    unit-test sends to those names take the federation path. Point PeerStore at a
    fresh empty tmp dir so federation resolution is a no-op unless a test opts in.
    """
    try:
        import skcomms.discovery as _disc

        peers_home = tmp_path / "skcomms_iso"
        (peers_home / "peers").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(_disc, "SKCOMMS_HOME", str(peers_home), raising=False)
    except Exception:  # pragma: no cover — skcomms optional in some envs
        pass
    yield


@pytest.fixture(autouse=True)
def _isolate_guest_revocation_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the guest SQLite store at a per-test tmp DB and reset its cache.

    Keeps every test (including the legacy ``test_guest.py`` revocation cases)
    from touching the real ``~/.skchat/guest_revocations.db`` and prevents
    revocation state from leaking between tests via the in-memory cache.
    """
    db = tmp_path / "guest_revocations.db"
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(db))
    try:
        from skchat import guest as _guest

        _guest._reset_revocation_cache()
        _guest._reset_device_revocation_cache()
    except Exception:  # pragma: no cover — guest module import-time guard
        pass
    yield
    try:
        from skchat import guest as _guest

        _guest._reset_revocation_cache()
        _guest._reset_device_revocation_cache()
    except Exception:  # pragma: no cover
        pass


@pytest.fixture(autouse=True)
def _isolate_device_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the device registry at a per-test tmp file and clear its throttle map.

    ``touch_throttled()`` runs on every authenticated dataplane request (Task 4's
    session stash calls it), and it reads/writes the device registry. Without
    this fixture, any test that authenticates with a real operator session under
    ``SKCHAT_DATAPLANE_AUTH=1`` but does not set ``SKCHAT_DEVICE_REGISTRY`` itself
    would stat the developer's real ``~/.skchat/state/operator_device_registry.json``.
    Also clears the in-memory throttle map so a warm entry from an earlier test
    cannot suppress a touch a later test expects.
    """
    registry = tmp_path / "operator_device_registry.json"
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(registry))
    # Same hazard, same fix: `devices reset` opens a bootstrap auto-approve
    # window, and an enrollment CONSUMES one. Unisolated, a CLI test writes a
    # real window into the developer's ~/.skchat/state and a later enrollment
    # test silently rides it, changing that test's outcome and leaving live
    # auto-approve state behind. Observed exactly that on 2026-08-08.
    monkeypatch.setenv("SKCHAT_BOOTSTRAP_WINDOW", str(tmp_path / "bootstrap_window.json"))
    try:
        from skchat import device_registry as _device_registry

        _device_registry._last_touch.clear()
    except Exception:  # pragma: no cover (device_registry module import-time guard)
        pass
    yield
    try:
        from skchat import device_registry as _device_registry

        _device_registry._last_touch.clear()
    except Exception:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Directory + environment fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_skchat_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temporary ~/.skchat directory with standard sub-dirs and env vars set.

    Creates history/, groups/, and presence/ under *tmp_path* and points
    SKCHAT_IDENTITY + SKCHAT_HOME at the temp location so no test ever
    touches the real user home.

    Args:
        tmp_path: Pytest-provided unique temporary directory.
        monkeypatch: Pytest monkeypatch fixture for env-var isolation.

    Returns:
        Path: Root of the temporary skchat home (tmp_path).
    """
    for subdir in ("history", "groups", "presence"):
        (tmp_path / subdir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("SKCHAT_IDENTITY", "capauth:test-agent@skworld.io")
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))

    return tmp_path


# ---------------------------------------------------------------------------
# Transport fixture
# ---------------------------------------------------------------------------


class _CapturingTransport:
    """ChatTransport stub that records every outbound message.

    Implements the same public surface as :class:`skchat.transport.ChatTransport`
    so tests can inject it as a drop-in replacement without hitting any real
    network or filesystem transport.

    Attributes:
        sent: All messages passed to :meth:`send_message` or
            :meth:`send_and_store`, in call order.
        identity: Simulated sender identity URI.
    """

    IDENTITY = "capauth:test-agent@skworld.io"

    def __init__(self) -> None:
        self.sent: list[ChatMessage] = []
        self._poll_queue: list[ChatMessage] = []
        self.identity: str = self.IDENTITY

    # ------------------------------------------------------------------
    # ChatTransport API
    # ------------------------------------------------------------------

    def send_message(
        self,
        message: ChatMessage,
        recipient_public_armor: Optional[str] = None,
    ) -> dict:
        """Capture *message* and return a successful delivery report.

        Args:
            message: The ChatMessage to capture.
            recipient_public_armor: Ignored by the stub.

        Returns:
            dict: Synthetic delivery report with ``delivered=True``.
        """
        self.sent.append(message)
        return {
            "delivered": True,
            "message_id": message.id,
            "recipient": message.recipient,
            "transport": "mock",
        }

    def send_and_store(
        self,
        recipient: str,
        content: str,
        thread_id: Optional[str] = None,
        reply_to: Optional[str] = None,
        ttl: Optional[int] = None,
        recipient_public_armor: Optional[str] = None,
    ) -> dict:
        """Compose, capture, and return a delivery report.

        Args:
            recipient: CapAuth identity URI of the recipient.
            content: Message text to capture.
            thread_id: Optional thread identifier.
            reply_to: Optional message ID being replied to.
            ttl: Ignored by the stub.
            recipient_public_armor: Ignored by the stub.

        Returns:
            dict: Synthetic delivery report with ``delivered=True``.
        """
        msg = ChatMessage(
            sender=self.IDENTITY,
            recipient=recipient,
            content=content,
            content_type=ContentType.PLAIN,
            thread_id=thread_id,
            reply_to_id=reply_to,
        )
        self.sent.append(msg)
        return {
            "delivered": True,
            "message_id": msg.id,
            "recipient": recipient,
            "transport": "mock",
        }

    def poll_inbox(
        self,
        sender_public_armor: Optional[str] = None,
    ) -> list[ChatMessage]:
        """Drain and return any messages queued via :meth:`inject`.

        Args:
            sender_public_armor: Ignored by the stub.

        Returns:
            list[ChatMessage]: Messages previously injected, then cleared.
        """
        msgs = list(self._poll_queue)
        self._poll_queue.clear()
        return msgs

    def inject(self, message: ChatMessage) -> None:
        """Queue *message* so it is returned by the next :meth:`poll_inbox`.

        Args:
            message: A ChatMessage to stage as an incoming message.
        """
        self._poll_queue.append(message)

    def send_typing_indicator(
        self,
        recipient: str,
        thread_id: Optional[str] = None,
    ) -> None:
        """No-op typing indicator for the stub.

        Args:
            recipient: Ignored.
            thread_id: Ignored.
        """


@pytest.fixture()
def mock_transport() -> _CapturingTransport:
    """Capturing ChatTransport stub that records all outbound messages.

    Returns a :class:`_CapturingTransport` instance.  Inspect
    ``mock_transport.sent`` in assertions to verify what was sent.

    Returns:
        _CapturingTransport: Ready-to-use capturing transport.
    """
    return _CapturingTransport()


# ---------------------------------------------------------------------------
# AgentMessenger fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_messenger(tmp_skchat_dir: Path):
    """AgentMessenger backed by an isolated FileTransport and temp history.

    History is stored under tmp_skchat_dir/history so tests are fully
    isolated from the real ~/.skchat data.

    Args:
        tmp_skchat_dir: Temp skchat home (provides history dir + env vars).

    Returns:
        AgentMessenger: Ready for agent-to-agent messaging in tests.
    """
    from skcomms.transports.file import FileTransport

    from skchat.agent_comm import AgentMessenger
    from skchat.history import ChatHistory
    from skchat.transport import ChatTransport

    inbox = tmp_skchat_dir / "inbox"
    outbox = tmp_skchat_dir / "outbox"
    inbox.mkdir(exist_ok=True)
    outbox.mkdir(exist_ok=True)

    file_transport = FileTransport(inbox_path=inbox, outbox_path=outbox)
    history = ChatHistory(history_dir=tmp_skchat_dir / "history")

    chat_transport = ChatTransport(
        skcomms=file_transport,
        history=history,
        identity="capauth:test-agent@skworld.io",
    )

    return AgentMessenger(
        identity="capauth:test-agent@skworld.io",
        history=history,
        transport=chat_transport,
    )


# ---------------------------------------------------------------------------
# SQLite / ChatHistory fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    """Temporary directory suitable as an SQLiteBackend ``base_path``.

    Creates a ``skchat-db/`` sub-directory under *tmp_path* so each
    test gets its own isolated SQLite store.

    Args:
        tmp_path: Pytest-provided unique temporary directory.

    Returns:
        Path: Isolated directory for the SQLiteBackend.
    """
    db_dir = tmp_path / "skchat-db"
    db_dir.mkdir()
    return db_dir


@pytest.fixture()
def chat_history(tmp_path: Path, tmp_db: Path):
    """ChatHistory backed by an isolated JSONL dir and SQLite store.

    JSONL files go to ``tmp_path/history/``; the optional SKMemory
    MemoryStore uses a SQLiteBackend rooted at *tmp_db*.  Falls back to
    a store-less ChatHistory if skmemory's SQLiteBackend is unavailable.

    Args:
        tmp_path: Pytest-provided unique temporary directory.
        tmp_db: Temp dir for the SQLiteBackend (from :func:`tmp_db`).

    Returns:
        ChatHistory: Fully isolated history instance.
    """
    from skchat.history import ChatHistory

    history_dir = tmp_path / "history"
    history_dir.mkdir()

    store = None
    try:
        from skmemory import MemoryStore
        from skmemory.backends.sqlite_backend import SQLiteBackend

        store = MemoryStore(primary=SQLiteBackend(base_path=str(tmp_db)))
    except Exception:  # pragma: no cover — optional dep
        pass

    return ChatHistory(store=store, history_dir=history_dir)


# ---------------------------------------------------------------------------
# Mock MemoryStore fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_memory() -> MagicMock:
    """MagicMock configured to behave like a skmemory MemoryStore.

    Returns a mock whose key methods (``snapshot``, ``list_memories``,
    ``search``) have sensible defaults so tests can inject it as a
    ChatHistory store without needing a real SQLite file.

    The mock's ``snapshot`` returns a new MagicMock each call with a
    unique ``id`` attribute so ID-tracking code does not collide.

    Returns:
        MagicMock: Drop-in MemoryStore substitute.
    """
    store = MagicMock(name="MemoryStore")
    _call_count = [0]

    def _snapshot(*_args: Any, **_kwargs: Any) -> MagicMock:
        _call_count[0] += 1
        mem = MagicMock(name=f"Memory#{_call_count[0]}")
        mem.id = f"mock-memory-{_call_count[0]:04d}"
        mem.tags = _kwargs.get("tags", [])
        mem.metadata = _kwargs.get("metadata", {})
        mem.content = _kwargs.get("content", "")
        mem.title = _kwargs.get("title", "")
        mem.created_at = datetime.now(timezone.utc)
        return mem

    store.snapshot.side_effect = _snapshot
    store.list_memories.return_value = []
    store.search.return_value = []
    return store


# ---------------------------------------------------------------------------
# PeerDiscovery fixture
# ---------------------------------------------------------------------------

_TEST_PEERS = [
    {
        "name": "Alice",
        "handle": "alice@skworld.io",
        "fingerprint": "AAAA111122223333",
        "entity_type": "agent",
        "contact_uris": ["capauth:alice@skworld.io"],
        "trust_level": "trusted",
        "capabilities": ["chat", "files"],
        "email": "alice@skworld.io",
        "added_at": "2025-01-01T00:00:00Z",
        "last_seen": None,
        "source": "test",
        "notes": "Test peer Alice",
    },
    {
        "name": "Bob",
        "handle": "bob@skworld.io",
        "fingerprint": "BBBB444455556666",
        "entity_type": "agent",
        "contact_uris": ["capauth:bob@skworld.io"],
        "trust_level": "trusted",
        "capabilities": ["chat"],
        "email": "bob@skworld.io",
        "added_at": "2025-01-01T00:00:00Z",
        "last_seen": None,
        "source": "test",
        "notes": "Test peer Bob",
    },
    {
        "name": "Charlie",
        "handle": "charlie@skworld.io",
        "fingerprint": "CCCC777788889999",
        "entity_type": "human",
        "contact_uris": ["capauth:charlie@skworld.io"],
        "trust_level": "observer",
        "capabilities": [],
        "email": "charlie@skworld.io",
        "added_at": "2025-01-02T00:00:00Z",
        "last_seen": None,
        "source": "test",
        "notes": "Test peer Charlie",
    },
]


@pytest.fixture()
def peer_discovery_fixture(tmp_path: Path):
    """PeerDiscovery backed by a temp peers directory with known test peers.

    Writes three JSON peer files (alice, bob, charlie) under
    ``tmp_path/peers/`` so tests have predictable, real-filesystem
    lookup without touching ``~/.skcapstone/peers/``.

    Use ``peer_discovery_fixture.list_peers()`` or
    ``peer_discovery_fixture.get_peer("alice")`` in assertions.

    Args:
        tmp_path: Pytest-provided unique temporary directory.

    Returns:
        PeerDiscovery: Instance pointed at the isolated peers directory.
    """
    from skchat.peer_discovery import PeerDiscovery

    peers_dir = tmp_path / "peers"
    peers_dir.mkdir()

    for peer in _TEST_PEERS:
        local = peer["handle"].split("@")[0]
        (peers_dir / f"{local}.json").write_text(
            json.dumps(peer, indent=2),
            encoding="utf-8",
        )

    return PeerDiscovery(peers_dir=peers_dir)


# ---------------------------------------------------------------------------
# LLM bridge stub
# ---------------------------------------------------------------------------


class _CannedLLMBridge:
    """Minimal LLMBridge stand-in that returns a configurable canned response.

    Call ``set_response(text)`` in a test to change what ``generate()``
    returns.  By default it returns an empty string so tests that do not
    care about the LLM output stay noise-free.
    """

    def __init__(self) -> None:
        self._response: str = ""

    def set_response(self, text: str) -> None:
        """Configure the canned response returned by generate().

        Args:
            text: Text to return on the next generate() call.
        """
        self._response = text

    def generate(
        self,
        prompt: str,
        system: str = "",
        **kwargs: Any,
    ) -> str:
        """Return the pre-configured canned response.

        Args:
            prompt: Ignored in the stub.
            system: Ignored in the stub.
            **kwargs: Ignored.

        Returns:
            str: The canned response set via set_response().
        """
        return self._response


@pytest.fixture()
def mock_llm_bridge() -> _CannedLLMBridge:
    """Stub LLMBridge with a configurable canned response.

    Usage in a test::

        def test_something(mock_llm_bridge):
            mock_llm_bridge.set_response("Mocked LLM output")
            result = mock_llm_bridge.generate("some prompt")
            assert result == "Mocked LLM output"

    Returns:
        _CannedLLMBridge: Stub with generate() + set_response().
    """
    return _CannedLLMBridge()


# ---------------------------------------------------------------------------
# transport — top-level alias for the capturing transport stub
# ---------------------------------------------------------------------------


@pytest.fixture()
def transport() -> _CapturingTransport:
    """Capturing ChatTransport stub (top-level alias for mock_transport).

    Prefer this fixture in tests that only need to verify outbound traffic
    and do not care about SKComms internals.  Tests that patch SKComms
    directly should use their own local ``transport`` fixture instead.

    Returns:
        _CapturingTransport: Fresh stub; inspect ``.sent`` in assertions.
    """
    return _CapturingTransport()


# ---------------------------------------------------------------------------
# peer_alice — pre-built Alice peer dict
# ---------------------------------------------------------------------------

_PEER_ALICE: dict[str, Any] = {
    "name": "Alice",
    "handle": "alice@skworld.io",
    "fingerprint": "AAAA111122223333",
    "entity_type": "agent",
    "contact_uris": ["capauth:alice@skworld.io"],
    "trust_level": "trusted",
    "capabilities": ["chat", "files"],
    "email": "alice@skworld.io",
    "added_at": "2025-01-01T00:00:00Z",
    "last_seen": None,
    "source": "test",
    "notes": "Test peer Alice",
}


@pytest.fixture()
def peer_alice() -> dict[str, Any]:
    """Pre-built peer dict representing Alice (capauth:alice@skworld.io).

    Identical to the Alice entry written by :func:`peer_discovery_fixture`
    so tests can reference peer metadata without constructing a full
    ``PeerDiscovery`` instance.

    Returns:
        dict: Alice's peer record as a plain Python dict.
    """
    return dict(_PEER_ALICE)


# ---------------------------------------------------------------------------
# event_loop — per-test asyncio event loop
# ---------------------------------------------------------------------------


@pytest.fixture()
def event_loop():
    """Provide a fresh asyncio event loop for each test function.

    Yields the loop and closes it after the test completes, ensuring no
    state leaks between async tests.

    Yields:
        asyncio.AbstractEventLoop: A new, running-ready event loop.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# --- CI collection gate ---------------------------------------------------
# A bare CI runner installs only skchat + dev. Test files importing an optional
# third-party dep (oqs) or a sibling SK* package (capauth/skcapstone/skos/
# skharness) cannot be COLLECTED there, and the import error crashes collection
# for the whole suite. skchat runs standalone without them, so skip such a file
# when its dep is absent. Local dev (all installed) skips nothing.
import importlib.util as _ilu
import re as _re

_OPTIONAL_DEPS = ("oqs", "capauth", "skcapstone", "skos", "skharness")
_MISSING_DEPS = tuple(m for m in _OPTIONAL_DEPS if _ilu.find_spec(m) is None)
_DEP_RE = {m: _re.compile(rf"^\s*(?:import|from)\s+{m}(?:\b|\.)", _re.M) for m in _MISSING_DEPS}


def pytest_ignore_collect(collection_path, config):
    if not _MISSING_DEPS or collection_path.suffix != ".py":
        return None
    try:
        src = collection_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if any(rx.search(src) for rx in _DEP_RE.values()):
        return True
    return None


# --- runtime-coupled test gates ------------------------------------------
# A handful of tests can only pass when a prerequisite that a bare CI runner
# lacks is present. Unlike the collect gate above (whole files that import an
# absent module at all), these are individual tests whose coupling is a
# runtime value (a private package's data, a sibling imported INSIDE the test
# body, or the operator's local peer store). We skip precisely those tests when
# the prerequisite is absent, so the rest of their file still runs in CI; on a
# developer/deployed node (where the prerequisite exists) they run normally.
_HAS_LUMINA_CREATIVE = _ilu.find_spec("lumina_creative") is not None
_HAS_SKSECURITY = _ilu.find_spec("sksecurity") is not None


def _has_lumina_peer_fingerprint() -> bool:
    # The fingerprint tests resolve "lumina@chef.skworld" to the real capauth
    # fingerprint via the operator's local peer store (daemon_proxy). Absent in
    # a bare CI runner -> resolution is "" (keyless) and the assertions can't hold.
    try:
        from skchat.daemon_proxy import fingerprint_for_identity

        return bool(fingerprint_for_identity("lumina@chef.skworld"))
    except Exception:
        return False


# test name -> (prerequisite-present flag, human reason)
def pytest_collection_modifyitems(config, items):
    lumina_creative_tests = {
        "test_phase2_api_is_importable_from_package_root",
        "test_wants_narrate_and_action_detectors",
        "test_respond_builds_persona_prefetches_memory_and_calls_llm",
        "test_build_default_registry_has_expected_tools",
        "test_operator_only_flags",
    }
    sksecurity_tests = {
        "test_self_report_reflects_negotiated_suite",
        "test_roundtrip_trusted_fqid_mints_token",
    }
    fingerprint_tests = {
        "test_soul_metadata_for_strict_resolution",
        "test_proven_sovereign_join_stamps_fingerprint",
        "test_proven_space_federation_stamps_fingerprint",
    }
    has_peer = _has_lumina_peer_fingerprint()
    for item in items:
        name = getattr(item, "originalname", None) or item.name
        if name in lumina_creative_tests and not _HAS_LUMINA_CREATIVE:
            item.add_marker(
                pytest.mark.skip(
                    reason="private lumina_creative package absent (narrate hints + creative tools)"
                )
            )
        elif name in sksecurity_tests and not _HAS_SKSECURITY:
            item.add_marker(pytest.mark.skip(reason="sksecurity sibling package absent"))
        elif name in fingerprint_tests and not has_peer:
            item.add_marker(
                pytest.mark.skip(
                    reason="lumina fingerprint not in local peer store (deployment-state)"
                )
            )


# --- CI agent state ------------------------------------------------------
# Many tests resolve the active agent (get_active_agent_name) or build an
# agent-scoped identity/transport. A bare CI runner has no SKAGENT and no
# ~/.skcapstone/agents, so that resolution returns None and the code raises
# "No agent configured" (616 errors in CI). Provide a deterministic default so
# the resolver yields a name; setdefault leaves a developer's real SKAGENT
# untouched.
import os as _os

_os.environ.setdefault("SKAGENT", "lumina")
_os.environ.setdefault("SKCAPSTONE_AGENT", "lumina")

# skmemory (<=0.11.4 on PyPI, which CI installs) resolves the active agent at
# IMPORT time (seeds.py module-level get_agent_paths()) and RAISES "No agent
# configured" when no non-template agent dir exists. A bare CI runner has none,
# so importing skmemory blows up ~14 tests. We can't patch the PyPI package, so
# provision a minimal agent dir in the DEFAULT location (NOT via SKCAPSTONE_HOME,
# which would repoint every agent-scoped path and break other tests). Only
# creates it when SKAGENT resolves to a dir that does not exist; a developer's
# real ~/.skcapstone/agents is left untouched. skmemory>=0.11.5 guards this at
# the source (seeds.py try/except) and makes the shim a harmless no-op.
try:
    from pathlib import Path as _P

    _agent_name = _os.environ.get("SKAGENT", "lumina")
    _skcap_home = _os.environ.get("SKCAPSTONE_HOME") or _os.path.expanduser("~/.skcapstone")
    _agent_dir = _P(_skcap_home) / "agents" / _agent_name
    if not _agent_dir.exists():
        for _sub in ("seeds", "config", "soul"):
            (_agent_dir / _sub).mkdir(parents=True, exist_ok=True)
except Exception:  # never let test bootstrap fail on this best-effort shim
    pass


# --- env isolation -------------------------------------------------------
# Some tests mutate os.environ directly (raw assignment, not monkeypatch) and
# leak agent/home vars (SKAGENT, SKCAPSTONE_HOME, SKCHAT_IDENTITY, ...) into
# later tests, which then fail only in-suite (they pass alone). Snapshot and
# restore os.environ around every test so no leak crosses a test boundary.
import os as _os_iso

import pytest as _pytest_iso


@_pytest_iso.fixture(autouse=True)
def _restore_environ():
    _snap = dict(_os_iso.environ)
    yield
    _os_iso.environ.clear()
    _os_iso.environ.update(_snap)
