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

function runCli(args: string): { ok: boolean; output: string } {
  try {
    const raw = execSync(`${SKCHAT_BIN} ${args}`, {
      encoding: "utf-8",
      timeout: EXEC_TIMEOUT,
      env: {
        ...process.env,
        PATH: `${process.env.HOME}/.local/bin:${process.env.HOME}/.skenv/bin:${process.env.PATH}`,
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

    api.logger.info?.("SKChat plugin registered (15 tools + /skchat command)");
  },
};

export default skchatPlugin;
