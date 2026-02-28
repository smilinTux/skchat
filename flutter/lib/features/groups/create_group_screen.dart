import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import '../../core/router/app_router.dart';
import '../../core/theme/theme.dart';
import '../../models/conversation.dart';
import '../../services/skcomm_client.dart';
import '../chats/peer_picker_provider.dart';
import 'groups_provider.dart';

/// Full-screen "Create Group" flow.
///
/// Steps:
///   1. Enter name + description.
///   2. Pick initial members from known peers (optional).
///   3. Submit → calls POST /api/v1/groups via [SKCommClient].
///   4. On success: shows AES-256-GCM key distribution info.
class CreateGroupScreen extends ConsumerStatefulWidget {
  const CreateGroupScreen({super.key});

  @override
  ConsumerState<CreateGroupScreen> createState() => _CreateGroupScreenState();
}

class _CreateGroupScreenState extends ConsumerState<CreateGroupScreen> {
  final _nameController = TextEditingController();
  final _descController = TextEditingController();
  final _selectedPeerIds = <String>{};
  bool _isSubmitting = false;

  @override
  void dispose() {
    _nameController.dispose();
    _descController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;
    final peersAsync = ref.watch(peerPickerProvider);

    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      appBar: AppBar(
        backgroundColor: SovereignColors.surfaceBase,
        title: Text(
          'New Group',
          style: tt.displayLarge?.copyWith(fontSize: 20),
        ),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back_rounded),
          onPressed: () => context.pop(),
        ),
        actions: [
          _isSubmitting
              ? const Padding(
                  padding: EdgeInsets.symmetric(horizontal: 16),
                  child: SizedBox(
                    width: 20,
                    height: 20,
                    child: CircularProgressIndicator(
                      color: SovereignColors.soulLumina,
                      strokeWidth: 2,
                    ),
                  ),
                )
              : TextButton(
                  onPressed: _canSubmit ? _submit : null,
                  child: Text(
                    'Create',
                    style: TextStyle(
                      color: _canSubmit
                          ? SovereignColors.soulLumina
                          : SovereignColors.textTertiary,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
          const SizedBox(width: 4),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.only(bottom: 40),
        children: [
          _buildGroupAvatar(tt),
          _buildNameField(tt),
          _buildDescField(tt),
          _buildEncryptionInfo(tt),
          _buildMembersSection(peersAsync, tt),
        ],
      ),
    );
  }

  bool get _canSubmit =>
      _nameController.text.trim().isNotEmpty && !_isSubmitting;

  Widget _buildGroupAvatar(TextTheme tt) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 24),
      child: Center(
        child: Container(
          width: 80,
          height: 80,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: SovereignColors.soulLumina.withValues(alpha: 0.15),
            border: Border.all(
              color: SovereignColors.soulLumina.withValues(alpha: 0.4),
              width: 2,
            ),
          ),
          child: Icon(
            Icons.group_rounded,
            color: SovereignColors.soulLumina,
            size: 34,
          ),
        ),
      ),
    );
  }

  Widget _buildNameField(TextTheme tt) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
      child: TextField(
        controller: _nameController,
        autofocus: true,
        textCapitalization: TextCapitalization.words,
        style: tt.bodyLarge,
        maxLength: 64,
        decoration: InputDecoration(
          labelText: 'Group name',
          hintText: 'e.g. Penguin Kingdom',
          hintStyle: tt.bodyLarge?.copyWith(
            color: SovereignColors.textTertiary,
          ),
          filled: true,
          fillColor: SovereignColors.surfaceRaised,
          counterStyle: TextStyle(color: SovereignColors.textTertiary),
          border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(14),
            borderSide:
                const BorderSide(color: SovereignColors.surfaceGlassBorder),
          ),
          enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(14),
            borderSide:
                const BorderSide(color: SovereignColors.surfaceGlassBorder),
          ),
          focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(14),
            borderSide: const BorderSide(color: SovereignColors.soulLumina),
          ),
        ),
        onChanged: (_) => setState(() {}),
      ),
    );
  }

  Widget _buildDescField(TextTheme tt) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
      child: TextField(
        controller: _descController,
        textCapitalization: TextCapitalization.sentences,
        style: tt.bodyLarge,
        maxLength: 200,
        maxLines: 2,
        decoration: InputDecoration(
          labelText: 'Description',
          hintText: 'What is this group about? (optional)',
          hintStyle: tt.bodyLarge?.copyWith(
            color: SovereignColors.textTertiary,
          ),
          filled: true,
          fillColor: SovereignColors.surfaceRaised,
          counterStyle: TextStyle(color: SovereignColors.textTertiary),
          border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(14),
            borderSide:
                const BorderSide(color: SovereignColors.surfaceGlassBorder),
          ),
          enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(14),
            borderSide:
                const BorderSide(color: SovereignColors.surfaceGlassBorder),
          ),
          focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(14),
            borderSide: const BorderSide(color: SovereignColors.soulLumina),
          ),
        ),
      ),
    );
  }

  Widget _buildEncryptionInfo(TextTheme tt) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
      child: GlassCard(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        child: Row(
          children: [
            const EncryptBadge(size: 16),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                'All messages encrypted with AES-256-GCM. '
                'Group key distributed to members via PGP.',
                style: tt.bodySmall?.copyWith(
                  color: SovereignColors.accentEncrypt,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildMembersSection(
      AsyncValue<List<PeerInfo>> peersAsync, TextTheme tt) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 20, 16, 8),
          child: Row(
            children: [
              Text(
                'Add Members',
                style: tt.titleMedium?.copyWith(fontWeight: FontWeight.w700),
              ),
              const SizedBox(width: 8),
              Text(
                '(optional)',
                style: tt.bodySmall?.copyWith(
                  color: SovereignColors.textTertiary,
                ),
              ),
              if (_selectedPeerIds.isNotEmpty) ...[
                const Spacer(),
                Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                  decoration: BoxDecoration(
                    color: SovereignColors.soulLumina.withValues(alpha: 0.15),
                    borderRadius: BorderRadius.circular(10),
                  ),
                  child: Text(
                    '${_selectedPeerIds.length} selected',
                    style: tt.labelSmall?.copyWith(
                      color: SovereignColors.soulLumina,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
              ],
            ],
          ),
        ),
        peersAsync.when(
          loading: () => const Padding(
            padding: EdgeInsets.all(24),
            child: Center(
              child: CircularProgressIndicator(
                color: SovereignColors.soulLumina,
                strokeWidth: 2,
              ),
            ),
          ),
          error: (_, __) => Padding(
            padding: const EdgeInsets.all(16),
            child: GlassCard(
              padding: const EdgeInsets.all(16),
              child: Row(
                children: [
                  const Icon(
                    Icons.cloud_off_rounded,
                    color: SovereignColors.textTertiary,
                    size: 20,
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      'Daemon offline — you can add members after creation.',
                      style: tt.bodySmall?.copyWith(
                        color: SovereignColors.textSecondary,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
          data: (peers) => peers.isEmpty
              ? Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16),
                  child: Text(
                    'No peers discovered yet. You can add members after creation.',
                    style: tt.bodySmall?.copyWith(
                      color: SovereignColors.textSecondary,
                    ),
                  ),
                )
              : Column(
                  children: peers
                      .map((peer) => _buildPeerCheckTile(peer, tt))
                      .toList(),
                ),
        ),
      ],
    );
  }

  Widget _buildPeerCheckTile(PeerInfo peer, TextTheme tt) {
    final isSelected = _selectedPeerIds.contains(peer.name);
    final isOnline = PeerPickerNotifier.isOnline(peer);
    final soulColor = SovereignColors.fromFingerprint(
      peer.fingerprint ?? peer.name,
    );
    final initials =
        peer.name.isNotEmpty ? peer.name[0].toUpperCase() : '?';

    return GlassCard(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 3),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      borderRadius: 14,
      onTap: () {
        setState(() {
          if (isSelected) {
            _selectedPeerIds.remove(peer.name);
          } else {
            _selectedPeerIds.add(peer.name);
          }
        });
      },
      child: Row(
        children: [
          SoulAvatar(
            soulColor: soulColor,
            initials: initials,
            size: 44,
            isOnline: isOnline,
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  peer.name,
                  style: tt.titleSmall?.copyWith(fontWeight: FontWeight.w600),
                ),
                Text(
                  isOnline ? 'Online' : 'Offline',
                  style: tt.bodySmall?.copyWith(
                    color: isOnline
                        ? SovereignColors.accentEncrypt
                        : SovereignColors.textTertiary,
                    fontSize: 12,
                  ),
                ),
              ],
            ),
          ),
          AnimatedContainer(
            duration: const Duration(milliseconds: 150),
            width: 24,
            height: 24,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: isSelected
                  ? SovereignColors.soulLumina
                  : Colors.transparent,
              border: Border.all(
                color: isSelected
                    ? SovereignColors.soulLumina
                    : SovereignColors.textTertiary,
                width: 2,
              ),
            ),
            child: isSelected
                ? const Icon(Icons.check_rounded,
                    color: Colors.black, size: 14)
                : null,
          ),
        ],
      ),
    );
  }

  Future<void> _submit() async {
    final name = _nameController.text.trim();
    if (name.isEmpty) return;

    setState(() => _isSubmitting = true);

    final client = ref.read(skcommClientProvider);
    final notifier = ref.read(groupsProvider.notifier);

    try {
      final result = await client.createGroup(
        name: name,
        description: _descController.text.trim().isNotEmpty
            ? _descController.text.trim()
            : null,
        memberUris: _selectedPeerIds.toList(),
      );

      final group = Conversation(
        peerId: result.groupId,
        displayName: result.name,
        lastMessage: 'Group created',
        lastMessageTime: DateTime.now(),
        isGroup: true,
        memberCount: result.memberCount,
        lastDeliveryStatus: 'delivered',
      );
      await notifier.addGroup(group);

      if (mounted) {
        await _showKeyDistributionSheet(result);
        if (mounted) {
          context.go(AppRoutes.groupInfoPath(result.groupId));
        }
      }
    } on Object catch (e) {
      // Daemon offline or API error — create locally and let user proceed.
      final groupId = 'group-${DateTime.now().millisecondsSinceEpoch}';
      await notifier.addGroup(Conversation(
        peerId: groupId,
        displayName: name,
        lastMessage: 'Group created',
        lastMessageTime: DateTime.now(),
        isGroup: true,
        memberCount: _selectedPeerIds.length + 1,
        lastDeliveryStatus: 'delivered',
      ));

      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text('Created locally (daemon offline): $e'),
            backgroundColor: SovereignColors.surfaceRaised,
          ),
        );
        context.go(AppRoutes.groupInfoPath(groupId));
      }
    } finally {
      if (mounted) setState(() => _isSubmitting = false);
    }
  }

  Future<void> _showKeyDistributionSheet(CreateGroupResult result) async {
    await showModalBottomSheet<void>(
      context: context,
      backgroundColor: SovereignColors.surfaceRaised,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (sheetCtx) {
        final tt = Theme.of(sheetCtx).textTheme;
        return SafeArea(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 20, 20, 24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                // Handle bar
                Center(
                  child: Container(
                    width: 36,
                    height: 4,
                    decoration: BoxDecoration(
                      color: SovereignColors.textTertiary.withValues(alpha: 0.4),
                      borderRadius: BorderRadius.circular(2),
                    ),
                  ),
                ),
                const SizedBox(height: 20),
                Row(
                  children: [
                    const EncryptBadge(size: 20),
                    const SizedBox(width: 10),
                    Text(
                      'Group Key Distributed',
                      style: tt.titleMedium?.copyWith(
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 16),
                GlassCard(
                  padding: const EdgeInsets.all(14),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      _keyInfoRow(
                        tt,
                        'Algorithm',
                        result.keyAlgorithm,
                      ),
                      if (result.keyId != null) ...[
                        const SizedBox(height: 8),
                        _keyInfoRow(tt, 'Key ID', result.keyId!,
                            copyable: true),
                      ],
                      const SizedBox(height: 8),
                      _keyInfoRow(
                        tt,
                        'Group ID',
                        result.groupId,
                        copyable: true,
                      ),
                      const SizedBox(height: 8),
                      _keyInfoRow(
                        tt,
                        'Members',
                        '${result.memberCount}',
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 12),
                Text(
                  'Keys are distributed to each member via their PGP public key. '
                  'Only group members can decrypt messages.',
                  style: tt.bodySmall?.copyWith(
                    color: SovereignColors.textSecondary,
                  ),
                ),
                const SizedBox(height: 20),
                SizedBox(
                  width: double.infinity,
                  child: FilledButton.icon(
                    onPressed: () => Navigator.of(sheetCtx).pop(),
                    icon: const Icon(Icons.check_rounded, size: 18),
                    label: const Text('Got it'),
                    style: FilledButton.styleFrom(
                      backgroundColor: SovereignColors.soulLumina,
                      foregroundColor: Colors.black,
                      padding: const EdgeInsets.symmetric(vertical: 14),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(14),
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  Widget _keyInfoRow(
    TextTheme tt,
    String label,
    String value, {
    bool copyable = false,
  }) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          width: 80,
          child: Text(
            label,
            style: tt.labelSmall?.copyWith(
              color: SovereignColors.textTertiary,
              fontSize: 11,
            ),
          ),
        ),
        const SizedBox(width: 8),
        Expanded(
          child: Text(
            value,
            style: tt.bodySmall?.copyWith(
              fontFamily: 'monospace',
              color: SovereignColors.textPrimary,
              fontWeight: FontWeight.w500,
            ),
          ),
        ),
        if (copyable)
          GestureDetector(
            onTap: () => Clipboard.setData(ClipboardData(text: value)),
            child: const Icon(
              Icons.copy_rounded,
              size: 14,
              color: SovereignColors.textTertiary,
            ),
          ),
      ],
    );
  }
}
