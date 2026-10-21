import { createDemoSession } from "../api";
import { setToken } from "../auth";
import type { DemoSession } from "../types";

/**
 * 公开演示沙箱（后端 AUTH_MODE=demo）。会话包含用于聊天的客户令牌，
 * 以及用于智能体控制台的受限管理员令牌。两者保存在 sessionStorage 中，
 * 使当前标签页刷新后可继续使用原沙箱，各标签页的存储相互独立。
 */
const KEY = "acme.demoSession";

type Stored = DemoSession & { started_at: number };

export function getDemo(): Stored | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Stored;
    // 令牌由服务端判定过期，前端提前一分钟清除沙箱会话。
    if (Date.now() > s.started_at + (s.expires_in - 60) * 1000) {
      clearDemo();
      return null;
    }
    return s;
  } catch {
    return null;
  }
}

export async function startDemo(turnstileToken?: string): Promise<Stored> {
  const session = await createDemoSession(turnstileToken);
  const stored: Stored = { ...session, started_at: Date.now() };
  try {
    sessionStorage.setItem(KEY, JSON.stringify(stored));
  } catch {
    /* 存储不可用时，令牌仍保留在当前页面的内存中。 */
  }
  setToken("admin", session.admin_token);
  setToken("customer", session.token);
  return stored;
}

export function clearDemo(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* 忽略存储清理失败，继续清除内存中的令牌。 */
  }
  setToken("admin", null);
  setToken("customer", null);
}
