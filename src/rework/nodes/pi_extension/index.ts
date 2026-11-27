/**
 * rework's bridge into a pi-run role (ADR 0022): the two tools a native role
 * has that pi does not. Both are thin HTTP calls to the rework daemon, so the
 * daemon stays the one control plane:
 *
 *   ask_orchestrator  -> POST /v1/runs/{run}/ask   (blocks until answered)
 *   library_search    -> GET  /v1/library/search
 *
 * Configured by env that rework sets on the pi process:
 *   REWORK_DAEMON_URL, REWORK_RUN_ID, REWORK_TOKEN (optional bearer)
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const base = (process.env.REWORK_DAEMON_URL || "").replace(/\/$/, "");
const runId = process.env.REWORK_RUN_ID || "";
const token = process.env.REWORK_TOKEN || "";

async function call(path: string, init: RequestInit = {}): Promise<any> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${base}${path}`, { ...init, headers });
  const text = await res.text();
  let data: any = {};
  try { data = JSON.parse(text); } catch { data = { error: text }; }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

const text = (t: string, isError = false) => ({
  content: [{ type: "text", text: t }], details: {}, ...(isError ? { isError: true } : {}),
});

export default function (pi: ExtensionAPI): void {
  if (!base || !runId) return; // not a rework run: register nothing
  pi.registerTool({
    name: "ask_orchestrator",
    label: "ask the orchestrator",
    description:
      "Ask the mission's orchestrator when you are genuinely blocked or need a decision " +
      "you should not make alone (scope, intent, missing access or context). Blocks until " +
      "answered. Do not ask about things you can find out yourself.",
    parameters: {
      type: "object",
      properties: {
        question: { type: "string", description: "One clear question with context." },
        kind: { type: "string", enum: ["question", "blocked", "needs_context", "needs_model"] },
      },
      required: ["question"],
      additionalProperties: false,
    } as any,
    async execute(_id: string, params: any) {
      try {
        const out = await call(`/v1/runs/${encodeURIComponent(runId)}/ask`, {
          method: "POST",
          body: JSON.stringify({ question: params.question, kind: params.kind || "question" }),
        });
        return text(String(out.answer ?? ""));
      } catch (e: any) {
        return text(`error: ${e.message}`, true);
      }
    },
  } as any);
  pi.registerTool({
    name: "library_search",
    label: "search the rework library",
    description:
      "Search the team's library of skills, tools, reports and memories from earlier " +
      "missions. Use it before re-deriving something a past mission may have recorded.",
    parameters: {
      type: "object",
      properties: { query: { type: "string" }, type: { type: "string" } },
      required: ["query"],
      additionalProperties: false,
    } as any,
    async execute(_id: string, params: any) {
      try {
        const q = new URLSearchParams({ q: params.query });
        if (params.type) q.set("type", params.type);
        const out = await call(`/v1/library/search?${q}`);
        const items = (out.items || []) as any[];
        if (!items.length) return text("no library items matched");
        return text(items.map((i) => `${i.id} [${i.type}] ${i.name}: ${i.description}\n${i.body}`).join("\n\n"));
      } catch (e: any) {
        return text(`error: ${e.message}`, true);
      }
    },
  } as any);
}
