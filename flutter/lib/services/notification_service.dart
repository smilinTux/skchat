import 'dart:io';

import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import 'notification_config.dart';

/// Sovereign local notification service — no Firebase, no Google, no tokens.
///
/// Use [NotificationService.instance] to access the singleton. Call
/// [initialize] once from your app entry point before showing any
/// notifications.
class NotificationService {
  NotificationService._();

  static final NotificationService instance = NotificationService._();

  final FlutterLocalNotificationsPlugin _plugin =
      FlutterLocalNotificationsPlugin();

  bool _initialized = false;

  // Notification ID counter — wraps at 2^31-1 to stay within Android's int limit.
  static int _nextId = 1;
  static int _allocateId() {
    final id = _nextId;
    _nextId = (_nextId + 1) & 0x7FFFFFFF;
    return id;
  }

  // Maps peerId → list of notification IDs so we can cancel per-peer.
  final Map<String, List<int>> _peerNotificationIds = {};

  /// Initializes the notification plugin and creates Android channels.
  ///
  /// Safe to call multiple times; subsequent calls are no-ops.
  Future<void> initialize() async {
    if (_initialized) return;

    final settings = buildInitializationSettings();

    await _plugin.initialize(
      settings,
      onDidReceiveNotificationResponse: _onNotificationTap,
      onDidReceiveBackgroundNotificationResponse: _onBackgroundNotificationTap,
    );

    // Create Android channels up-front (no-op on other platforms).
    if (Platform.isAndroid) {
      final androidPlugin = _plugin
          .resolvePlatformSpecificImplementation<
              AndroidFlutterLocalNotificationsPlugin>();
      await androidPlugin?.createNotificationChannel(messageAndroidChannel);
      await androidPlugin?.createNotificationChannel(signingAndroidChannel);
    }

    _initialized = true;
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  /// Shows a local notification for an incoming SKChat message.
  ///
  /// Args:
  ///   senderName (String): Display name of the sender (falls back to [peerId]).
  ///   content (String): Message body text.
  ///   peerId (String): Sender's peer identity string; used for grouping/cancellation.
  Future<void> showMessageNotification({
    required String senderName,
    required String content,
    required String peerId,
  }) async {
    await _ensureInitialized();

    final id = _allocateId();
    _peerNotificationIds.putIfAbsent(peerId, () => []).add(id);

    await _plugin.show(
      id,
      senderName,
      content,
      messageNotificationDetails(),
      payload: peerId,
    );
  }

  /// Shows a local notification for an incoming SKSeal document signing request.
  ///
  /// Intended for future integration with the SKSeal signing pipeline.
  ///
  /// Args:
  ///   documentTitle (String): Human-readable title of the document to sign.
  ///   senderName (String): Name of the peer requesting the signature.
  Future<void> showSigningRequest({
    required String documentTitle,
    required String senderName,
  }) async {
    await _ensureInitialized();

    final id = _allocateId();

    await _plugin.show(
      id,
      'Signing request from $senderName',
      'Document: $documentTitle',
      signingNotificationDetails(),
      payload: 'signing:$senderName',
    );
  }

  /// Cancels all active notifications regardless of peer.
  Future<void> cancelAll() async {
    await _plugin.cancelAll();
    _peerNotificationIds.clear();
  }

  /// Cancels every notification that was shown for the given [peerId].
  ///
  /// Useful when the user opens a conversation — clears the unread badge.
  Future<void> cancelForPeer(String peerId) async {
    final ids = _peerNotificationIds.remove(peerId);
    if (ids == null) return;
    for (final id in ids) {
      await _plugin.cancel(id);
    }
  }

  // ── Internals ──────────────────────────────────────────────────────────────

  Future<void> _ensureInitialized() async {
    if (!_initialized) await initialize();
  }

  /// Called when the user taps a notification while the app is in the foreground
  /// or background (but still running).
  static void _onNotificationTap(NotificationResponse response) {
    // Jarvis will wire navigation here when integrating with go_router.
    // payload is peerId for messages, 'signing:<peer>' for signing requests.
  }

  /// Called when the user taps a notification while the app is terminated.
  /// Must be a top-level or static function — Flutter constraint.
  @pragma('vm:entry-point')
  static void _onBackgroundNotificationTap(NotificationResponse response) {
    // Handled on next app launch via getNotificationAppLaunchDetails().
  }
}
