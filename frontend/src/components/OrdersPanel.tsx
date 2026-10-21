import { MessageSquarePlus, Package } from "lucide-react";
import { money } from "../lib/outcome";
import { parseDate } from "../lib/locale";
import type { Order } from "../types";
import { Badge, Skeleton } from "./ui";

function daysAgo(iso: string | null): string {
  if (!iso) return "尚未送达";
  const d = Math.floor((Date.now() - parseDate(iso).getTime()) / 86_400_000);
  return d <= 0 ? "今天送达" : `已送达 ${d} 天`;
}

function Tags({ o }: { o: Order }) {
  const tags = [];
  if (o.refunded) tags.push(<Badge key="r" tone="ok">已退款</Badge>);
  if (o.is_final_sale) tags.push(<Badge key="f" tone="bad">清仓不退</Badge>);
  if (o.amount > 500) tags.push(<Badge key="h" tone="warn">超过 500 美元</Badge>);
  if (o.scenario === "out_of_window") tags.push(<Badge key="w">超过 30 天</Badge>);
  if (!tags.length) tags.push(<Badge key="e" tone="info">可申请退款</Badge>);
  return <div className="mt-2 flex flex-wrap gap-1">{tags}</div>;
}

export default function OrdersPanel({
  orders,
  loading,
  onAsk,
  disabled,
}: {
  orders: Order[];
  loading: boolean;
  onAsk: (prompt: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className="p-4">
      <p className="mb-3 text-xs text-subtle">
        订单状态与数据库同步，退款成功后会在这里更新。
      </p>
      {loading && orders.length === 0 && (
        <div className="space-y-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      )}
      <ul className="space-y-2">
        {!loading && orders.length === 0 && <li className="py-6 text-center text-sm text-subtle">暂无订单</li>}
        {orders.map((o) => (
          <li
            key={o.id}
            className="group rounded-xl border border-line bg-surface p-3 transition-colors hover:border-brand/40"
          >
            <div className="flex items-start gap-3">
              <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-muted">
                <Package size={16} />
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-sm font-medium leading-snug">{o.product_name}</span>
                  <span className="text-sm font-semibold tabular-nums">{money(o.amount)}</span>
                </div>
                <div className="mt-0.5 font-mono text-[11px] text-subtle">{o.id}</div>
                <div className="text-[11px] text-subtle">{daysAgo(o.delivered_date)}</div>
                <Tags o={o} />
              </div>
            </div>
            <button
              disabled={disabled}
              onClick={() => onAsk(`我想申请订单 ${o.id} 的退款。`)}
              className="mt-2 flex w-full items-center justify-center gap-1.5 rounded-lg border border-dashed border-line py-1.5 text-xs font-medium text-muted transition-colors hover:border-brand/50 hover:text-brand disabled:opacity-40"
            >
              <MessageSquarePlus size={13} /> 申请退款
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
