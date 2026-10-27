import { useState } from "react";
import { formatDate, formatTime } from "../lib/locale";
import { orderStatusLabel, resumeLabel, TICKET_GUIDANCE, TICKET_STATUS } from "../lib/tickets";
import type { AdminTicketDetail, TicketDecisionRequest } from "../types";
import { Badge, Button } from "./ui";
import TicketHistory from "./TicketHistory";

export { TICKET_STATUS } from "../lib/tickets";

interface Props {
  detail: AdminTicketDetail;
  disabled: boolean;
  retry: { label: string; requestId: string; description: string } | null;
  onRetry: () => void;
  onCommentChange: () => void;
  onDecision: (decision: TicketDecisionRequest["decision"], comment: string) => void;
  onResume: () => void;
}

const timestamp = (value: string) => `${formatDate(value)} ${formatTime(value)}`;

export default function TicketDetailPanel({
  detail: ticket, disabled, retry,
  onRetry, onCommentChange, onDecision, onResume,
}: Props) {
  const [comment, setComment] = useState("");
  const status = TICKET_STATUS[ticket.status];
  const manual = ticket.status === "FAILED_TERMINAL";
  const resume = resumeLabel(ticket.status);
  const actionDisabled = disabled || Boolean(retry);
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold">换货工单</h2>
          <p className="mt-1 break-all font-mono text-sm text-subtle">{ticket.id}</p>
        </div>
        <Badge tone={status.tone}>{status.label}</Badge>
      </div>
      <p className="rounded-xl border border-brand/20 bg-brand/5 p-4 text-sm leading-relaxed text-muted">{TICKET_GUIDANCE[ticket.status]}</p>

      <dl className="grid grid-cols-1 gap-2 text-sm text-muted sm:grid-cols-2">
        <div><dt className="text-subtle">客户</dt><dd className="break-all font-mono">{ticket.customer_id}</dd></div>
        <div><dt className="text-subtle">首次申请</dt><dd>{timestamp(ticket.created_at)}</dd></div>
        <div><dt className="text-subtle">订单</dt><dd className="font-mono">{ticket.order_id}</dd></div>
        <div><dt className="text-subtle">商品</dt><dd>{ticket.order.product_name}</dd></div>
        <div><dt className="text-subtle">订单情况</dt><dd>{orderStatusLabel(ticket.order.status)} · {ticket.order.refunded ? "已退款" : "未退款"} · {ticket.order.is_final_sale ? "清仓商品" : "普通商品"}</dd></div>
        <div><dt className="text-subtle">签收日期</dt><dd>{ticket.order.delivered_date ? formatDate(ticket.order.delivered_date) : "无签收记录"}</dd></div>
      </dl>
      <section className="space-y-2 text-sm">
        <h3 className="text-sm font-semibold">申请内容</h3>
        <p className="whitespace-pre-wrap break-words">{ticket.description}</p>
        <p className="text-sm text-muted">原因：{ticket.reason_type === "size" ? "尺码不合适" : ticket.reason_type === "defect" ? "商品故障" : ticket.reason_type || "待补充"}</p>
        <p className="whitespace-pre-wrap break-words text-sm text-muted">目标要求：{ticket.requested_change || "待补充"}</p>
        {ticket.reference_code && <p className="break-all font-mono text-sm text-ok">模拟受理单号：{ticket.reference_code}</p>}
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-semibold">建单时的知识证据</h3>
        {ticket.evidence_refs.length === 0 && <p className="text-sm text-subtle">没有保存知识证据。</p>}
        {ticket.evidence_refs.map((ref, index) => (
          <blockquote key={index} className="rounded-lg border border-line bg-surface/50 p-3 text-sm">
            <p className="break-all font-mono text-subtle">{ref.document_id} · 版本 {ref.revision} · 片段 {ref.chunk_index}</p>
            <p className="mt-1 whitespace-pre-wrap break-words text-muted">{ref.excerpt || "无摘要"}</p>
          </blockquote>
        ))}
      </section>

      {ticket.approval && <section className="rounded-lg border border-line p-3 text-sm">
        <h3 className="font-semibold">最终审批：{ticket.approval.decision === "approved" ? "批准" : "拒绝"}</h3>
        <p className="mt-1 break-all text-subtle">{ticket.approval.admin_subject} · {timestamp(ticket.approval.created_at)}</p>
        <p className="mt-2 whitespace-pre-wrap break-words text-muted">{ticket.approval.comment || "未填写审批意见"}</p>
      </section>}

      {ticket.last_error_code && <div role="status" className="rounded-lg border border-bad/30 bg-bad/5 p-3 text-sm text-bad">
        <p className="break-words font-mono">{ticket.last_error_code}</p>
        {ticket.last_error_message && <p className="mt-1 break-words">{ticket.last_error_message}</p>}
      </div>}
      <section className="space-y-3 border-t border-line pt-4" aria-label="工单操作" aria-busy={disabled}>
        {retry && <div className="space-y-2 text-sm text-muted">
          <p>上次请求的编号与内容已保留。</p>
          <p className="break-all font-mono text-xs">请求编号：{retry.requestId}</p>
          <p className="whitespace-pre-wrap break-words">{retry.description}</p>
          <Button size="sm" onClick={onRetry} disabled={disabled || manual}>{retry.label}</Button>
        </div>}
        {ticket.status === "PENDING_REVIEW" && <div className="space-y-2">
          <label htmlFor={`ticket-comment-${ticket.id}`} className="block text-sm font-medium">审批意见</label>
          <textarea id={`ticket-comment-${ticket.id}`} value={comment} maxLength={1000} rows={3} disabled={disabled}
            onChange={(e) => { setComment(e.target.value); onCommentChange(); }}
            placeholder="可填写审批理由（最多 1000 字）"
            className="w-full resize-y rounded-lg border border-line bg-surface p-2 text-sm placeholder:text-subtle" />
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" size="sm" disabled={actionDisabled} onClick={() => onDecision("approved", comment)}>批准换货申请</Button>
            <Button size="sm" disabled={actionDisabled} onClick={() => onDecision("rejected", comment)}>拒绝申请</Button>
          </div>
        </div>}
        {resume && <Button disabled={actionDisabled} onClick={onResume}>{resume}</Button>}
      </section>

      <div className="border-t border-line pt-5"><TicketHistory events={ticket.events} technical /></div>
    </div>
  );
}
