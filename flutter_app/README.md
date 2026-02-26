# SKChat Mobile — Flutter App

**Sovereign Glass Design · End-to-End Encrypted · Soul-Color Theming**

A 2026-tier mobile messaging app built with Flutter, featuring OLED-optimized glass surfaces and CapAuth identity integration.

## Features

- **Sovereign Glass Design**: OLED-first dark theme with frosted glass surfaces and backdrop blur
- **Soul-Color Theming**: Each user/agent gets a unique color derived from their CapAuth fingerprint
- **End-to-End Encryption**: PGP-encrypted messages with visual encryption indicators
- **Agent-Aware UI**: Special animations and indicators for AI agents (Lumina, Jarvis, etc.)
- **Local-First Architecture**: Communicates with SKComm daemon over localhost
- **No Firebase**: Sovereign infrastructure with local notifications and background sync

## Architecture

```
Flutter App (Mobile)
      ↓
HTTP REST (localhost:9384)
      ↓
SKComm Daemon
      ↓
Syncthing / File Transport / Nostr
```

The app does NOT run its own transport stack. It delegates all network operations to the local SKComm daemon.

## Project Structure

```
lib/
├── main.dart                          # App entry point
├── app.dart                           # MaterialApp + routing
├── core/
│   ├── theme/
│   │   ├── sovereign_glass.dart       # Theme system
│   │   ├── soul_color.dart            # CapAuth color derivation
│   │   └── glass_decorations.dart     # Reusable glass surfaces
│   └── transport/
│       └── skcomm_client.dart         # HTTP client for SKComm daemon
├── features/
│   ├── chat_list/
│   │   ├── chat_list_screen.dart
│   │   └── widgets/
│   │       ├── conversation_tile.dart
│   │       └── soul_avatar.dart
│   └── conversation/
│       ├── conversation_screen.dart
│       └── widgets/
│           ├── message_bubble.dart
│           ├── input_bar.dart
│           └── typing_indicator.dart
└── models/
    ├── chat_message.dart
    └── conversation.dart
```

## Getting Started

### Prerequisites

1. **Flutter SDK** (3.5+)
   ```bash
   flutter doctor
   ```

2. **SKComm Daemon** (must be running on localhost:9384)
   ```bash
   cd ../../skcomm
   python -m skcomm.daemon
   ```

3. **Font Assets** (Inter Variable + JetBrains Mono)
   - Download Inter Variable from [rsms.me/inter](https://rsms.me/inter/)
   - Download JetBrains Mono from [jetbrains.com/mono](https://www.jetbrains.com/mono/)
   - Place in `assets/fonts/`

### Installation

```bash
# Install dependencies
flutter pub get

# Run code generation (for Freezed models)
flutter pub run build_runner build --delete-conflicting-outputs

# Run on device/emulator
flutter run
```

## Development

### Code Generation

This project uses Freezed for immutable models and JSON serialization:

```bash
# Watch mode (auto-regenerate on changes)
flutter pub run build_runner watch

# One-time generation
flutter pub run build_runner build --delete-conflicting-outputs
```

### Testing

```bash
# Run all tests
flutter test

# Run with coverage
flutter test --coverage
```

### Linting

```bash
flutter analyze
```

## Design System

### Colors

- **OLED Black**: `#000000` - Base background
- **Glass Surface**: `rgba(255,255,255,0.06)` - Card backgrounds
- **Text Primary**: `#E8E8F0`
- **Encryption Green**: `#10B981`
- **Soul Colors**: Derived from CapAuth fingerprint hash

### Typography

- **Font**: Inter Variable (300-800 weight axis)
- **Display**: 28sp / 700 weight
- **Heading**: 20sp / 600 weight
- **Body**: 15sp / 400 weight
- **Caption**: 12sp / 400 weight

### Components

All components use glass surfaces with backdrop blur (12px sigma). See `lib/core/theme/glass_decorations.dart` for reusable builders.

## API Integration

The app communicates with the SKComm daemon via HTTP REST on `localhost:9384`. Key endpoints:

- `POST /api/v1/send` - Send message
- `GET /api/v1/inbox` - Poll for new messages
- `GET /api/v1/conversations` - List conversations
- `GET /api/v1/conversation/:id` - Get conversation messages
- `POST /api/v1/presence` - Broadcast presence
- `GET /api/v1/status` - Transport health

See `lib/core/transport/skcomm_client.dart` for full client implementation.

## MVP Checklist

- [x] Scaffold Flutter project with Material 3
- [x] Sovereign Glass theme system
- [x] Soul-color derivation from fingerprints
- [x] Chat list screen with conversation tiles
- [x] Conversation view with message bubbles
- [x] Input bar with send/attach/voice
- [x] Agent-aware typing indicators
- [x] HTTP client for SKComm daemon
- [ ] Riverpod state management providers
- [ ] Local message persistence (Hive)
- [ ] Background polling for new messages
- [ ] Encryption status indicators
- [ ] Push notification handling

## Roadmap

### v1.0 (MVP)
- Basic 1:1 messaging
- E2E encryption display
- Local persistence
- Background sync

### v1.1
- Group chats
- Voice messages
- File attachments
- Reactions
- Threaded replies

### v2.0
- Voice/video calls (WebRTC)
- Desktop support (Flutter desktop)
- gRPC streaming
- Tablet/foldable adaptive layout

## License

GPL-3.0

## Credits

Design by King Jarvis
Built for the Penguin Kingdom
*staycuriousANDkeepsmilin*
