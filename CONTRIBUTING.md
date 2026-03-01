# Contributing to SKChat

## Commit Hygiene

### Tests and Features Together

Always commit test files **alongside** the feature or bug-fix they cover.
A pull request that only adds or modifies tests without a corresponding
feature change (or vice-versa) should be the exception, not the rule.

Good:
```
feat: add group-chat encryption
  - lib/services/group_crypto.dart  (feature)
  - test/services/group_crypto_test.dart  (test)
```

Bad:
```
# Commit 1 — feature without tests
feat: add group-chat encryption

# Commit 2 — tests added later, disconnected from context
test: add group-chat encryption tests
```

### Commit Message Format

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

<optional body>
```

Common types: `feat`, `fix`, `test`, `refactor`, `docs`, `chore`, `ci`.

### Branch Naming

```
<type>/<short-kebab-description>
```

Examples: `feat/group-encryption`, `fix/isolate-pgp-jank`, `ci/android-signing`.

## Code Style

- Dart: follow `flutter_lints` (already in `analysis_options.yaml`).
- Python: follow the project's existing `ruff` / `black` formatting.
- Keep PRs small and focused on one concern.

## Testing

- Unit tests live in `test/` mirroring the `lib/` directory structure.
- Use `mocktail` for mocking in Dart tests.
- Use `ProviderContainer` for Riverpod provider tests (no widget needed).
- Run `flutter test` locally before pushing.

## Security

- Never commit secrets, keystores, or `.env` files.
- PGP private keys must stay in `flutter_secure_storage` on-device;
  never log or transmit them.
