import 'package:flutter_test/flutter_test.dart';
import 'package:skchat/features/onboarding/onboarding_provider.dart';

void main() {
  group('OnboardingState', () {
    test('defaults are correct', () {
      const state = OnboardingState();

      expect(state.currentStep, 0);
      expect(state.isComplete, false);
      expect(state.identityChoice, isNull);
      expect(state.generatedFingerprint, isNull);
      expect(state.daemonDetected, false);
      expect(state.syncthingDetected, false);
      expect(state.isDetecting, false);
    });

    test('copyWith updates only specified fields', () {
      const state = OnboardingState();

      final updated = state.copyWith(currentStep: 3);

      expect(updated.currentStep, 3);
      expect(updated.isComplete, false);
      expect(updated.identityChoice, isNull);
    });

    test('copyWith preserves all fields when none specified', () {
      final state = OnboardingState(
        currentStep: 2,
        isComplete: true,
        identityChoice: 'generate',
        generatedFingerprint: 'ABCDEF',
        daemonDetected: true,
        syncthingDetected: true,
        isDetecting: false,
      );

      final updated = state.copyWith();

      expect(updated.currentStep, 2);
      expect(updated.isComplete, true);
      expect(updated.identityChoice, 'generate');
      expect(updated.generatedFingerprint, 'ABCDEF');
      expect(updated.daemonDetected, true);
      expect(updated.syncthingDetected, true);
      expect(updated.isDetecting, false);
    });

    test('copyWith sets identity choice to import', () {
      const state = OnboardingState();

      final updated = state.copyWith(identityChoice: 'import');

      expect(updated.identityChoice, 'import');
    });

    test('copyWith sets identity choice to generate', () {
      const state = OnboardingState();

      final updated = state.copyWith(identityChoice: 'generate');

      expect(updated.identityChoice, 'generate');
    });

    test('copyWith tracks transport detection', () {
      const state = OnboardingState();

      final detecting = state.copyWith(isDetecting: true);
      expect(detecting.isDetecting, true);

      final detected = detecting.copyWith(
        isDetecting: false,
        daemonDetected: true,
        syncthingDetected: false,
      );
      expect(detected.isDetecting, false);
      expect(detected.daemonDetected, true);
      expect(detected.syncthingDetected, false);
    });

    test('copyWith sets generated fingerprint', () {
      const state = OnboardingState(identityChoice: 'generate');

      final updated = state.copyWith(
        generatedFingerprint: 'CCBE9306410CF8CD5E393D6DEC31663B95230684',
      );

      expect(updated.generatedFingerprint,
          'CCBE9306410CF8CD5E393D6DEC31663B95230684');
    });
  });

  group('kOnboardingPageCount', () {
    test('is 5 pages', () {
      expect(kOnboardingPageCount, 5);
    });
  });

  group('Onboarding providers', () {
    test('providers are accessible', () {
      expect(onboardingProvider, isNotNull);
      expect(onboardingCompleteProvider, isNotNull);
    });
  });
}
