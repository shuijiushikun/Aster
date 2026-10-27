import clsx from "clsx";
import { ArrowLeft, ClipboardList, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ApiError, cancelMyTicket, fetchMyTicket, fetchMyTickets, friendlyError, supplementMyTicket } from "../api";
import { formatDate, formatTime } from "../lib/locale";
import type { TicketAccepted, TicketDetail, TicketSummary, TicketSupplementRequest } from "../types";
import { TICKET_STATUS } from "./TicketDetailPanel";
import { Badge, Button, Skeleton } from "./ui";

type Action = { kind: "supplement"; body: TicketSupplementRequest } | { kind: "cancel"; body: { request_id: string } };
const EVENT_LABELS: Record<string, string> = { CREATED: "提交换货申请", SUPPLEMENTED: "补充申请材料", CANCELLED: "取消申请", DECIDED: "完成审批", RESUME_REQUESTED: "请求继续处理", VALIDATED: "核验业务规则", EXECUTION_FAILED: "受理执行失败", POLICY_REJECTED: "规则校验未通过", COMPLETED: "生成模拟受理单", REFUND_CANCELLED: "退款后终止申请" };

function SupplementForm({ ticket, disabled, pending, onEdit, onSubmit }: {
  ticket: TicketDetail; disabled: boolean; pending: boolean; onEdit: () => void;
  onSubmit: (data: Omit<TicketSupplementRequest, "request_id">) => void;
}) {
  const [description, setDescription] = useState(ticket.description);
  const [reason, setReason] = useState(ticket.reason_type === "size" || ticket.reason_type === "defect" ? ticket.reason_type : "");
  const [change, setChange] = useState(ticket.requested_change ?? "");
  return <form className="space-y-3 rounded-2xl bg-surface-2 p-4" onSubmit={(e) => {
    e.preventDefault();
    if (!description.trim() || pending) return;
    onSubmit({ description: description.trim(), ...(reason ? { reason_type: reason as "size" | "defect" } : {}), ...(change.trim() ? { requested_change: change.trim() } : {}) });
  }}>
    <h3 className="text-sm font-semibold">补充申请材料</h3>
    <label className="block text-sm font-medium">问题描述
      <textarea required value={description} maxLength={2000} rows={3} disabled={disabled}
        onChange={(e) => { setDescription(e.target.value); onEdit(); }} className="form-field mt-1.5" />
    </label>
    <label className="block text-sm font-medium">换货原因
      <select value={reason} disabled={disabled} onChange={(e) => { setReason(e.target.value); onEdit(); }} className="form-field mt-1.5">
        <option value="">尚未确定</option><option value="defect">商品故障</option><option value="size">尺码不合适</option>
      </select>
    </label>
    <label className="block text-sm font-medium">希望更换为
      <input value={change} maxLength={500} disabled={disabled} placeholder="例如：同型号，或所需尺码" onChange={(e) => { setChange(e.target.value); onEdit(); }} className="form-field mt-1.5" />
    </label>
    <Button type="submit" variant="primary" size="sm" disabled={disabled || pending || !description.trim()}>提交补充材料</Button>
  </form>;
}

