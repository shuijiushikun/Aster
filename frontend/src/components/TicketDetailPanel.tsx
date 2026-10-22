import { useState } from "react";
import { formatDate, formatTime } from "../lib/locale";
import type { Tone } from "../lib/outcome";
import type { AdminTicketDetail, TicketDecisionRequest, TicketStatus } from "../types";
import { Badge, Button } from "./ui";

export const TICKET_STATUS: Record<TicketStatus, { label: string; tone: Tone }> = {
  OPEN: { label: "待校验", tone: "info" },
  WAITING_INFO: { label: "待客户补充", tone: "warn" },
  PENDING_REVIEW: { label: "待审批", tone: "warn" },
  APPROVED: { label: "已批准待受理", tone: "info" },
  FAILED_RETRYABLE: { label: "失败可重试", tone: "bad" },
  COMPLETED: { label: "已生成模拟受理单", tone: "ok" },
  REJECTED: { label: "已拒绝", tone: "neutral" },
  FAILED_TERMINAL: { label: "需人工处置", tone: "bad" },
  CANCELLED: { label: "已取消", tone: "neutral" },
};

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
  const waiting = ticket.status === "WAITING_INFO" || ticket.status === "PENDING_REVIEW";
  const finished = ["COMPLETED", "REJECTED", "CANCELLED"].includes(ticket.status);
  const actionDisabled = disabled || Boolean(retry);
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold">换货工单</h2>
          <p className="mt-1 break-all font-mono text-xs text-subtle">{ticket.id}</p>
        </div>
        <Badge tone={status.tone}>{status.label}</Badge>
      </div>

      <dl className="grid grid-cols-1 gap-2 text-xs text-muted sm:grid-cols-2">
        <div><dt className="text-subtle">客户</dt><dd className="break-all font-mono">{ticket.customer_id}</dd></div>
        <div><dt className="text-subtle">首次申请</dt><dd>{timestamp(ticket.created_at)}</dd></div>
        <div><dt className="text-subtle">订单</dt><dd className="font-mono">{ticket.order_id}</dd></div>
        <div><dt className="text-subtle">商品</dt><dd>{ticket.order.product_name}</dd></div>
        <div><dt className="text-subtle">订单事实</dt><dd>{ticket.order.status} · {ticket.order.refunded ? "已退款" : "未退款"} · {ticket.order.is_final_sale ? "最终销售" : "非最终销售"}</dd></div>
        <div><dt className="text-subtle">签收日期</dt><dd>{ticket.order.delivered_date ? formatDate(ticket.order.delivered_date) : "无签收记录"}</dd></div>
      </dl>
      <section className="space-y-2 text-sm">
        <h3 className="text-xs font-semibold">申请内容</h3>
        <p className="whitespace-pre-wrap break-words">{ticket.description}</p>
        <p className="text-xs text-muted">原因：{ticket.reason_type === "size" ? "尺码不合适" : ticket.reason_type === "defect" ? "商品故障" : ticket.reason_type || "待补充"}</p>
        <p className="whitespace-pre-wrap break-words text-xs text-muted">目标要求：{ticket.requested_change || "待补充"}</p>
        {ticket.reference_code && <p className="break-all font-mono text-xs text-ok">模拟受理单号：{ticket.reference_code}</p>}
      </section>

      <section className="space-y-2">
        <h3 className="text-xs font-semibold">建单时的知识证据</h3>
        {ticket.evidence_refs.length === 0 && <p className="text-xs text-subtle">没有保存知识证据。</p>}
        {ticket.evidence_refs.map((ref, index) => (
          <blockquote key={index} className="rounded-lg border border-line bg-surface/50 p-3 text-xs">
            <p className="break-all font-mono text-subtle">{ref.document_id} · 版本 {ref.revision} · 片段 {ref.chunk_index}</p>
            <p className="mt-1 whitespace-pre-wrap break-words text-muted">{ref.excerpt || "无摘要"}</p>
          </blockquote>
        ))}
      </section>

      {ticket.approval && <section className="rounded-lg border border-line p-3 text-xs">
        <h3 className="font-semibold">最终审批：{ticket.approval.decision === "approved" ? "批准" : "拒绝"}</h3>
        <p className="mt-1 break-all text-subtle">{ticket.approval.admin_subject} · {timestamp(ticket.approval.created_at)}</p>
        <p className="mt-2 whitespace-pre-wrap break-words text-muted">{ticket.approval.comment || "未填写审批意见"}</p>
      </section>}

      {ticket.last_error_code && <div role="status" className="rounded-lg border border-bad/30 bg-bad/5 p-3 text-xs text-bad">
        <p className="break-words font-mono">{ticket.last_error_code}</p>
        {ticket.last_error_message && <p className="mt-1 break-words">{ticket.last_error_message}</p>}
      </div>}
      <section className="space-y-3 border-t border-line pt-4" aria-label="工单操作" aria-busy={disabled}>
        {retry && <div className="space-y-2 text-xs text-muted">
          <p>上次请求的编号与内容已保留。</p>
          <p className="break-all font-mono text-[10px]">请求编号：{retry.requestId}</p>
          <p className="whitespace-pre-wrap break-words">{retry.description}</p>
          <Button size="sm" onClick={onRetry} disabled={disabled || manual}>{retry.label}</Button>
        </div>}
        {ticket.status === "PENDING_REVIEW" && <div className="space-y-2">
          <label htmlFor={`ticket-comment-${ticket.id}`} className="block text-xs font-medium">审批意见</label>
          <textarea id={`ticket-comment-${ticket.id}`} value={comment} maxLength={1000} rows={3} disabled={disabled}
            onChange={(e) => { setComment(e.target.value); onCommentChange(); }}
            placeholder="可填写审批理由（最多 1000 字）"
            className="w-full resize-y rounded-lg border border-line bg-surface p-2 text-sm placeholder:text-subtle" />
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" size="sm" disabled={actionDisabled} onClick={() => onDecision("approved", comment)}>批准换货申请</Button>
            <Button size="sm" disabled={actionDisabled} onClick={() => onDecision("rejected", comment)}>拒绝申请</Button>
          </div>
        </div>}
        <div className="space-y-2">
          <Button size="sm" disabled={actionDisabled || manual} onClick={onResume}>
            {finished ? "协调结束状态" : "重试继续处理"}
          </Button>
          <p className="text-xs text-subtle">{manual ? "该工单无法自动恢复，需要人工处置。" : waiting
            ? "继续处理会核对已提交事实；待补充或待审批时仍会等待，不能代替补充或审批。"
            : finished ? "工单已结束，此入口仅协调工作流或返回既有结果。"
            : "每次请求尝试继续处理；中断或失败后可再次重试。"}</p>
        </div>
      </section>

      <details className="border-t border-line pt-3">
        <summary className="cursor-pointer text-xs font-semibold">业务事件（{ticket.events.length}）</summary>
        <ol className="mt-3 space-y-3">
          {ticket.events.map((event) => <li key={event.id} className="border-l-2 border-line pl-3 text-xs">
            <p className="break-words font-mono">{event.kind} · {event.actor_type}</p>
            <p className="mt-1 text-subtle">{timestamp(event.created_at)}</p>
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words text-[10px] text-muted">{JSON.stringify(event.payload_json, null, 2)}</pre>
          </li>)}
        </ol>
      </details>
    </div>
  );
}
