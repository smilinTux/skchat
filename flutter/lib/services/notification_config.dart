import 'package:flutter_local_notifications/flutter_local_notifications.dart';

/// Channel ID used for all SKChat message notifications.
const kMessageChannelId = 'skchat_messages';
const kMessageChannelName = 'SKChat Messages';
const kMessageChannelDesc = 'Incoming peer-to-peer messages';

/// Channel ID used for SKSeal signing requests.
const kSigningChannelId = 'skchat_signing';
const kSigningChannelName = 'SKSeal Signing Requests';
const kSigningChannelDesc = 'Document signing requests from peers';

/// Builds the Android-specific initialization settings.
AndroidInitializationSettings buildAndroidInitSettings() {
  // app_icon must exist at android/app/src/main/res/drawable/app_icon.png
  return const AndroidInitializationSettings('@mipmap/ic_launcher');
}

/// Builds the Darwin (iOS/macOS) initialization settings.
DarwinInitializationSettings buildDarwinInitSettings() {
  return const DarwinInitializationSettings(
    requestAlertPermission: true,
    requestBadgePermission: true,
    requestSoundPermission: true,
  );
}

/// Builds the Linux initialization settings.
/// Uses a themed icon name available in most freedesktop icon themes.
LinuxInitializationSettings buildLinuxInitSettings() {
  return LinuxInitializationSettings(
    defaultActionName: 'Open SKChat',
    defaultIcon: AssetsLinuxIcon('icons/app_icon.png'),
  );
}

/// Assembles the full cross-platform [InitializationSettings].
InitializationSettings buildInitializationSettings() {
  return InitializationSettings(
    android: buildAndroidInitSettings(),
    iOS: buildDarwinInitSettings(),
    macOS: buildDarwinInitSettings(),
    linux: buildLinuxInitSettings(),
  );
}

/// Android notification channel for incoming chat messages (high importance).
AndroidNotificationChannel get messageAndroidChannel =>
    const AndroidNotificationChannel(
      kMessageChannelId,
      kMessageChannelName,
      description: kMessageChannelDesc,
      importance: Importance.high,
      playSound: true,
      enableVibration: true,
    );

/// Android notification channel for signing requests (max importance).
AndroidNotificationChannel get signingAndroidChannel =>
    const AndroidNotificationChannel(
      kSigningChannelId,
      kSigningChannelName,
      description: kSigningChannelDesc,
      importance: Importance.max,
      playSound: true,
      enableVibration: true,
    );

/// Platform-specific [NotificationDetails] for a chat message notification.
NotificationDetails messageNotificationDetails() {
  return NotificationDetails(
    android: AndroidNotificationDetails(
      kMessageChannelId,
      kMessageChannelName,
      channelDescription: kMessageChannelDesc,
      importance: Importance.high,
      priority: Priority.high,
      icon: '@mipmap/ic_launcher',
      category: AndroidNotificationCategory.message,
    ),
    iOS: const DarwinNotificationDetails(
      categoryIdentifier: 'MESSAGE',
    ),
    macOS: const DarwinNotificationDetails(
      categoryIdentifier: 'MESSAGE',
    ),
    linux: const LinuxNotificationDetails(
      urgency: LinuxNotificationUrgency.normal,
    ),
  );
}

/// Platform-specific [NotificationDetails] for a SKSeal signing request.
NotificationDetails signingNotificationDetails() {
  return NotificationDetails(
    android: AndroidNotificationDetails(
      kSigningChannelId,
      kSigningChannelName,
      channelDescription: kSigningChannelDesc,
      importance: Importance.max,
      priority: Priority.max,
      icon: '@mipmap/ic_launcher',
      category: AndroidNotificationCategory.event,
    ),
    iOS: const DarwinNotificationDetails(
      categoryIdentifier: 'SIGNING_REQUEST',
    ),
    macOS: const DarwinNotificationDetails(
      categoryIdentifier: 'SIGNING_REQUEST',
    ),
    linux: const LinuxNotificationDetails(
      urgency: LinuxNotificationUrgency.critical,
    ),
  );
}
