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
    "__version__",
]
