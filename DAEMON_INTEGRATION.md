# SKChat Daemon Integration Notes

## Task 68f17961: SKChat receive daemon

This task requires the SKComm daemon API to be completed by transport-builder first.

### Current Status

**Task b5fcf55d (Identity Bridge) - COMPLETED:**
- ✅ Automatic CapAuth identity resolution from `~/.skcapstone/identity/`
- ✅ Peer name resolver for friendly names (e.g., "lumina" → "capauth:lumina@capauth.local")
- ✅ Looks up peers in `~/.skcapstone/peers/` or `~/.skcomm/peers/`
- ✅ CLI commands support both full URIs and friendly names
- ✅ All tests passing (176 tests)

### Daemon Integration Plan (Waiting on transport-builder)

The receive daemon needs:

1. **SKComm Daemon API** (transport-builder task)
   - HTTP REST API server for message polling
   - Background service for continuous polling
   - Configuration in `~/.skcomm/config.yml`

2. **SKChat Daemon Integration** (this task, blocked)
   - When SKComm daemon is ready, integrate via:
     - HTTP client to poll daemon API
     - Store incoming messages in SKChat history
     - Optional: Register as SKComm daemon plugin

3. **Alternative: Simple Background Loop**
   - For MVP, could use `skchat watch` (already implemented)
   - Runs `transport.poll_inbox()` every N seconds
   - Can be wrapped in systemd service or screen session

### Integration Points

```python
# In skchat/daemon.py (to be created)
from .transport import ChatTransport
from .history import ChatHistory

def daemon_loop(interval: int = 5):
    """Simple background polling loop."""
    transport = ChatTransport.from_config()
    history = ChatHistory.from_config()
    
    while True:
        messages = transport.poll_inbox()
        for msg in messages:
            history.store_message(msg)
        time.sleep(interval)
```

### Current Workaround

Users can run `skchat watch` in a tmux/screen session for continuous receiving:

```bash
skchat watch --interval 5
```

This provides the receive daemon functionality until the SKComm HTTP API is ready.

### Files Modified for b5fcf55d

- `src/skchat/identity_bridge.py` (NEW) - Identity and peer resolution
- `src/skchat/cli.py` - Updated `_get_identity()`, `send()`, `history()`
- `src/skchat/__init__.py` - Exported new identity functions
- `tests/test_identity_bridge.py` (NEW) - 14 tests for identity bridge

### Testing

```bash
# Test identity resolution
skchat status

# Test peer name resolution
skchat send lumina "Hello from sovereign identity!"

# Test conversation history with friendly names
skchat history lumina
```
