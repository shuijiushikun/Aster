import { useEffect, useState } from "react";
import { devCustomers, devLogin, getToken, onTokenChange, setToken } from "../auth";
import { friendlyError } from "../api";
import { useToast } from "./Toaster";

type Customer = { id: string; name: string; email: string };

/**
 * 本地开发身份切换器，仅在后端以 AUTH_MODE=dev 运行时显示。
 * 生产环境的 /api/dev/* 路由返回 404，因此此组件会自动隐藏。
 */
export default function DevIdentity() {
  const toast = useToast();
  const [customers, setCustomers] = useState<Customer[] | null>(null);
  const [current, setCurrent] = useState<string>("");
  const [, force] = useState(0);

  useEffect(() => {
    devCustomers().then(setCustomers);
    return onTokenChange(() => force((n) => n + 1));
  }, []);

  if (!customers) return null;

  const hasAdmin = Boolean(getToken("admin"));

  return (
    <div className="flex items-center gap-2 rounded-lg border border-warn/30 bg-warn/10 px-2 py-1 text-xs text-warn">
      <span className="font-semibold">开发</span>
      <select
        value={current}
        onChange={async (e) => {
          const id = e.target.value;
          try {
            if (id) await devLogin("customer", id);
            else setToken("customer", null);
            setCurrent(id);
          } catch (err) {
            toast(friendlyError(err), "error");
          }
        }}
        className="max-w-[8rem] rounded bg-surface px-1 py-0.5 text-fg sm:max-w-[10rem]"
        aria-label="选择演示客户"
      >
        <option value="">选择演示客户…</option>
        {customers.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name} ({c.id})
          </option>
        ))}
      </select>
      <button
        onClick={async () => {
          try {
            if (hasAdmin) setToken("admin", null);
            else await devLogin("admin");
          } catch (err) {
            toast(friendlyError(err), "error");
          }
        }}
        className="rounded bg-surface px-2 py-0.5 text-fg hover:bg-surface-2"
      >
        {hasAdmin ? "管理：开" : "管理：关"}
      </button>
    </div>
  );
}
