import clsx from "clsx";
import { RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { getToken } from "../auth";
import {
  ApiError, decideAdminTicket, fetchAdminTicket, fetchAdminTickets, friendlyError, resumeAdminTicket,
} from "../api";
import { formatDate, formatTime } from "../lib/locale";
import type { AdminTicketDetail, TicketAccepted, TicketDecisionRequest, TicketStatus, TicketSummary } from "../types";
import TicketDetailPanel, { TICKET_STATUS } from "./TicketDetailPanel";
import { Badge, Button, Skeleton } from "./ui";

type Action =
  | { kind: "decision"; body: TicketDecisionRequest }
  | { kind: "resume"; body: { request_id: string } };

export default function AdminTickets() {
  const mounted = useRef(true);
  const identity = useRef(getToken("admin"));
  const alive = () => mounted.current && getToken("admin") === identity.current;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const [status, setStatus] = useState<TicketStatus | "">("");
  const [tickets, setTickets] = useState<TicketSummary[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<AdminTicketDetail | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [retryRequests, setRetryRequests] = useState<Record<string, Action>>({});
  const [accepted, setAccepted] = useState<Record<string, TicketAccepted>>({});

  useEffect(() => {
    const controller = new AbortController();
    setTickets(null);
    setListError(null);
    fetchAdminTickets(status || undefined, controller.signal)
      .then((rows) => { if (!controller.signal.aborted) setTickets(rows); })
      .catch((err) => { if (!controller.signal.aborted) setListError(friendlyError(err)); });
    return () => controller.abort();
  }, [status]);

  useEffect(() => {
    const controller = new AbortController();
    setDetail(null);
    setDetailError(null);
    setActionError(null);
    if (selectedId) {
      fetchAdminTicket(selectedId, controller.signal)
        .then((row) => { if (!controller.signal.aborted) setDetail(row); })
        .catch((err) => { if (!controller.signal.aborted) setDetailError(friendlyError(err)); });
    }
    return () => controller.abort();
  }, [selectedId]);

  const refresh = async () => {
    if (!alive()) return;
    setLoading(true);
    const [list, current] = await Promise.allSettled([
      fetchAdminTickets(status || undefined),
      selectedId ? fetchAdminTicket(selectedId) : Promise.resolve(null),
    ]);
    if (!alive()) return;
    if (list.status === "fulfilled") {
      setTickets(list.value);
      setListError(null);
    } else setListError(friendlyError(list.reason));
    if (current.status === "fulfilled") {
      setDetail(current.value);
      setDetailError(null);
    } else {
      setDetail(null);
      setDetailError(friendlyError(current.reason));
    }
    setLoading(false);
  };

  const clearRetry = () => {
    if (!selectedId) return;
    setRetryRequests((requests) => {
      const next = { ...requests };
      delete next[selectedId];
      return next;
    });
  };

  const execute = async (action: Action) => {
    if (!selectedId || busy || loading) return;
    setBusy(true);
    setActionError(null);
    setAccepted((rows) => {
      const next = { ...rows };
      delete next[selectedId];
      return next;
    });
    try {
      const result = action.kind === "decision"
        ? await decideAdminTicket(selectedId, action.body)
        : await resumeAdminTicket(selectedId, action.body);
      if (!alive()) return;
      setAccepted((rows) => ({ ...rows, [selectedId]: result }));
      clearRetry();
    } catch (err) {
      if (!alive()) return;
      if (err instanceof ApiError && err.status === 409) {
        clearRetry();
        setActionError("操作与当前工单事实冲突，已重新查询最新状态。请核对后继续。");
      } else if (!(err instanceof ApiError) || err.status === 0 || err.status >= 500) {
        // 网络或服务异常时可能已经提交；重试必须使用原请求 ID 和原请求体。
        setRetryRequests((rows) => ({ ...rows, [selectedId]: action }));
        setActionError("尚未确认请求结果。已查询工单；如需重试，请使用“重试上次请求”保留原请求编号。");
      } else {
        clearRetry();
        setActionError(friendlyError(err));
      }
    } finally {
      if (alive()) { await refresh(); setBusy(false); }
    }
  };

  const disabled = busy || loading || (tickets === null && !listError);
  const current = detail?.id === selectedId ? detail : null;
  const retry = selectedId ? retryRequests[selectedId] : undefined;
  const receipt = selectedId ? accepted[selectedId] : undefined;
  return (
    <section className="flex min-h-0 flex-1 flex-col" aria-label="换货工单审批">
      <div className="flex flex-wrap items-end justify-between gap-3 border-b border-line px-4 py-3">
        <label className="flex items-center gap-2 text-sm text-muted">
          工单状态
          <select value={status} onChange={(e) => setStatus(e.target.value as TicketStatus | "")} disabled={disabled}
            className="rounded-lg border border-line bg-surface px-2 py-1.5 text-fg">
            <option value="">全部状态</option>
            {(Object.keys(TICKET_STATUS) as TicketStatus[]).map((key) => (
              <option key={key} value={key}>{TICKET_STATUS[key].label}</option>
            ))}
          </select>
        </label>
        <Button size="sm" onClick={() => void refresh()} disabled={disabled}>
          <RefreshCw size={13} className={clsx(disabled && "animate-spin")} /> 刷新工单
        </Button>
      </div>
      <div className="grid min-h-0 flex-1 overflow-y-auto lg:grid-cols-[300px_1fr] lg:overflow-hidden">
        <aside className="max-h-64 overflow-y-auto border-b border-line lg:max-h-none lg:border-b-0 lg:border-r" aria-label="工单列表">
          {listError && <p role="alert" className="p-4 text-sm text-bad">{listError}</p>}
          {tickets === null && !listError && <div className="space-y-2 p-3"><Skeleton className="h-16" /><Skeleton className="h-16" /></div>}
          {tickets?.length === 0 && <p className="p-6 text-center text-sm text-subtle">当前状态下暂无工单。</p>}
          {tickets?.map((ticket) => (
            <button key={ticket.id} onClick={() => setSelectedId(ticket.id)} disabled={disabled}
              aria-pressed={selectedId === ticket.id}
              className={clsx("block w-full border-b border-line/70 px-4 py-3 text-left disabled:opacity-60", selectedId === ticket.id ? "bg-brand/5" : "hover:bg-surface-2")}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-mono text-sm">{ticket.order_id}</span>
                <Badge tone={TICKET_STATUS[ticket.status].tone}>{TICKET_STATUS[ticket.status].label}</Badge>
              </div>
              <p className="mt-1 line-clamp-2 text-sm text-muted">{ticket.description}</p>
              <p className="mt-2 truncate font-mono text-xs text-subtle">{ticket.id}</p>
              <p className="mt-1 text-xs text-subtle">更新于 {formatDate(ticket.updated_at)} {formatTime(ticket.updated_at)}</p>
              {ticket.last_error_code && <p className="mt-1 break-words text-xs text-bad">{ticket.last_error_code}</p>}
            </button>
          ))}
        </aside>
        <div className="min-h-0 p-4 lg:overflow-y-auto">
          {receipt && <div role="status" className="mb-4 rounded-lg border border-info/30 bg-info/5 p-3 text-sm text-muted">
            <p>请求已保存，接收时状态：{TICKET_STATUS[receipt.status].label}。最新状态以重新查询的工单详情为准。</p>
            <p className="mt-1 break-all font-mono text-xs">请求编号：{receipt.request_id}</p>
            <p className="mt-1">下方已更新工单详情。若仍显示“已批准，待办理”或“办理异常”，可继续办理原申请。</p>
          </div>}
          {actionError && <p role="alert" className="mb-4 text-sm text-bad">{actionError}</p>}
          {!selectedId ? <p className="py-8 text-center text-sm text-subtle">选择工单，核对订单与证据后审批或继续处理。</p>
            : detailError ? <p role="alert" className="text-sm text-bad">{detailError} 请刷新工单。</p>
            : !current ? <Skeleton className="h-48" />
            : <TicketDetailPanel key={current.id} detail={current} disabled={disabled}
                retry={retry ? {
                  label: retry.kind === "decision" ? "重试上次审批请求" : "重试上次继续处理请求",
                  requestId: retry.body.request_id,
                  description: retry.kind === "decision"
                    ? `${retry.body.decision === "approved" ? "批准" : "拒绝"}：${retry.body.comment || "未填写审批意见"}`
                    : "继续处理工单",
                } : null}
                onRetry={() => { if (retry) void execute(retry); }}
                onCommentChange={() => { if (retry?.kind === "decision") clearRetry(); }}
                onDecision={(decision, comment) => void execute({ kind: "decision", body: { request_id: crypto.randomUUID(), decision, comment } })}
                onResume={() => void execute({ kind: "resume", body: { request_id: crypto.randomUUID() } })} />}
        </div>
      </div>
    </section>
  );
}
