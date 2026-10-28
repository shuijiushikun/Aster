import { RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";
import type { RefundConfirmation } from "../types";
import { money } from "../lib/outcome";
import { Button, Modal } from "./ui";

export default function RefundConfirmationDialog({ proposal, busy, error, onDecide }: {
  proposal: RefundConfirmation; busy: boolean; error: string | null;
  onDecide: (decision: "confirm" | "cancel") => void;
}) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  const seconds = Math.max(0, Math.ceil((Date.parse(proposal.expires_at) - now) / 1000));
  const manual = proposal.operation === "escalate_to_human";
  const cancel = () => { if (!busy) onDecide("cancel"); };
  return <Modal title="确认退款申请" onClose={cancel}>
    <span className="mb-5 inline-flex h-11 w-11 items-center justify-center rounded-full border border-line bg-surface-2"><RotateCcw size={21} /></span>
    <h2 className="text-xl font-semibold tracking-tight">最后核对一下退款信息</h2>
    <p className="mt-2 text-sm leading-6 text-muted">{manual ? "确认后将提交人工审核，审核通过前不会记录退款。" : "确认后将按下方金额记录模拟退款。"} 请确认是您希望处理的这笔订单。</p>
    <dl className="my-6 divide-y divide-line rounded-lg border border-line px-4">
      {[["订单号", proposal.order_id], ["商品", proposal.product_name], ["商品型号", proposal.model_code ?? "该订单未登记型号"],
        ["处理方式", manual ? "提交人工退款审核" : "模拟退款"]].map(([label, value]) =>
        <div key={label} className="flex items-start justify-between gap-5 py-3 text-sm"><dt className="shrink-0 text-muted">{label}</dt><dd className="break-words text-right font-medium">{value}</dd></div>)}
      <div className="flex items-center justify-between gap-4 py-4"><dt className="text-sm text-muted">申请退款金额</dt><dd className="text-xl font-semibold tabular-nums">{money(Number(proposal.amount))}</dd></div>
    </dl>
    <p className="text-xs leading-5 text-subtle">本项目使用模拟退款账本，没有真实资金划转。取消将保留原订单状态。</p>
    <p className="mt-2 text-xs text-muted">{seconds ? `确认有效期剩余 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : "确认已过期，请取消后重新提出退款申请。"}</p>
    {error && <p role="alert" className="mt-3 text-sm leading-6 text-bad">{error}</p>}
    <div className="mt-6 flex justify-end gap-3">
      <Button disabled={busy} onClick={cancel}>取消申请</Button>
      <Button variant="primary" disabled={busy || !seconds} onClick={() => onDecide("confirm")}>
        {busy ? "正在处理…" : manual ? "确认提交审核" : "确认退款"}
      </Button>
    </div>
  </Modal>;
}
