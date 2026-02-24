"""SKChat — AI-native encrypted P2P communication.

Chat should be sovereign. Your AI should be in the room.

SK = staycuriousANDkeepsmilin
"""

__version__ = "0.1.0"
__author__ = "smilinTux Team"
__license__ = "GPL-3.0-or-later"

from .models import (
    ChatMessage,
    ContentType,
    DeliveryStatus,
    Reaction,
    Thread,
)
from .crypto import (
    ChatCrypto,
    CryptoError,
    CryptoResult,
    DecryptionError,
    EncryptionError,
    SigningError,
    VerificationError,
)
from .presence import (
    PresenceIndicator,
    PresenceState,
    PresenceTracker,
)
from .history import ChatHistory
from .transport import ChatTransport
from .identity_bridge import (
    get_sovereign_identity,
    resolve_peer_name,
    get_peer_transport_address,
    IdentityResolutionError,
    PeerResolutionError,
)
from .daemon import ChatDaemon, run_daemon

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
    "__version__",
]
