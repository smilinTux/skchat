/**
 * SKChat — OpenClaw Plugin
 *
 * Registers agent tools that wrap the skchat CLI so Lumina and other
 * OpenClaw agents can use the sovereign chat system as first-class tools.
 *
 * Requires: skchat CLI on PATH (typically via ~/.skenv/bin/skchat)
 */

import { execSync } from "node:child_process";
import type { OpenClawPluginApi, AnyAgentTool } from "openclaw/plugin-sdk";
import { emptyPluginConfigSchema } from "openclaw/plugin-sdk";

const SKCHAT_BIN = process.env.SKCHAT_BIN || "skchat";
const EXEC_TIMEOUT = 30_000;
const IS_WIN = process.platform === "win32";

function skenvPath(): string {
  if (IS_WIN) {
    const local = process.env.LOCALAPPDATA || "";
    return `${local}\\skenv\\Scripts`;
  }
  const home = process.env.HOME || "";
  return `${home}/.local/bin:${home}/.skenv/bin`;
}

function runCli(args: string): { ok: boolean; output: string } {
  try {
    const raw = execSync(`${SKCHAT_BIN} ${args}`, {
      encoding: "utf-8",
      timeout: EXEC_TIMEOUT,
      env: {
        ...process.env,
        PATH: `${skenvPath()}${IS_WIN ? ";" : ":"}${process.env.PATH}`,
      },
    }).trim();
    return { ok: true, output: raw };
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    return { ok: false, output: msg };
  }
}

function textResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

function escapeShellArg(s: string): string {
  return `'${s.replace(/'/g, "'\\''")}'`;
}

// ── Tool definitions ────────────────────────────────────────────────────

