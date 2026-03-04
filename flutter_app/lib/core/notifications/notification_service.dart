import 'package:flutter_local_notifications/flutter_local_notifications.dart';

/// Singleton service for showing local push notifications.
///
/// Wraps [FlutterLocalNotificationsPlugin] and provides a simple API for
/// displaying message notifications with sender name and preview text.
/// Call [initialize] once at app startup before showing any notifications.
class NotificationService {
  static final NotificationService _instance = NotificationService._internal();

  factory NotificationService() => _instance;
  NotificationService._internal();

  final FlutterLocalNotificationsPlugin _plugin =
      FlutterLocalNotificationsPlugin();
  int _idCounter = 0;

  static const _channelId = 'skchat_messages';
  static const _channelName = 'SKChat Messages';
  static const _channelDescription =
      'Sovereign encrypted peer-to-peer messages';

  bool _initialized = false;

  /// Initialize the notification plugin. Must be called once at startup.
  Future<void> initialize() async {
    if (_initialized) return;
    const androidSettings =
        AndroidInitializationSettings('@mipmap/ic_launcher');
    const iosSettings = DarwinInitializationSettings(
      requestAlertPermission: true,
      requestBadgePermission: true,
      requestSoundPermission: true,
    );
    const settings = InitializationSettings(
      android: androidSettings,
      iOS: iosSettings,
    );
    await _plugin.initialize(settings);
    _initialized = true;
  }

  /// Show a notification for an incoming chat message.
  ///
  /// [senderName] appears as the notification title.
  /// [messagePreview] is truncated to 120 chars and shown as the body.
  Future<void> showMessageNotification({
    required String senderName,
    required String messagePreview,
  }) async {
    if (!_initialized) return;
    const androidDetails = AndroidNotificationDetails(
      _channelId,
      _channelName,
      channelDescription: _channelDescription,
      importance: Importance.high,
      priority: Priority.high,
      showWhen: true,
    );
    const iosDetails = DarwinNotificationDetails(
      badgeNumber: 1,
      presentAlert: true,
      presentBadge: true,
      presentSound: true,
    );
    const details = NotificationDetails(
      android: androidDetails,
      iOS: iosDetails,
    );
    final preview = messagePreview.length > 120
        ? '${messagePreview.substring(0, 120)}…'
        : messagePreview;
    await _plugin.show(_idCounter++, senderName, preview, details);
  }

  /// Cancel all pending and delivered notifications.
  Future<void> cancelAll() async => _plugin.cancelAll();
}
