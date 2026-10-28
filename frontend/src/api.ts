import { getToken, type Role } from "./auth";
import { apiUrl } from "./config";
import type {
  AdminStats,
  AdminTicketDetail,
  AgentEvent,
  ConversationDetail,
  ConversationSummary,
  DemoSession,
  Meta,
  Order,
  RefundConfirmation,
  TicketAccepted,
  TicketDecisionRequest,
  TicketStatus,
  TicketSummary,
  TicketDetail,
  TicketSupplementRequest,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public retryAfter?: number,
  ) {
    super(message);
  }
}

/** 业务错误转换为中文提示；未知异常不直接展示服务端或第三方的内部细节。 */
export function friendlyError(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.status) {
      case 0:
        return "无法连接服务器，请检查网络后重试。";
      case 401:
        return "登录已失效，请重新登录；演示模式下可重置演示环境。";
      case 403:
        return "当前身份没有访问权限。";
      case 404:
        return "未找到请求的数据，请尝试新建对话。";
      case 409:
        return "上一条消息仍在处理中，请稍候。";
      case 400:
      case 429:
        // 只展示本项目后端给出的中文业务提示，兼容旧版后端的英文错误。
        return /[\u4e00-\u9fff]/.test(err.message)
          ? err.message
          : err.status === 429 ? "请求过于频繁或会话已达上限，请稍后重试或新建对话。" : "请求无效，请检查输入。";
      case 413:
        return "发送的内容过大，请缩短消息后重试。";
      case 422:
        return "输入格式不正确，请检查后重试。";
      case 503:
        return `服务繁忙，请在 ${err.retryAfter ?? 5} 秒后重试。`;
    }
  }
  return "服务暂时出现异常，请稍后重试。";
}

async function request(role: Role | null, url: string, init: RequestInit = {}): Promise<Response> {
  const token = role ? getToken(role) : null;
  const headers = new Headers(init.headers);
  // 客户和管理员使用各自的登录凭证；后端校验后才决定身份，不能靠聊天文本切换。
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");
  let res: Response;
  try {
    res = await fetch(apiUrl(url), { ...init, headers });
  } catch (err) {
    if ((err as Error)?.name === "AbortError") throw err;
    throw new ApiError(0, "无法连接服务器，请检查网络后重试。");
  }
  if (!res.ok) {
    let detail = "请求失败，请稍后重试。";
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
      else if (typeof body?.detail?.message === "string") detail = body.detail.message;
    } catch {
      /* 错误响应体不是 JSON，继续使用默认提示。 */
    }
    const retry = Number(res.headers.get("Retry-After")) || undefined;
    throw new ApiError(res.status, detail, retry);
  }
  return res;
}

/**
 * 使用 fetch 读取 SSE：原生 EventSource 无法发送 POST 或 Authorization 头。
 * buffer 保留跨网络分片的不完整事件，直到收到空行再解析。
 */
async function readSSE(res: Response, onEvent: (event: AgentEvent) => void): Promise<void> {
  if (!res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const flush = (frame: string) => {
    const data = frame
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (data) onEvent(JSON.parse(data) as AgentEvent);
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    // 网络分片可能切在一个汉字中间；流式解码会保留未收齐的字节，避免中文乱码。
    buffer += decoder.decode(value, { stream: true });
    // 一个网络分片可能包含半个或多个事件；只处理空行分隔的完整帧，剩余内容留到下次。
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) flush(frame);
  }
  if (buffer.trim()) flush(buffer);
}

/** 以当前已登录客户的身份，流式接收一轮聊天的事件。 */
export async function streamChat(
  message: string,
  conversationId: string | null,
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await request("customer", "/api/chat", {
    method: "POST",
    body: JSON.stringify({ message, conversation_id: conversationId }),
    signal,
  });
  // HTTP 请求成功后继续读取事件流，由 Chat.tsx 按事件类型更新界面。
  await readSSE(res, onEvent);
}

export async function fetchConversations(): Promise<ConversationSummary[]> {
  return (await request("admin", "/api/conversations?limit=100")).json();
}

export async function fetchMyConversations(signal?: AbortSignal): Promise<ConversationSummary[]> {
  return (await request("customer", "/api/me/conversations?limit=100", { signal })).json();
}

export async function fetchMyConversation(id: string, signal?: AbortSignal): Promise<ConversationDetail> {
  return (await request("customer", `/api/me/conversations/${encodeURIComponent(id)}`, { signal })).json();
}

