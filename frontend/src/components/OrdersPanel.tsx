import { Headphones, MessageSquarePlus, Package, Shirt } from "lucide-react";
import { money } from "../lib/outcome";
import { formatDate } from "../lib/locale";
import type { Order } from "../types";
import { Badge, Button, Skeleton } from "./ui";

export function ProductIcon({ name, size = 28 }: { name: string; size?: number }) {
  return /耳机|headphone/i.test(name) ? <Headphones size={size} strokeWidth={1.4} /> : /衣|衫|服装|shirt/i.test(name) ? <Shirt size={size} strokeWidth={1.4} /> : <Package size={size} strokeWidth={1.4} />;
}

export function OrderSummary({ order, orders, onSelect, onAsk, disabled }: {
  order: Order | null; orders: Order[]; onSelect: (id: string) => void; onAsk: (prompt: string) => void; disabled: boolean;
}) {
  if (!order) return <div className="order-summary flex items-center gap-4 p-5"><span className="product-tile"><Package size={35} strokeWidth={1.3} /></span><div><p className="text-sm font-semibold">您的订单，随时可查</p><p className="mt-1 text-sm text-subtle">登录后选择订单，开始售后咨询。</p></div></div>;
  return <section className="order-summary" aria-label="当前订单摘要">
    <div className="product-tile"><ProductIcon name={order.product_name} size={58} /></div>
    <div className="min-w-0 flex-1">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="eyebrow">当前咨询订单</p><h2 className="mt-1 text-lg font-semibold tracking-tight">{order.product_name}</h2></div>
        <label className="sr-only" htmlFor="order-select">切换咨询订单</label><select id="order-select" value={order.id} onChange={(e) => onSelect(e.target.value)} disabled={disabled} className="max-w-full rounded-full border border-line bg-surface-2 px-3 py-2 font-mono text-sm">
          {orders.map((o) => <option key={o.id} value={o.id}>{o.id}</option>)}
        </select></div>
      <div className="mt-3 flex flex-wrap items-center gap-2"><Badge tone={order.refunded ? "neutral" : "brand"}>{order.refunded ? "已退款" : "可咨询售后"}</Badge>{order.is_final_sale && <Badge tone="warn">最终销售商品</Badge>}<span className="text-xs text-subtle">资格由实际订单与规则核验</span></div>
      <dl className="mt-4 grid grid-cols-2 gap-x-5 gap-y-2 text-sm sm:grid-cols-3"><div><dt className="text-subtle">订单金额</dt><dd className="mt-1 font-medium">{money(order.amount)}</dd></div><div><dt className="text-subtle">订单状态</dt><dd className="mt-1">{order.refunded ? "已退款" : order.status === "delivered" ? "已送达" : order.status === "pending" ? "待处理" : order.status === "shipped" ? "已发货" : order.status}</dd></div><div><dt className="text-subtle">签收日期</dt><dd className="mt-1">{order.delivered_date ? formatDate(order.delivered_date) : "暂无记录"}</dd></div></dl>
      <div className="mt-4 flex gap-2"><Button size="sm" disabled={disabled} onClick={() => onAsk(`请查询订单 ${order.id} 的详细情况和售后进度。`)}>咨询此订单</Button><Button size="sm" variant="primary" disabled={disabled} onClick={() => onAsk(`我想申请订单 ${order.id} 的换货，请协助核验并告诉我需要补充什么。`)}>申请换货</Button></div>
    </div>
  </section>;
}

export default function OrdersPanel({ orders, loading, onAsk, onSelect, disabled }: {
  orders: Order[]; loading: boolean; onAsk: (prompt: string) => void; onSelect?: (id: string) => void; disabled?: boolean;
}) {
  return <div className="space-y-3 p-4"><p className="text-sm text-subtle">订单状态来自当前账户。售后资格由后端核验。</p>
    {loading && !orders.length && <Skeleton className="h-24" />}
    {!loading && !orders.length && <p className="py-6 text-center text-sm text-subtle">暂无订单</p>}
    {orders.map((o) => <div key={o.id} className="rounded-2xl border border-line bg-surface p-4">
      <button disabled={disabled} onClick={() => onSelect?.(o.id)} className="flex w-full items-center gap-3 text-left"><span className="rounded-xl bg-surface-2 p-3 text-brand"><ProductIcon name={o.product_name} /></span><div className="min-w-0"><p className="text-sm font-semibold">{o.product_name}</p><p className="mt-1 font-mono text-xs text-subtle">{o.id}</p><p className="mt-1 text-sm">{money(o.amount)} · {o.refunded ? "已退款" : "可咨询售后"}</p></div></button>
      <button disabled={disabled} onClick={() => onAsk(`请帮我查询订单 ${o.id} 的情况。`)} className="mt-3 flex items-center gap-1.5 text-sm font-medium text-brand"><MessageSquarePlus size={13} /> 咨询订单</button>
    </div>)}
  </div>;
}
