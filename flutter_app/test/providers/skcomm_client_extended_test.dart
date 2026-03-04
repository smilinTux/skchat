import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';

import 'package:skchat_mobile/core/transport/skcomm_client.dart';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

class MockDio extends Mock implements Dio {}

/// Build a fake successful Dio Response.
Response<T> _fakeResponse<T>(T data, {int statusCode = 200}) {
  return Response<T>(
    data: data,
    statusCode: statusCode,
    requestOptions: RequestOptions(path: '/'),
  );
}

/// Build a fake DioException (connection refused).
DioException _connectionError() => DioException(
      type: DioExceptionType.connectionError,
      requestOptions: RequestOptions(path: '/'),
    );

void main() {
  late MockDio mockDio;
  late SKCommClient client;

  setUp(() {
    mockDio = MockDio();
    client = SKCommClient(baseUrl: 'http://test.local', dio: mockDio);
  });

  // -------------------------------------------------------------------------
  // getTrustInfo
  // -------------------------------------------------------------------------
  group('SKCommClient.getTrustInfo', () {
    test('returns trust map on success', () async {
      when(() => mockDio.get(any(),
              queryParameters: any(named: 'queryParameters')))
          .thenAnswer((_) async => _fakeResponse<Map<String, dynamic>>({
                'trust_level': 'high',
                'verified': true,
              }));

      final trust = await client.getTrustInfo('lumina');
      expect(trust['trust_level'], 'high');
      expect(trust['verified'], isTrue);
    });

    test('throws SKCommException on connection error', () async {
      when(() => mockDio.get(any(),
              queryParameters: any(named: 'queryParameters')))
          .thenThrow(_connectionError());

      expect(
        () => client.getTrustInfo('jarvis'),
        throwsA(isA<SKCommException>()),
      );
    });

    test('calls correct endpoint', () async {
      when(() => mockDio.get('http://test.local/api/v1/trust/opus',
              queryParameters: any(named: 'queryParameters')))
          .thenAnswer((_) async =>
              _fakeResponse<Map<String, dynamic>>({'trust_level': 'medium'}));

      await client.getTrustInfo('opus');

      verify(() => mockDio.get(
            'http://test.local/api/v1/trust/opus',
            queryParameters: any(named: 'queryParameters'),
          )).called(1);
    });
  });

  // -------------------------------------------------------------------------
  // getMemoryEntries
  // -------------------------------------------------------------------------
  group('SKCommClient.getMemoryEntries', () {
    test('returns list of entries on success', () async {
      when(() => mockDio.get(any(),
              queryParameters: any(named: 'queryParameters')))
          .thenAnswer((_) async => _fakeResponse<List<dynamic>>([
                {'id': '1', 'content': 'sovereign penguin'},
              ]));

      final entries = await client.getMemoryEntries();
      expect(entries, hasLength(1));
      expect(entries.first['content'], 'sovereign penguin');
    });

    test('passes query param when provided', () async {
      when(() => mockDio.get(
            'http://test.local/api/v1/memory',
            queryParameters: {'q': 'penguin'},
          )).thenAnswer((_) async => _fakeResponse<List<dynamic>>([]));

      await client.getMemoryEntries(query: 'penguin');

      verify(() => mockDio.get(
            'http://test.local/api/v1/memory',
            queryParameters: {'q': 'penguin'},
          )).called(1);
    });

    test('throws SKCommException on error', () async {
      when(() => mockDio.get(any(),
              queryParameters: any(named: 'queryParameters')))
          .thenThrow(_connectionError());

      expect(
        () => client.getMemoryEntries(),
        throwsA(isA<SKCommException>()),
      );
    });
  });

  // -------------------------------------------------------------------------
  // storeMemory
  // -------------------------------------------------------------------------
  group('SKCommClient.storeMemory', () {
    test('posts to /api/v1/memory with content and tags', () async {
      when(() => mockDio.post(any(), data: any(named: 'data')))
          .thenAnswer((_) async => _fakeResponse<void>(null, statusCode: 201));

      await client.storeMemory(
          content: 'recall this', tags: ['short-term'], scope: 'mid-term');

      final captured = verify(
        () => mockDio.post(
          'http://test.local/api/v1/memory',
          data: captureAny(named: 'data'),
        ),
      ).captured.single as Map<String, dynamic>;

      expect(captured['content'], 'recall this');
      expect(captured['tags'], containsAll(['short-term']));
      expect(captured['scope'], 'mid-term');
    });

    test('omits optional fields when null', () async {
      when(() => mockDio.post(any(), data: any(named: 'data')))
          .thenAnswer((_) async => _fakeResponse<void>(null));

      await client.storeMemory(content: 'bare content');

      final captured = verify(
        () => mockDio.post(
          'http://test.local/api/v1/memory',
          data: captureAny(named: 'data'),
        ),
      ).captured.single as Map<String, dynamic>;

      expect(captured.containsKey('tags'), isFalse);
      expect(captured.containsKey('scope'), isFalse);
    });

    test('throws SKCommException on connection error', () async {
      when(() => mockDio.post(any(), data: any(named: 'data')))
          .thenThrow(_connectionError());

      expect(
        () => client.storeMemory(content: 'test'),
        throwsA(isA<SKCommException>()),
      );
    });
  });
}
