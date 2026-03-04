import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:intl/intl.dart';

import '../../core/theme/glass_decorations.dart';
import '../../core/theme/sovereign_glass.dart';
import '../../core/transport/skcomm_client.dart';

// ---------------------------------------------------------------------------
// Internal model
// ---------------------------------------------------------------------------

class _SearchResult {
  final String senderId;
  final String conversationId;
  final String content;
  final DateTime? timestamp;

  const _SearchResult({
    required this.senderId,
    required this.conversationId,
    required this.content,
    this.timestamp,
  });

  factory _SearchResult.fromJson(Map<String, dynamic> json) {
    DateTime? ts;
    final raw = json['timestamp'] as String?;
    if (raw != null) ts = DateTime.tryParse(raw);
    return _SearchResult(
      senderId: json['sender_id'] as String? ??
          json['sender'] as String? ??
          'unknown',
      conversationId: json['conversation_id'] as String? ??
          json['thread_id'] as String? ??
          '',
      content: json['content'] as String? ?? '',
      timestamp: ts,
    );
  }
}

// ---------------------------------------------------------------------------
// Riverpod provider
// ---------------------------------------------------------------------------

/// Fetches full-text search results from the SKComm daemon.
/// Returns an empty list when the query is blank or the daemon is offline.
final _searchProvider =
    FutureProvider.family<List<_SearchResult>, String>((ref, query) async {
  if (query.trim().isEmpty) return [];
  final client = ref.read(skcommClientProvider);
  try {
    final raw = await client.searchMessages(query.trim());
    return raw.map(_SearchResult.fromJson).toList();
  } catch (_) {
    return [];
  }
});

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

/// Full-text message search screen.
///
/// Submitting the query (Enter or the search icon button) triggers a fetch
/// against the SKComm daemon's `/api/v1/search` endpoint. Each result shows
/// the sender (peer), message date, and a snippet with the query highlighted.
/// Tapping a result navigates to the conversation with that peer.
class SearchScreen extends ConsumerStatefulWidget {
  const SearchScreen({super.key});

  @override
  ConsumerState<SearchScreen> createState() => _SearchScreenState();
}

class _SearchScreenState extends ConsumerState<SearchScreen> {
  final _controller = TextEditingController();
  String _submittedQuery = '';

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _submit() {
    final q = _controller.text.trim();
    if (q != _submittedQuery) setState(() => _submittedQuery = q);
  }

  @override
  Widget build(BuildContext context) {
    final resultsAsync = ref.watch(_searchProvider(_submittedQuery));

    return Scaffold(
      appBar: GlassDecorations.appBar(
        title: '',
        titleWidget: TextField(
          controller: _controller,
          autofocus: true,
          textInputAction: TextInputAction.search,
          style: const TextStyle(
            color: SovereignGlassTheme.textPrimary,
            fontSize: 16,
          ),
          decoration: const InputDecoration(
            hintText: 'Search messages…',
            hintStyle: TextStyle(color: SovereignGlassTheme.textSecondary),
            border: InputBorder.none,
          ),
          onSubmitted: (_) => _submit(),
          onChanged: (v) {
            if (v.isEmpty) setState(() => _submittedQuery = '');
          },
        ),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () => context.pop(),
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.search),
            onPressed: _submit,
          ),
        ],
      ),
      body: resultsAsync.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (_, __) => const Center(
          child: Text(
            'Search unavailable — daemon offline',
            style: TextStyle(color: SovereignGlassTheme.textSecondary),
          ),
        ),
        data: (results) => _buildBody(results),
      ),
    );
  }

  Widget _buildBody(List<_SearchResult> results) {
    if (_submittedQuery.isEmpty) {
      return Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(
              Icons.manage_search,
              size: 48,
              color: SovereignGlassTheme.textSecondary,
            ),
            const SizedBox(height: 16),
            const Text(
              'Type a query and press Enter',
              style: TextStyle(color: SovereignGlassTheme.textSecondary),
            ),
          ],
        ),
      );
    }

    if (results.isEmpty) {
      return Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(
              Icons.search_off,
              size: 48,
              color: SovereignGlassTheme.textSecondary,
            ),
            const SizedBox(height: 16),
            Text(
              'No results for "$_submittedQuery"',
              style:
                  const TextStyle(color: SovereignGlassTheme.textSecondary),
            ),
          ],
        ),
      );
    }

    return ListView.builder(
      padding: const EdgeInsets.symmetric(vertical: 8),
      itemCount: results.length,
      itemBuilder: (context, index) {
        final r = results[index];
        return _ResultTile(
          result: r,
          query: _submittedQuery,
          onTap: () {
            final dest = r.conversationId.isNotEmpty
                ? r.conversationId
                : r.senderId;
            context.go('/conversation/$dest');
          },
        );
      },
    );
  }
}

// ---------------------------------------------------------------------------
// Result tile
// ---------------------------------------------------------------------------

class _ResultTile extends StatelessWidget {
  final _SearchResult result;
  final String query;
  final VoidCallback onTap;

  const _ResultTile({
    required this.result,
    required this.query,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final dateStr = result.timestamp != null
        ? DateFormat('MMM d, HH:mm').format(result.timestamp!)
        : '';
    final snippet = result.content.length > 140
        ? '${result.content.substring(0, 140)}…'
        : result.content;

    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      decoration: SovereignGlassTheme.glassDecoration(),
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          onTap: onTap,
          borderRadius:
              BorderRadius.circular(SovereignGlassTheme.borderRadius),
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        result.senderId,
                        style: const TextStyle(
                          fontSize: 14,
                          fontWeight: FontWeight.w600,
                          color: SovereignGlassTheme.textPrimary,
                        ),
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                    Text(
                      dateStr,
                      style: const TextStyle(
                        fontSize: 12,
                        color: SovereignGlassTheme.textTertiary,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 4),
                _HighlightText(text: snippet, query: query),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Highlighted text widget
// ---------------------------------------------------------------------------

/// Renders [text] with every occurrence of [query] highlighted in accent green.
class _HighlightText extends StatelessWidget {
  final String text;
  final String query;

  const _HighlightText({required this.text, required this.query});

  @override
  Widget build(BuildContext context) {
    const base = TextStyle(
      fontSize: 13,
      color: SovereignGlassTheme.textSecondary,
    );
    if (query.isEmpty) return Text(text, style: base);

    final lower = text.toLowerCase();
    final qLower = query.toLowerCase();
    final spans = <TextSpan>[];
    var start = 0;

    while (true) {
      final idx = lower.indexOf(qLower, start);
      if (idx == -1) {
        spans.add(TextSpan(text: text.substring(start)));
        break;
      }
      if (idx > start) {
        spans.add(TextSpan(text: text.substring(start, idx)));
      }
      spans.add(TextSpan(
        text: text.substring(idx, idx + query.length),
        style: const TextStyle(
          color: SovereignGlassTheme.accentEncrypt,
          fontWeight: FontWeight.bold,
        ),
      ));
      start = idx + query.length;
    }

    return RichText(text: TextSpan(style: base, children: spans));
  }
}
