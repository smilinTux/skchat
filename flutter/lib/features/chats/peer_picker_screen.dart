import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import '../../core/router/app_router.dart';
import '../../core/theme/theme.dart';
import '../../models/conversation.dart';
import '../../services/skcomm_client.dart';
import 'chats_provider.dart';
import 'peer_picker_provider.dart';

/// Well-known agent names — same set as chats_provider.dart.
const _knownAgents = {'lumina', 'jarvis', 'opus', 'ava', 'ara'};

Color? _agentSoulColor(String name) {
  switch (name.toLowerCase()) {
    case 'lumina':
      return SovereignColors.soulLumina;
    case 'jarvis':
      return SovereignColors.soulJarvis;
    case 'chef':
      return SovereignColors.soulChef;
    default:
      return null;
  }
}

/// Peer picker screen — discovers peers via SKComm and lets the user
/// start a new 1:1 encrypted conversation.
class PeerPickerScreen extends ConsumerStatefulWidget {
  const PeerPickerScreen({super.key});

  @override
  ConsumerState<PeerPickerScreen> createState() => _PeerPickerScreenState();
}

class _PeerPickerScreenState extends ConsumerState<PeerPickerScreen> {
  final _searchController = TextEditingController();
  String _query = '';

  @override
  void dispose() {
    _searchController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final peersAsync = ref.watch(peerPickerProvider);
    final tt = Theme.of(context).textTheme;

    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      appBar: AppBar(
        backgroundColor: SovereignColors.surfaceBase,
        title: Text(
          'New Message',
          style: tt.displayLarge?.copyWith(fontSize: 20),
        ),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back_rounded),
          onPressed: () => context.pop(),
        ),
      ),
      body: Column(
        children: [
          _buildSearchBar(tt),
          Expanded(
            child: peersAsync.when(
              loading: () => _buildLoading(),
              error: (err, _) => _buildError(tt, err),
              data: (peers) => _buildPeerList(peers, tt),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildSearchBar(TextTheme tt) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      child: TextField(
        controller: _searchController,
        autofocus: true,
        style: tt.bodyLarge,
        decoration: InputDecoration(
          hintText: 'Search peers...',
          hintStyle: tt.bodyLarge?.copyWith(
            color: SovereignColors.textTertiary,
          ),
          prefixIcon: const Icon(
            Icons.search_rounded,
            color: SovereignColors.textSecondary,
          ),
          suffixIcon: _query.isNotEmpty
              ? IconButton(
                  icon: const Icon(
                    Icons.clear_rounded,
                    color: SovereignColors.textSecondary,
                  ),
                  onPressed: () {
                    _searchController.clear();
                    setState(() => _query = '');
                  },
                )
              : null,
          filled: true,
          fillColor: SovereignColors.surfaceRaised,
          border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(16),
            borderSide: const BorderSide(
              color: SovereignColors.surfaceGlassBorder,
            ),
          ),
          enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(16),
            borderSide: const BorderSide(
              color: SovereignColors.surfaceGlassBorder,
            ),
          ),
          focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(16),
            borderSide: const BorderSide(
              color: SovereignColors.textSecondary,
            ),
          ),
          contentPadding: const EdgeInsets.symmetric(vertical: 12),
        ),
        onChanged: (value) => setState(() => _query = value.toLowerCase()),
      ),
    );
  }

  Widget _buildLoading() {
    return const Center(
      child: CircularProgressIndicator(
        color: SovereignColors.soulLumina,
      ),
    );
  }

  Widget _buildError(TextTheme tt, Object error) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(
              Icons.cloud_off_rounded,
              size: 48,
              color: SovereignColors.textTertiary,
            ),
            const SizedBox(height: 16),
            Text(
              'SKComm daemon unreachable',
              style: tt.titleMedium,
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 8),
            Text(
              'Start the daemon to discover peers.',
              style: tt.bodyMedium?.copyWith(
                color: SovereignColors.textSecondary,
              ),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 24),
            FilledButton.icon(
              onPressed: () => ref.read(peerPickerProvider.notifier).refresh(),
              icon: const Icon(Icons.refresh_rounded),
              label: const Text('Retry'),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildPeerList(List<PeerInfo> peers, TextTheme tt) {
    // Filter by search query.
    final filtered = _query.isEmpty
        ? peers
        : peers
            .where((p) => p.name.toLowerCase().contains(_query))
            .toList();

    if (filtered.isEmpty) {
      return _buildEmpty(tt);
    }

    // Group into online and offline sections.
    final online = filtered
        .where((p) => PeerPickerNotifier.isOnline(p))
        .toList();
    final offline = filtered
        .where((p) => !PeerPickerNotifier.isOnline(p))
        .toList();

    return ListView(
      padding: const EdgeInsets.only(bottom: 32),
      children: [
        if (online.isNotEmpty) ...[
          _buildSectionHeader(tt, 'Online', online.length),
          ...online.map((p) => _buildPeerTile(p, tt)),
        ],
        if (offline.isNotEmpty) ...[
          _buildSectionHeader(tt, 'Offline', offline.length),
          ...offline.map((p) => _buildPeerTile(p, tt)),
        ],
      ],
    );
  }

  Widget _buildEmpty(TextTheme tt) {
    return Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const Icon(
            Icons.person_search_rounded,
            size: 48,
            color: SovereignColors.textTertiary,
          ),
          const SizedBox(height: 16),
          Text(
            _query.isNotEmpty ? 'No peers match "$_query"' : 'No peers found',
            style: tt.titleMedium,
          ),
          const SizedBox(height: 8),
          Text(
            'Peers appear when connected via SKComm.',
            style: tt.bodyMedium?.copyWith(
              color: SovereignColors.textSecondary,
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildSectionHeader(TextTheme tt, String label, int count) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
      child: Text(
        '$label ($count)',
        style: tt.labelMedium?.copyWith(
          color: SovereignColors.textSecondary,
          letterSpacing: 1.2,
        ),
      ),
    );
  }

  Widget _buildPeerTile(PeerInfo peer, TextTheme tt) {
    final name = peer.name;
    final lowerName = name.toLowerCase();
    final isAgent = _knownAgents.contains(lowerName);
    final soulColor = _agentSoulColor(lowerName) ??
        SovereignColors.fromFingerprint(peer.fingerprint ?? lowerName);
    final isOnline = PeerPickerNotifier.isOnline(peer);

    // Derive initials from display name.
    final parts = name.trim().split(RegExp(r'\s+'));
    final initials = parts.length >= 2
        ? '${parts[0][0]}${parts[1][0]}'.toUpperCase()
        : name.isNotEmpty
            ? name[0].toUpperCase()
            : '?';

    return GlassCard(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 3),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      borderRadius: 14,
      onTap: () => _selectPeer(peer),
      child: Row(
        children: [
          // Soul avatar
          SoulAvatar(
            soulColor: soulColor,
            initials: initials,
            size: 44,
            isOnline: isOnline,
            isAgent: isAgent,
          ),
          const SizedBox(width: 14),

          // Name & status
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Flexible(
                      child: Text(
                        name,
                        style: tt.titleMedium?.copyWith(
                          fontWeight: FontWeight.w600,
                        ),
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                    if (isAgent) ...[
                      const SizedBox(width: 6),
                      Container(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 6,
                          vertical: 1,
                        ),
                        decoration: BoxDecoration(
                          color: soulColor.withValues(alpha: 0.15),
                          borderRadius: BorderRadius.circular(6),
                        ),
                        child: Text(
                          'AGENT',
                          style: TextStyle(
                            color: soulColor,
                            fontSize: 9,
                            fontWeight: FontWeight.w700,
                            letterSpacing: 0.8,
                          ),
                        ),
                      ),
                    ],
                  ],
                ),
                const SizedBox(height: 2),
                Text(
                  _statusText(peer, isOnline),
                  style: tt.bodySmall?.copyWith(
                    color: isOnline
                        ? SovereignColors.accentEncrypt
                        : SovereignColors.textTertiary,
                  ),
                ),
              ],
            ),
          ),

          // Encryption indicator
          const EncryptBadge(size: 14),
        ],
      ),
    );
  }

  String _statusText(PeerInfo peer, bool isOnline) {
    if (isOnline) return 'Online';
    if (peer.transports.isNotEmpty) {
      return 'via ${peer.transports.first}';
    }
    if (peer.lastSeen != null) {
      final diff = DateTime.now().difference(peer.lastSeen!);
      if (diff.inHours < 24) return 'Last seen ${diff.inHours}h ago';
      return 'Last seen ${diff.inDays}d ago';
    }
    return 'Discovered';
  }

  void _selectPeer(PeerInfo peer) {
    final name = peer.name;
    final lowerName = name.toLowerCase();
    final isAgent = _knownAgents.contains(lowerName);

    // Create the conversation and add it to the chats list.
    final conversation = Conversation(
      peerId: lowerName,
      displayName: name,
      lastMessage: '',
      lastMessageTime: DateTime.now(),
      soulColor: _agentSoulColor(lowerName),
      soulFingerprint: peer.fingerprint ?? lowerName,
      isOnline: PeerPickerNotifier.isOnline(peer),
      isAgent: isAgent,
      lastDeliveryStatus: 'sent',
    );

    ref.read(chatsProvider.notifier).addConversation(conversation);

    // Navigate to the new conversation, replacing the peer picker.
    context.go(AppRoutes.conversationPath(lowerName));
  }
}
