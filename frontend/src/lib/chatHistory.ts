import { parseDate } from "./locale";
import type { ChatMessage, ConversationDetail, KnowledgeSource } from "../types";

/** 仅用于选择浏览器保存的会话指针，所有会话访问仍由服务端验签与鉴权。 */
export function historyKey(token: string | null): string | null {
  try {
    const payload = JSON.parse(atob(token!.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return typeof payload.sub === "string" ? `acme.activeConversation.${payload.sub}` : null;
  } catch { return null; }
}

export function restoreHistory(detail: ConversationDetail) {
  const messages: ChatMessage[] = detail.messages.filter((m) => m.role === "user" || m.role === "assistant")
    .map((m) => ({ id: `saved-${m.id}`, role: m.role as "user" | "assistant", content: m.content, at: parseDate(m.created_at).getTime() }));
  const lastUser = [...detail.messages].reverse().find((m) => m.role === "user");
  // 新接口按所属客户消息恢复最后一轮；旧服务没有关联字段时展示全部已有步骤。
  const linked = detail.events.some((e) => e.message_id != null);
  const steps = linked ? detail.events.filter((e) => e.message_id === lastUser?.id) : detail.events;
  const evidence = [...steps].reverse().find((e) => e.payload.tool === "search_knowledge" && e.step_type === "tool_result");
  const raw = evidence?.payload.result;
  const data = raw?.ok === true ? raw.data : raw;
  const sources: KnowledgeSource[] = Array.isArray(data?.sources) ? data.sources : [];
  return { messages, steps, sources, sourcesReceived: Boolean(evidence),
    startedAt: lastUser ? parseDate(lastUser.created_at).getTime() : null,
    endedAt: messages[messages.length - 1]?.at ?? null };
}
