import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { mkdirSync, writeFileSync, renameSync, existsSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { createHash } from "node:crypto";

// Observation only: no tools, prompts, UI, or task mutations.
export default function (pi: ExtensionAPI) {
  const terminal = process.env.HC_TERMINAL_ID;
  if (!terminal || !/^t[\w-]+$/.test(terminal)) return;
  const dir = join(process.env.HERDR_APP_HOME || join(homedir(), ".config", "herdr-app"), "data", "agent-todos", "pi");
  const file = join(dir, `${terminal}.json`);
  const publish = (_event: unknown, ctx: ExtensionContext) => {
    // Headless/child sessions must not replace the interactive terminal's list.
    if (ctx.mode !== "tui") return;
    let fromState: any = undefined, fromResult: any = undefined;
    let request: string | undefined, taskRequest: string | undefined;
    for (const entry of ctx.sessionManager.getBranch()) {
      if (entry.type === "message" && entry.message.role === "user") {
        const content = entry.message.content;
        const text = typeof content === "string" ? content : content.filter(b => b.type === "text").map(b => b.text).join("\n");
        if (text.trim() && !text.trimStart().startsWith("[herdr-todo-request]") && !text.trimStart().startsWith("请根据当前对话中最近一项明确的工作生成 Todo 清单。")) {
          request = createHash("sha256").update(JSON.stringify([entry.id, text])).digest("hex");
        }
      }
      if (entry.type === "custom" && entry.customType === "rpiv-todo-state" && Array.isArray((entry.data as any)?.tasks)) {
        fromState = entry.data;
        taskRequest = request;
      }
      if (entry.type === "message" && entry.message.role === "toolResult" && entry.message.toolName === "todo") {
        const details = entry.message.details;
        if (Array.isArray(details?.tasks)) {
          fromResult = details;
          taskRequest = request;
        } else if (Array.isArray(details?.todos)) {
          fromResult = { tasks: details.todos.map(({ id, text, done }: { id: number; text: string; done: boolean }) => ({
            id, subject: text, status: done ? "completed" : "pending",
          })) };
          taskRequest = request;
        }
      }
    }
    const data = { pid: process.pid, terminal, sessionId: ctx.sessionManager.getSessionId(), sessionFile: ctx.sessionManager.getSessionFile(), tasks: (fromState ?? fromResult)?.tasks ?? [], request, taskRequest, updatedAt: Date.now() };
    try { mkdirSync(dir, { recursive: true }); const tmp = `${file}.${process.pid}.tmp`; writeFileSync(tmp, JSON.stringify(data)); renameSync(tmp, file); }
    catch (e) { ctx.ui.notify(`Herdr todo bridge: ${String(e)}`, "warning"); }
  };
  for (const event of ["session_start", "session_tree", "session_compact", "turn_start", "turn_end", "agent_settled"] as const) pi.on(event, publish);
  pi.on("tool_execution_end", (event, ctx) => { if (event.toolName === "todo") publish(event, ctx); });
  pi.on("session_shutdown", (_event, ctx) => { if (ctx.mode === "tui") { try { if (existsSync(file)) unlinkSync(file); } catch {} } });
}