/** 所有状态来自 GET；202 仅作为接收回执。组件随客户凭证变化重新挂载。 */
export default function CustomerTickets({ refreshKey, suggestedId, onChanged }: {
  refreshKey: number; suggestedId: string | null; onChanged: () => void;
}) {
  const [tickets, setTickets] = useState<TicketSummary[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TicketDetail | null>(null);
  const [version, setVersion] = useState(0);
  const [listError, setListError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Record<string, Action>>({});
  const [receipt, setReceipt] = useState<TicketAccepted | null>(null);
  const actionController = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; actionController.current?.abort(); }; }, []);
  useEffect(() => { if (suggestedId) setSelectedId(suggestedId); }, [suggestedId]);
  useEffect(() => {
    const controller = new AbortController();
    fetchMyTickets(controller.signal).then((rows) => {
      if (!controller.signal.aborted) { setTickets(rows); setListError(null); }
    }).catch((err) => { if (!controller.signal.aborted) setListError(friendlyError(err)); });
    return () => controller.abort();
  }, [refreshKey, version]);
  useEffect(() => {
    const controller = new AbortController();
    setDetailError(null);
    if (selectedId) fetchMyTicket(selectedId, controller.signal).then((row) => {
      if (!controller.signal.aborted) setDetail(row);
    }).catch((err) => { if (!controller.signal.aborted) { setDetail(null); setDetailError(friendlyError(err)); } });
    return () => controller.abort();
  }, [selectedId, refreshKey, version]);
  useEffect(() => {
    if (!tickets?.some((t) => ["OPEN", "APPROVED", "PENDING_REVIEW", "WAITING_INFO", "FAILED_RETRYABLE"].includes(t.status))) return;
    const timer = setInterval(() => setVersion((v) => v + 1), 8000);
    return () => clearInterval(timer);
  }, [tickets]);
  function clearPending(id: string) { setPending((rows) => { const next = { ...rows }; delete next[id]; return next; }); }
  async function execute(id: string, action: Action) {
    if (busy) return;
    const controller = new AbortController();
    actionController.current = controller;
    setBusy(true); setNotice(null); setReceipt(null);
    try {
      const result = action.kind === "cancel" ? await cancelMyTicket(id, action.body, controller.signal) : await supplementMyTicket(id, action.body, controller.signal);
      if (!mounted.current) return;
      clearPending(id); setReceipt(result);
    } catch (err) {
      if (!mounted.current || controller.signal.aborted) return;
      if (err instanceof ApiError && err.status === 409) {
        clearPending(id); setNotice("操作与工单当前状态冲突，正在刷新最新事实，请核对后继续。");
      } else if (!(err instanceof ApiError) || err.status === 0 || err.status >= 500) {
        setPending((rows) => ({ ...rows, [id]: action }));
        setNotice("暂未确认请求结果。原请求编号与内容已保留，请刷新核对，或重试同一请求。");
      } else { clearPending(id); setNotice(friendlyError(err)); }
    } finally {
      if (mounted.current) { setBusy(false); setVersion((v) => v + 1); onChanged(); }
    }
  }
  const current = detail?.id === selectedId ? detail : null;
  const retry = selectedId ? pending[selectedId] : undefined;
  return <section aria-label="我的售后工单" className="space-y-4">
    <div className="flex items-center justify-between"><h2 className="flex items-center gap-2 text-sm font-semibold"><ClipboardList size={16} className="text-brand" /> 我的工单</h2>
      <Button size="sm" variant="ghost" disabled={busy} onClick={() => setVersion((v) => v + 1)}><RefreshCw size={13} /> 刷新</Button></div>
    {listError && <p role="alert" className="text-sm text-bad">{listError}</p>}
    {tickets === null && !listError && <Skeleton className="h-20" />}
    {!selectedId ? <div className="space-y-2">
      {tickets?.length === 0 && <div className="rounded-2xl border border-dashed border-line p-5 text-center"><ClipboardList size={25} className="mx-auto mb-2 text-brand/50" /><p className="text-sm text-muted">还没有售后工单</p><p className="mt-1 text-xs leading-relaxed text-subtle">在对话中说明订单与换货需求，助手会协助核验。</p></div>}
      {tickets?.map((t) => <button key={t.id} onClick={() => { setSelectedId(t.id); setNotice(null); setReceipt(null); }} className="w-full rounded-2xl border border-line bg-surface p-3.5 text-left transition hover:border-brand/40">
        <div className="flex items-center justify-between gap-2"><span className="font-mono text-xs">{t.order_id}</span><Badge tone={TICKET_STATUS[t.status].tone}>{TICKET_STATUS[t.status].label}</Badge></div>
        <p className="mt-2 line-clamp-2 text-sm leading-relaxed text-muted">{t.description}</p><p className="mt-2 text-xs text-subtle">{formatDate(t.updated_at)} {formatTime(t.updated_at)}</p>
      </button>)}
    </div> : <div className="space-y-4">
      <button disabled={busy} onClick={() => { setSelectedId(null); setNotice(null); setReceipt(null); }} className="flex items-center gap-1 text-sm text-muted hover:text-brand"><ArrowLeft size={13} /> 返回全部工单</button>
      {receipt?.ticket_id === selectedId && <p role="status" className="rounded-xl bg-brand/10 p-3 text-sm leading-relaxed text-brand">请求已接收（{TICKET_STATUS[receipt.status].label}）。接收回执不代表处理完成，下方详情以重新查询的结果为准。</p>}
      {notice && <p role="alert" className="rounded-xl bg-warn/10 p-3 text-sm leading-relaxed text-warn">{notice}</p>}
      {detailError ? <p role="alert" className="text-sm text-bad">{detailError} 请刷新重试。</p> : !current ? <Skeleton className="h-36" /> : <>
        <div className="rounded-2xl border border-line bg-surface p-4">
          <Badge tone={TICKET_STATUS[current.status].tone}>{TICKET_STATUS[current.status].label}</Badge>
          <p className="mt-3 text-sm font-semibold">订单 {current.order_id}</p><p className="mt-2 whitespace-pre-wrap break-words text-sm leading-relaxed text-muted">{current.description}</p>
          <p className="mt-2 text-sm text-subtle">原因：{current.reason_type === "size" ? "尺码不合适" : current.reason_type === "defect" ? "商品故障" : "待补充"}</p>
          <p className="mt-1 whitespace-pre-wrap break-words text-sm text-subtle">更换要求：{current.requested_change || "待补充"}</p>
          {current.reference_code && <p className="mt-3 break-all text-sm font-semibold text-ok">模拟受理单：{current.reference_code}</p>}
          <p className="mt-3 break-all font-mono text-xs text-subtle">工单 {current.id}</p>
        </div>
        {retry && <div className="space-y-2 rounded-2xl border border-warn/30 bg-warn/5 p-3 text-sm">
          <p>待确认请求：{retry.kind === "cancel" ? "取消工单" : "补充材料"}</p><p className="break-all font-mono text-xs text-subtle">{retry.body.request_id}</p>
          {retry.kind === "supplement" && <p className="whitespace-pre-wrap break-words text-muted">{retry.body.description} · {retry.body.requested_change || "未指定更换要求"}</p>}
          <Button size="sm" disabled={busy} onClick={() => void execute(current.id, retry)}>重试上次请求</Button>
        </div>}
        {current.status === "WAITING_INFO" && <SupplementForm key={current.id} ticket={current} pending={Boolean(retry)} disabled={busy || retry?.kind === "cancel"} onEdit={() => { if (retry?.kind === "supplement") clearPending(current.id); }}
          onSubmit={(body) => { if (!retry) void execute(current.id, { kind: "supplement", body: { request_id: crypto.randomUUID(), ...body } }); }} />}
        {["WAITING_INFO", "PENDING_REVIEW"].includes(current.status) && <Button size="sm" className="w-full" disabled={busy || Boolean(retry)} onClick={() => void execute(current.id, { kind: "cancel", body: { request_id: crypto.randomUUID() } })}>取消换货申请</Button>}
        <details className="border-t border-line pt-3"><summary className="cursor-pointer text-sm text-muted">申请记录与证据（{current.events.length}）</summary>
          <div className="mt-3 space-y-3">{current.evidence_refs.map((ref, i) => <blockquote key={i} className="rounded-xl bg-surface-2 p-3 text-sm text-muted"><p className="font-mono text-xs">{ref.document_id} · v{ref.revision} · 片段 {ref.chunk_index}</p><p className="mt-1">{ref.excerpt || "无摘录"}</p></blockquote>)}
            {current.events.map((event) => <div key={event.id} className={clsx("border-l-2 border-line pl-3 text-sm")}><p className="break-words">{EVENT_LABELS[event.kind] || "工单状态更新"}</p><p className="mt-1 text-xs text-subtle">{formatDate(event.created_at)} {formatTime(event.created_at)}</p></div>)}
          </div>
        </details>
      </>}
    </div>}
  </section>;
}