function createSKChatSendTool() {
  return {
    name: "skchat_send",
    label: "SKChat Send",
    description: "Send a message to a peer.",
    parameters: {
      type: "object",
      required: ["recipient", "message"],
      properties: {
        recipient: { type: "string", description: "Recipient peer ID or name." },
        message: { type: "string", description: "Message content." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const to = escapeShellArg(String(params.recipient ?? ""));
      const msg = escapeShellArg(String(params.message ?? ""));
      const result = runCli(`send ${to} ${msg}`);
      return textResult(result.output);
    },
  };
}

function createSKChatInboxTool() {
  return {
    name: "skchat_inbox",
    label: "SKChat Inbox",
    description: "Check inbox for new and unread messages.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("inbox");
      return textResult(result.output);
    },
  };
}

function createSKChatHistoryTool() {
  return {
    name: "skchat_history",
    label: "SKChat History",
    description: "View conversation history with a specific peer.",
    parameters: {
      type: "object",
      required: ["peer"],
      properties: {
        peer: { type: "string", description: "Peer ID or name." },
        limit: { type: "number", description: "Max messages to return (default: 20)." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const peer = escapeShellArg(String(params.peer ?? ""));
      let cmd = `history ${peer}`;
      if (typeof params.limit === "number") cmd += ` --limit ${params.limit}`;
      const result = runCli(cmd);
      return textResult(result.output);
    },
  };
}

function createSKChatSearchTool() {
  return {
    name: "skchat_search",
    label: "SKChat Search",
    description: "Full-text search across all messages.",
    parameters: {
      type: "object",
      required: ["query"],
      properties: {
        query: { type: "string", description: "Search query." },
        limit: { type: "number", description: "Max results (default: 10)." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const query = escapeShellArg(String(params.query ?? ""));
      let cmd = `search ${query}`;
      if (typeof params.limit === "number") cmd += ` --limit ${params.limit}`;
      const result = runCli(cmd);
      return textResult(result.output);
    },
  };
}

function createSKChatWhoTool() {
  return {
    name: "skchat_who",
    label: "SKChat Who",
    description: "Show who is currently online.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("who");
      return textResult(result.output);
    },
  };
}

function createSKChatGroupSendTool() {
  return {
    name: "skchat_group_send",
    label: "SKChat Group Send",
    description: "Send a message to a group.",
    parameters: {
      type: "object",
      required: ["group_id", "message"],
      properties: {
        group_id: { type: "string", description: "Group ID." },
        message: { type: "string", description: "Message content." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const gid = escapeShellArg(String(params.group_id ?? ""));
      const msg = escapeShellArg(String(params.message ?? ""));
      const result = runCli(`group send ${gid} ${msg}`);
      return textResult(result.output);
    },
  };
}

function createSKChatGroupListTool() {
  return {
    name: "skchat_group_list",
    label: "SKChat Group List",
    description: "List all groups.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("group list");
      return textResult(result.output);
    },
  };
}

function createSKChatGroupCreateTool() {
  return {
    name: "skchat_group_create",
    label: "SKChat Group Create",
    description: "Create a new group.",
    parameters: {
      type: "object",
      required: ["name"],
      properties: {
        name: { type: "string", description: "Group name." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const name = escapeShellArg(String(params.name ?? ""));
      const result = runCli(`group create ${name}`);
      return textResult(result.output);
    },
  };
}

function createSKChatGroupMembersTool() {
  return {
    name: "skchat_group_members",
    label: "SKChat Group Members",
    description: "List members of a group.",
    parameters: {
      type: "object",
      required: ["group_id"],
      properties: {
        group_id: { type: "string", description: "Group ID." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const gid = escapeShellArg(String(params.group_id ?? ""));
      const result = runCli(`group members ${gid}`);
      return textResult(result.output);
    },
  };
}

function createSKChatDaemonStatusTool() {
  return {
    name: "skchat_daemon_status",
    label: "SKChat Daemon Status",
    description: "Check the skchat daemon health and status.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("daemon status");
      return textResult(result.output);
    },
  };
}

function createSKChatThreadsTool() {
  return {
    name: "skchat_threads",
    label: "SKChat Threads",
    description: "List active conversation threads.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("threads");
      return textResult(result.output);
    },
  };
}

function createSKChatReactTool() {
  return {
    name: "skchat_react",
    label: "SKChat React",
    description: "Add an emoji reaction to a message.",
    parameters: {
      type: "object",
      required: ["msg_id", "emoji"],
      properties: {
        msg_id: { type: "string", description: "Message ID to react to." },
        emoji: { type: "string", description: "Emoji reaction (e.g. heart, thumbsup)." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const msgId = escapeShellArg(String(params.msg_id ?? ""));
      const emoji = escapeShellArg(String(params.emoji ?? ""));
      const result = runCli(`react ${msgId} ${emoji}`);
      return textResult(result.output);
    },
  };
}

function createSKChatSendFileTool() {
  return {
    name: "skchat_send_file",
    label: "SKChat Send File",
    description: "Send a file to a peer.",
    parameters: {
      type: "object",
      required: ["to", "path"],
      properties: {
        to: { type: "string", description: "Recipient peer ID or name." },
        path: { type: "string", description: "Path to the file to send." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const to = escapeShellArg(String(params.to ?? ""));
      const path = escapeShellArg(String(params.path ?? ""));
      const result = runCli(`send-file ${to} ${path}`);
      return textResult(result.output);
    },
  };
}

function createSKChatPresenceTool() {
  return {
    name: "skchat_presence",
    label: "SKChat Presence",
    description: "Show presence state of all known peers.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("presence");
      return textResult(result.output);
    },
  };
}

function createSKChatStatusTool() {
  return {
    name: "skchat_status",
    label: "SKChat Status",
    description: "Show overall chat system status.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("status");
      return textResult(result.output);
    },
  };
}

// ── Voice tools ─────────────────────────────────────────────────────────

function runPython(script: string): { ok: boolean; output: string } {
  const home = process.env.HOME || "";
  const pythonBin = `${home}/.skenv/bin/python`;
  try {
    const raw = execSync(`${pythonBin} -c ${escapeShellArg(script)}`, {
      encoding: "utf-8",
      timeout: 120_000,  // voice ops can be slow on CPU
      env: {
        ...process.env,
        PATH: `${skenvPath()}:/usr/bin:/usr/local/bin:${process.env.PATH}`,
      },
    }).trim();
    return { ok: true, output: raw };
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    return { ok: false, output: msg };
  }
}

function createTranscribeAudioFileTool() {
  return {
    name: "skchat_transcribe_audio",
    label: "SKChat Transcribe Audio",
    description:
      "Transcribe an audio file (OGG, WAV, MP3, etc.) with rich metadata. " +
      "Uses SenseVoice (preferred) for text + emotion (happy/sad/angry/neutral) " +
      "+ audio events (laughter, applause). Falls back to Whisper. " +
      "Use this to process incoming voice messages from Telegram.",
    parameters: {
      type: "object",
      required: ["file_path"],
      properties: {
        file_path: {
          type: "string",
          description: "Absolute path to the audio file to transcribe.",
        },
        backend: {
          type: "string",
          description:
            "STT backend: 'sensevoice' (rich), 'whisper' (text only), " +
            "or 'auto' (try sensevoice first). Default: auto.",
        },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const filePath = String(params.file_path ?? "");
      const backend = String(params.backend ?? "auto");
      if (!filePath) return textResult(JSON.stringify({ error: "file_path required" }));

      const script = `
import json, os, re, subprocess, tempfile, sys

file_path = ${JSON.stringify(filePath)}
backend = ${JSON.stringify(backend)}

if not os.path.isfile(file_path):
    print(json.dumps({"error": f"File not found: {file_path}"}))
    sys.exit(0)

# Convert to WAV if needed
wav_path = file_path
tmp_wav = None
if not file_path.lower().endswith('.wav'):
    tmp_wav = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    tmp_wav.close()
    wav_path = tmp_wav.name
    subprocess.run(['ffmpeg', '-i', file_path, '-ar', '16000', '-ac', '1', '-f', 'wav', wav_path, '-y'],
                   capture_output=True, timeout=30)

result = {"transcribed": False, "text": "", "backend": "none"}

# Try SenseVoice
if backend in ('sensevoice', 'auto'):
    try:
        from funasr import AutoModel
        sv = AutoModel(model='iic/SenseVoiceSmall', trust_remote_code=True, device='cpu', disable_update=True)
        res = sv.generate(input=wav_path, language='auto', use_itn=True)
        raw = res[0].get('text', '') if res else ''
        tokens = re.findall(r'<\\|([^|]+)\\|>', raw)
        text = re.sub(r'<\\|[^|]+\\|>', '', raw).strip()
        emotion = tokens[1].lower() if len(tokens) >= 2 else 'unknown'
        events = []
        if len(tokens) >= 3 and tokens[2].lower() != 'speech':
            events.append(tokens[2].lower())
        result = {"transcribed": bool(text), "text": text, "emotion": emotion, "events": events, "backend": "sensevoice"}

        # Try emotion2vec
        try:
            emo = AutoModel(model='iic/emotion2vec_plus_large', trust_remote_code=True, device='cpu', disable_update=True)
            emo_res = emo.generate(input=wav_path, granularity='utterance', extract_embedding=False)
            if emo_res and emo_res[0].get('labels'):
                labels = emo_res[0]['labels']
                scores = emo_res[0]['scores']
                result['emotion_detailed'] = labels[scores.index(max(scores))]
        except:
            pass
    except ImportError:
        pass
    except Exception as e:
        result['sensevoice_error'] = str(e)

# Fallback to Whisper
if not result.get('transcribed') and backend in ('whisper', 'auto'):
    try:
        import whisper
        model = whisper.load_model('base')
        res = model.transcribe(wav_path)
        text = res.get('text', '').strip()
        result = {"transcribed": bool(text), "text": text, "backend": "whisper"}
    except:
        pass

if tmp_wav:
    os.unlink(wav_path)

print(json.dumps(result))
`;

      const res = runPython(script);
      return textResult(res.output);
    },
  };
}

function createGenerateVoiceMessageTool() {
  return {
    name: "skchat_generate_voice",
    label: "SKChat Generate Voice Message",
    description:
      "Generate an OGG Opus voice message file from text using Piper TTS. " +
      "Returns the file path to the generated audio, ready to send via " +
      "Telegram. Use the returned file_path as a media attachment.",
    parameters: {
      type: "object",
      required: ["text"],
      properties: {
        text: {
          type: "string",
          description: "The text to synthesize into a voice message.",
        },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const text = String(params.text ?? "");
      if (!text) return textResult(JSON.stringify({ error: "text required" }));

      const script = `
import json, os, subprocess, time

text = ${JSON.stringify(text)}
piper_bin = '/usr/local/piper/piper'
voice_model = os.path.expanduser('~/.local/share/piper-voices/en_US-amy-medium.onnx')

if not os.path.isfile(piper_bin):
    print(json.dumps({"generated": False, "error": "Piper not installed"}))
    exit(0)
if not os.path.isfile(voice_model):
    print(json.dumps({"generated": False, "error": "Voice model not found"}))
    exit(0)

output_dir = '/tmp/piper-tts'
os.makedirs(output_dir, exist_ok=True)
ts = int(time.time() * 1000)
wav_path = f'{output_dir}/lumina-voice-{ts}.wav'
ogg_path = f'{output_dir}/lumina-voice-{ts}.ogg'

proc = subprocess.run([piper_bin, '-m', voice_model, '-f', wav_path],
                      input=text, capture_output=True, text=True, timeout=30)
if proc.returncode != 0:
    print(json.dumps({"generated": False, "error": proc.stderr}))
    exit(0)

subprocess.run(['ffmpeg', '-i', wav_path, '-c:a', 'libopus', '-b:a', '64k', ogg_path, '-y'],
               capture_output=True, timeout=30)
os.unlink(wav_path)

if os.path.isfile(ogg_path):
    print(json.dumps({"generated": True, "file_path": ogg_path}))
else:
    print(json.dumps({"generated": False, "error": "OGG not created"}))
`;

      const res = runPython(script);
      return textResult(res.output);
    },
  };
}

// ── Plugin registration ─────────────────────────────────────────────────

const skchatPlugin = {
  id: "skchat",
  name: "SKChat",
  description:
    "Sovereign chat system — messaging, groups, threads, file transfer, and presence.",
  configSchema: emptyPluginConfigSchema(),

  register(api: OpenClawPluginApi) {
    const tools = [
      createSKChatSendTool(),
      createSKChatInboxTool(),
      createSKChatHistoryTool(),
      createSKChatSearchTool(),
      createSKChatWhoTool(),
      createSKChatGroupSendTool(),
      createSKChatGroupListTool(),
      createSKChatGroupCreateTool(),
      createSKChatGroupMembersTool(),
      createSKChatDaemonStatusTool(),
      createSKChatThreadsTool(),
      createSKChatReactTool(),
      createSKChatSendFileTool(),
      createSKChatPresenceTool(),
      createSKChatStatusTool(),
      createTranscribeAudioFileTool(),
      createGenerateVoiceMessageTool(),
    ];

    for (const tool of tools) {
      api.registerTool(tool as unknown as AnyAgentTool, {
        names: [tool.name],
        optional: true,
      });
    }

    api.registerCommand({
      name: "skchat",
      description: "Run skchat CLI commands. Usage: /skchat <subcommand> [args]",
      acceptsArgs: true,
      handler: async (ctx) => {
        const args = ctx.args?.trim() ?? "status";
        const result = runCli(args);
        return { text: result.output };
      },
    });

    api.logger.info?.("SKChat plugin registered (17 tools + /skchat command)");
  },
};

export default skchatPlugin;