export async function fetchRefundConfirmation(cid: string, signal?: AbortSignal): Promise<RefundConfirmation | null> {
  return (await request("customer", `/api/me/conversations/${encodeURIComponent(cid)}/refund-confirmation`, { signal })).json();
}

export async function decideRefund(cid: string, id: string, decision: "confirm" | "cancel", signal?: AbortSignal): Promise<void> {
  await request("customer", `/api/me/conversations/${encodeURIComponent(cid)}/refund-confirmations/${encodeURIComponent(id)}`, {
    method: "POST", body: JSON.stringify({ decision }), signal,
  });
}

export async function fetchConversation(id: string): Promise<ConversationDetail> {
  return (await request("admin", `/api/conversations/${encodeURIComponent(id)}`)).json();
}

/**
 * 以管理员身份订阅会话的实时执行事件。
 * 事件流断开时（如 Pod 被替换）逐步延长等待时间并重连，返回关闭订阅的函数。
 */
export function subscribeToConversation(
  id: string,
  onEvent: (event: AgentEvent) => void,
): () => void {
  const controller = new AbortController();
  let attempt = 0;

  const connect = async () => {
    // 管理端独立订阅同一会话的事件；断线后重连，离开页面时通过 AbortController 停止。
    while (!controller.signal.aborted) {
      try {
        const res = await request("admin", `/api/conversations/${encodeURIComponent(id)}/stream`, {
          signal: controller.signal,
        });
        attempt = 0;
        await readSSE(res, onEvent);
      } catch (err) {
        if (controller.signal.aborted) return;
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) return;
      }
      attempt += 1;
      await new Promise((r) => setTimeout(r, Math.min(15000, 500 * 2 ** attempt)));
    }
  };
  void connect();
  return () => controller.abort();
}

export async function fetchMeta(): Promise<Meta> {
  return (await request(null, "/api/meta")).json();
}

export async function createDemoSession(turnstileToken?: string): Promise<DemoSession> {
  const res = await request(null, "/api/demo/session", {
    method: "POST",
    body: JSON.stringify({ turnstile_token: turnstileToken ?? null }),
  });
  return res.json();
}

export async function fetchOrders(signal?: AbortSignal): Promise<Order[]> {
  return (await request("customer", "/api/me/orders", { signal })).json();
}

export async function fetchMyTickets(signal?: AbortSignal): Promise<TicketSummary[]> {
  return (await request("customer", "/api/me/tickets", { signal })).json();
}

export async function fetchMyTicket(id: string, signal?: AbortSignal): Promise<TicketDetail> {
  return (await request("customer", `/api/me/tickets/${encodeURIComponent(id)}`, { signal })).json();
}

export async function supplementMyTicket(id: string, data: TicketSupplementRequest, signal?: AbortSignal): Promise<TicketAccepted> {
  return (await request("customer", `/api/me/tickets/${encodeURIComponent(id)}/supplement`, {
    method: "POST", body: JSON.stringify(data), signal,
  })).json();
}

export async function cancelMyTicket(id: string, data: { request_id: string }, signal?: AbortSignal): Promise<TicketAccepted> {
  return (await request("customer", `/api/me/tickets/${encodeURIComponent(id)}/cancel`, {
    method: "POST", body: JSON.stringify(data), signal,
  })).json();
}

export async function fetchStats(): Promise<AdminStats> {
  return (await request("admin", "/api/admin/stats")).json();
}

export async function fetchAdminTickets(
  status?: TicketStatus,
  signal?: AbortSignal,
): Promise<TicketSummary[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return (await request("admin", `/api/admin/tickets${query}`, { signal })).json();
}

export async function fetchAdminTicket(id: string, signal?: AbortSignal): Promise<AdminTicketDetail> {
  return (await request("admin", `/api/admin/tickets/${encodeURIComponent(id)}`, { signal })).json();
}

export async function decideAdminTicket(id: string, data: TicketDecisionRequest): Promise<TicketAccepted> {
  return (await request("admin", `/api/admin/tickets/${encodeURIComponent(id)}/decision`, {
    method: "POST",
    body: JSON.stringify(data),
  })).json();
}

export async function resumeAdminTicket(id: string, data: { request_id: string }): Promise<TicketAccepted> {
  return (await request("admin", `/api/admin/tickets/${encodeURIComponent(id)}/resume`, {
    method: "POST",
    body: JSON.stringify(data),
  })).json();
}
