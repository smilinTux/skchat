"""SKChat — AI-native encrypted P2P communication.

Chat should be sovereign. Your AI should be in the room.

SK = staycuriousANDkeepsmilin
"""

try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version
    __version__ = _pkg_version("skchat-sovereign")
except (ImportError, PackageNotFoundError):
    try:
        from ._version import version as __version__
    except ImportError:
        __version__ = "0.0.0+unknown"

__author__ = "smilinTux Team"
__license__ = "GPL-3.0-or-later"

from .agent_comm import AgentMessenger
from .crypto import (
    ChatCrypto,
    CryptoError,
    CryptoResult,
    DecryptionError,
    EncryptionError,
    SigningError,
    VerificationError,
)
from .daemon import ChatDaemon, run_daemon
from .encrypted_store import EncryptedChatHistory
from .ephemeral import MessageReaper
from .files import FileChunk, FileReceiver, FileSender, FileTransfer
from .group import (
    GroupChat,
    GroupKeyDistributor,
    GroupMember,
    GroupMessageEncryptor,
    MemberRole,
    ParticipantType,
)
from .history import ChatHistory
from .identity_bridge import (
    IdentityResolutionError,
    PeerResolutionError,
    get_peer_transport_address,
    get_sovereign_identity,
    resolve_peer_name,
)
from .models import (
    ChatMessage,
    ContentType,
    DeliveryStatus,
    Reaction,
    Thread,
)
from .plugins import ChatPlugin, PluginMeta, PluginRegistry, PluginState
from .presence import (
    PresenceIndicator,
    PresenceState,
    PresenceTracker,
)
from .reactions import ReactionEvent, ReactionManager, ReactionSummary
from .transport import ChatTransport
from . import integration  # noqa: F401 — optional skcapstone backbone (ADR adapter)

__all__ = [
    "ChatMessage",
    "ContentType",
    "DeliveryStatus",
    "Reaction",
    "Thread",
    "ChatCrypto",
    "CryptoError",
    "CryptoResult",
    "DecryptionError",
    "EncryptionError",
    "SigningError",
    "VerificationError",
    "PresenceIndicator",
    "PresenceState",
    "PresenceTracker",
    "ChatHistory",
    "ChatTransport",
    "get_sovereign_identity",
    "resolve_peer_name",
    "get_peer_transport_address",
    "IdentityResolutionError",
    "PeerResolutionError",
    "ChatDaemon",
    "run_daemon",
    "GroupChat",
    "GroupKeyDistributor",
    "GroupMember",
    "GroupMessageEncryptor",
    "MemberRole",
    "ParticipantType",
    "MessageReaper",
    "FileChunk",
    "FileReceiver",
    "FileSender",
    "FileTransfer",
    "ReactionEvent",
    "ReactionManager",
    "ReactionSummary",
    "ChatPlugin",
    "PluginMeta",
    "PluginRegistry",
    "PluginState",
    "AgentMessenger",
    "EncryptedChatHistory",
    "integration",
    "__version__",
]
