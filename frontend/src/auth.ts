/**
 * 客服组件的身份来源。
 *
 * 生产环境由已掌握登录身份的宿主网站签发短期 JWT，并通过以下任一方式交给组件：
 *   - 在前端脚本加载前设置 `window.ACME_SUPPORT_TOKEN`；
 *   - 由来源位于 VITE_TRUSTED_PARENT_ORIGINS 白名单中的父窗口发送
 *     `postMessage({ type: "acme-support-token", token })`。
 * 后端负责验证令牌，聊天过程不再询问用户身份。
 *
 * 本地开发模式（AUTH_MODE=dev）下，前端可通过 /api/dev/token 获取令牌。
 */

import { apiUrl } from "./config";

export type Role = "customer" | "admin";

declare global {
  interface Window {
    ACME_SUPPORT_TOKEN?: string;
    ACME_ADMIN_TOKEN?: string;
  }
}

const KEYS: Record<Role, string> = {
  customer: "acme.customerToken",
  admin: "acme.adminToken",
};

const listeners = new Set<() => void>();

function read(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

export function getToken(role: Role): string | null {
  const injected = role === "customer" ? window.ACME_SUPPORT_TOKEN : window.ACME_ADMIN_TOKEN;
  return injected || read(KEYS[role]);
}

export function setToken(role: Role, token: string | null): void {
  try {
    if (token) sessionStorage.setItem(KEYS[role], token);
    else sessionStorage.removeItem(KEYS[role]);
  } catch {
    /* 存储不可用（如隐私模式）时，继续使用内存中的令牌。 */
  }
  if (role === "customer") window.ACME_SUPPORT_TOKEN = token ?? undefined;
  else window.ACME_ADMIN_TOKEN = token ?? undefined;
  listeners.forEach((fn) => fn());
}

export function onTokenChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

const trustedOrigins = (import.meta.env.VITE_TRUSTED_PARENT_ORIGINS ?? "")
  .split(",")
  .map((o: string) => o.trim())
  .filter(Boolean);

window.addEventListener("message", (e: MessageEvent) => {
  if (!trustedOrigins.includes(e.origin)) return;
  const data = e.data as { type?: string; token?: string; role?: Role };
  if (data?.type === "acme-support-token" && typeof data.token === "string") {
    setToken(data.role === "admin" ? "admin" : "customer", data.token);
  }
});

/** 仅开发环境使用：通过后端 /api/dev/token 接口签发令牌。 */
export async function devLogin(role: Role, customerId?: string): Promise<void> {
  const res = await fetch(apiUrl("/api/dev/token"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(role === "admin" ? { admin: true } : { customer_id: customerId }),
  });
  if (!res.ok) throw new Error("开发身份切换失败，请重试。");
  setToken(role, (await res.json()).token);
}

export async function devCustomers(): Promise<{ id: string; name: string; email: string }[] | null> {
  try {
    const res = await fetch(apiUrl("/api/dev/customers"));
    return res.ok ? res.json() : null;
  } catch {
    return null;
  }
}
