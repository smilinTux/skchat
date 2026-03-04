import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../../services/skcomm_client.dart';

// ---------------------------------------------------------------------------
// Provider

/// Polls GET /api/v1/file_status?transfer_id={id} every 2 seconds.
///
/// Yields the latest [FileTransferStatus] or null on transient errors.
/// The stream stops automatically once the transfer reaches a terminal state
/// ('completed' or 'failed').  Auto-disposes when no widget is watching.
final fileTransferStatusProvider =
    StreamProvider.family.autoDispose<FileTransferStatus?, String>(
  (ref, transferId) async* {
    final client = ref.watch(skcommClientProvider);
    while (true) {
      FileTransferStatus? status;
      try {
        status = await client.getFileStatus(transferId);
      } catch (_) {
        // Daemon temporarily unreachable — yield null, keep polling.
        status = null;
      }
      yield status;
      if (status != null && status.isTerminal) break;
      await Future.delayed(const Duration(seconds: 2));
    }
  },
);

// ---------------------------------------------------------------------------
// Widget

/// Renders a file transfer card inside a chat bubble.
///
/// Displays filename, a [LinearProgressIndicator], transfer percent, and
/// estimated speed.  Uses [fileTransferStatusProvider] to poll every 2 s.
class FileTransferBubble extends ConsumerWidget {
  const FileTransferBubble({
    super.key,
    required this.transferId,
    required this.fileName,
    required this.fileSize,
    required this.soulColor,
  });

  final String transferId;
  final String fileName;

  /// Hint size shown before the first poll completes (may be 0 if unknown).
  final int fileSize;

  final Color soulColor;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final statusAsync = ref.watch(fileTransferStatusProvider(transferId));
    final tt = Theme.of(context).textTheme;

    return statusAsync.when(
      loading: () => _buildCard(
        tt: tt,
        fileName: fileName,
        fileSize: fileSize,
        progress: 0.0,
        speedLabel: '',
        percentLabel: '0%',
        statusIcon: _Spinner(color: soulColor),
        soulColor: soulColor,
      ),
      error: (err, _) => _buildCard(
        tt: tt,
        fileName: fileName,
        fileSize: fileSize,
        progress: 0.0,
        speedLabel: '',
        percentLabel: 'Error',
        statusIcon: Icon(
          Icons.error_outline_rounded,
          size: 16,
          color: Colors.red.shade300,
        ),
        soulColor: soulColor,
      ),
      data: (status) {
        if (status == null) {
          // Transient polling error — show spinner with last-known filename.
          return _buildCard(
            tt: tt,
            fileName: fileName,
            fileSize: fileSize,
            progress: 0.0,
            speedLabel: '',
            percentLabel: '–',
            statusIcon: _Spinner(color: soulColor),
            soulColor: soulColor,
          );
        }

        final Widget statusIcon;
        if (status.isCompleted) {
          statusIcon = Icon(
            Icons.check_circle_rounded,
            size: 16,
            color: soulColor,
          );
        } else if (status.isFailed) {
          statusIcon = Icon(
            Icons.cancel_rounded,
            size: 16,
            color: Colors.red.shade300,
          );
        } else {
          statusIcon = _Spinner(color: soulColor);
        }

        final String percentLabel;
        if (status.isCompleted) {
          percentLabel = 'Done';
        } else if (status.isFailed) {
          percentLabel = 'Failed';
        } else {
          percentLabel =
              '${(status.progress * 100).toStringAsFixed(0)}%';
        }

        return _buildCard(
          tt: tt,
          fileName: status.fileName.isNotEmpty ? status.fileName : fileName,
          fileSize: status.fileSize > 0 ? status.fileSize : fileSize,
          progress: status.progress,
          speedLabel: status.isTerminal ? '' : status.speedLabel,
          percentLabel: percentLabel,
          statusIcon: statusIcon,
          soulColor: soulColor,
        );
      },
    );
  }

  Widget _buildCard({
    required TextTheme tt,
    required String fileName,
    required int fileSize,
    required double progress,
    required String speedLabel,
    required String percentLabel,
    required Widget statusIcon,
    required Color soulColor,
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        // ── Filename row ─────────────────────────────────────────────────
        Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.insert_drive_file_rounded,
              size: 16,
              color: soulColor.withValues(alpha: 0.8),
            ),
            const SizedBox(width: 6),
            Flexible(
              child: Text(
                fileName,
                style:
                    tt.bodyMedium?.copyWith(fontWeight: FontWeight.w600),
                overflow: TextOverflow.ellipsis,
                maxLines: 1,
              ),
            ),
            const SizedBox(width: 6),
            statusIcon,
          ],
        ),

        const SizedBox(height: 8),

        // ── Progress bar ─────────────────────────────────────────────────
        ClipRRect(
          borderRadius: BorderRadius.circular(4),
          child: LinearProgressIndicator(
            value: progress,
            minHeight: 5,
            backgroundColor: soulColor.withValues(alpha: 0.15),
            valueColor: AlwaysStoppedAnimation<Color>(soulColor),
          ),
        ),

        const SizedBox(height: 6),

        // ── Stats row ─────────────────────────────────────────────────────
        Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              _formatBytes(fileSize),
              style: tt.labelSmall?.copyWith(
                color: soulColor.withValues(alpha: 0.65),
              ),
            ),
            if (speedLabel.isNotEmpty) ...[
              const SizedBox(width: 5),
              Text(
                '• $speedLabel',
                style: tt.labelSmall?.copyWith(
                  color: soulColor.withValues(alpha: 0.65),
                ),
              ),
            ],
            const Spacer(),
            Text(
              percentLabel,
              style: tt.labelSmall?.copyWith(
                fontWeight: FontWeight.w700,
                color: soulColor,
              ),
            ),
          ],
        ),
      ],
    );
  }

  static String _formatBytes(int bytes) {
    if (bytes <= 0) return '';
    if (bytes < 1024) return '$bytes B';
    if (bytes < 1024 * 1024) {
      return '${(bytes / 1024).toStringAsFixed(1)} KB';
    }
    if (bytes < 1024 * 1024 * 1024) {
      return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
    }
    return '${(bytes / (1024 * 1024 * 1024)).toStringAsFixed(2)} GB';
  }
}

// ---------------------------------------------------------------------------
// Internal helpers

class _Spinner extends StatelessWidget {
  const _Spinner({required this.color});

  final Color color;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: 14,
      height: 14,
      child: CircularProgressIndicator(
        strokeWidth: 2,
        valueColor: AlwaysStoppedAnimation<Color>(color),
      ),
    );
  }
}
