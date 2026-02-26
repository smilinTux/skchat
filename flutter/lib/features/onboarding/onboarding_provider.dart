import 'dart:io';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:hive_flutter/hive_flutter.dart';

const _kOnboardingBox = 'onboarding';
const _kIsCompleteKey = 'isComplete';
const _kIdentityChoiceKey = 'identityChoice';

/// Total number of onboarding pages.
const kOnboardingPageCount = 5;

/// State for the onboarding wizard.
class OnboardingState {
  const OnboardingState({
    this.currentStep = 0,
    this.isComplete = false,
    this.identityChoice,
    this.generatedFingerprint,
    this.daemonDetected = false,
    this.syncthingDetected = false,
    this.isDetecting = false,
  });

  final int currentStep;
  final bool isComplete;

  /// 'import' | 'generate' | null if not yet chosen.
  final String? identityChoice;

  /// Hex fingerprint shown after key generation.
  final String? generatedFingerprint;

  final bool daemonDetected;
  final bool syncthingDetected;

  /// True while transport detection is in progress.
  final bool isDetecting;

  OnboardingState copyWith({
    int? currentStep,
    bool? isComplete,
    String? identityChoice,
    String? generatedFingerprint,
    bool? daemonDetected,
    bool? syncthingDetected,
    bool? isDetecting,
  }) {
    return OnboardingState(
      currentStep: currentStep ?? this.currentStep,
      isComplete: isComplete ?? this.isComplete,
      identityChoice: identityChoice ?? this.identityChoice,
      generatedFingerprint: generatedFingerprint ?? this.generatedFingerprint,
      daemonDetected: daemonDetected ?? this.daemonDetected,
      syncthingDetected: syncthingDetected ?? this.syncthingDetected,
      isDetecting: isDetecting ?? this.isDetecting,
    );
  }
}

/// Notifier for the onboarding wizard.
///
/// Persists [isComplete] and [identityChoice] to Hive so that the wizard
/// only runs once per device.
class OnboardingNotifier extends Notifier<OnboardingState> {
  @override
  OnboardingState build() {
    Future.microtask(_loadPersistedState);
    return const OnboardingState();
  }

  Future<void> _loadPersistedState() async {
    final box = await Hive.openBox<dynamic>(_kOnboardingBox);
    final isComplete = box.get(_kIsCompleteKey, defaultValue: false) as bool;
    final identityChoice = box.get(_kIdentityChoiceKey) as String?;
    state = state.copyWith(
      isComplete: isComplete,
      identityChoice: identityChoice,
    );
  }

  /// Advance to the next page in the wizard.
  void nextStep() {
    final next = state.currentStep + 1;
    if (next < kOnboardingPageCount) {
      state = state.copyWith(currentStep: next);
    }
  }

  /// Jump to a specific step.
  void goToStep(int step) {
    state = state.copyWith(currentStep: step);
  }

  /// Record the user's identity choice and optionally store a fingerprint.
  Future<void> setIdentityChoice(
    String choice, {
    String? fingerprint,
  }) async {
    final box = await Hive.openBox<dynamic>(_kOnboardingBox);
    await box.put(_kIdentityChoiceKey, choice);
    state = state.copyWith(
      identityChoice: choice,
      generatedFingerprint: fingerprint ?? state.generatedFingerprint,
    );
  }

  /// Probe localhost for SKComm daemon and Syncthing availability.
  Future<void> detectTransports() async {
    state = state.copyWith(isDetecting: true);

    // SKComm daemon typically listens on :8765; Syncthing GUI on :8384.
    final results = await Future.wait([
      _tcpProbe('localhost', 8765),
      _tcpProbe('localhost', 8384),
    ]);

    state = state.copyWith(
      daemonDetected: results[0],
      syncthingDetected: results[1],
      isDetecting: false,
    );
  }

  /// Returns true if a TCP connection can be established within 2 seconds.
  Future<bool> _tcpProbe(String host, int port) async {
    try {
      final socket = await Socket.connect(
        host,
        port,
        timeout: const Duration(seconds: 2),
      );
      await socket.close();
      return true;
    } catch (_) {
      return false;
    }
  }

  /// Persist completion and mark the wizard as done.
  Future<void> markComplete() async {
    final box = await Hive.openBox<dynamic>(_kOnboardingBox);
    await box.put(_kIsCompleteKey, true);
    state = state.copyWith(isComplete: true);
  }
}

final onboardingProvider =
    NotifierProvider<OnboardingNotifier, OnboardingState>(
  OnboardingNotifier.new,
);

/// Convenience provider — true once onboarding has been completed.
final onboardingCompleteProvider = Provider<bool>((ref) {
  return ref.watch(onboardingProvider).isComplete;
});
