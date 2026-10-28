import clsx from "clsx";
import { Activity, ArrowUpRight, Check, ClipboardList, Copy, History, MessageCircle, Package, RotateCcw, Send, User } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, decideRefund, fetchRefundConfirmation, fetchMyConversation, fetchMyConversations, fetchOrders, friendlyError, streamChat } from "../api";
import { getToken, onTokenChange } from "../auth";
import AgentPanel from "../components/AgentPanel";
import CustomerTickets from "../components/CustomerTickets";
import ConversationHistory from "../components/ConversationHistory";
import KnowledgeSources from "../components/KnowledgeSources";
import Logo from "../components/Logo";
import Markdown from "../components/Markdown";
import OrdersPanel, { OrderSummary } from "../components/OrdersPanel";
import RefundConfirmationDialog from "../components/RefundConfirmationDialog";
import { useToast } from "../components/Toaster";
import { Badge, Button, Drawer, IconButton, ResponsivePanel } from "../components/ui";
import { getDemo } from "../lib/demo";
import { historyKey, restoreHistory } from "../lib/chatHistory";
import { useMeta } from "../lib/meta";
import { formatTime, toolLabel } from "../lib/locale";
import { OUTCOME_META, outcomeOf } from "../lib/outcome";
import { scenarioCards } from "../lib/scenarios";
import type { AgentEvent, ChatMessage, ConversationSummary, KnowledgeSource, Order, RefundConfirmation, StepEvent } from "../types";

const MAX_CHARS = 2000;
const uid = () => crypto.randomUUID();

function statusLine(steps: StepEvent[]): string {
  const last = [...steps].reverse().find((s) => !["usage", "trace"].includes(s.step_type));
  if (last?.step_type === "tool_call") return `正在${toolLabel(last.payload.tool)}…`;
  if (last?.step_type === "policy_eval") return "正在核验业务规则…";
  if (last?.step_type === "decision") return "正在整理回复…";
  return "正在为您处理…";
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return <IconButton label={done ? "已复制" : "复制消息"} className="h-6 w-6" onClick={async () => {
    try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1500); } catch { /* 剪贴板不可用。 */ }
  }}>{done ? <Check size={12} /> : <Copy size={12} />}</IconButton>;
}

function Bubble({ message: m }: { message: ChatMessage }) {
  const user = m.role === "user";
  const outcome = m.outcome ? OUTCOME_META[m.outcome] : null;
  return <div className={clsx("group flex animate-fade-up items-start gap-3", user && "flex-row-reverse")}>
    {user ? <span className="message-avatar user-avatar"><User size={17} /></span> : <span className="message-avatar"><Logo size={25} /></span>}
    <div className="min-w-0 max-w-[87%] sm:max-w-[80%]">
      <div className={clsx("message-bubble", user ? "user-bubble whitespace-pre-wrap" : "assistant-bubble", m.error && "border border-bad/30 bg-bad/5")}>
        {user ? m.content : <Markdown>{m.content}</Markdown>}
      </div>
      <div className={clsx("mt-1 flex items-center gap-2 text-xs text-subtle", user && "justify-end")}><span>{formatTime(m.at)}</span>{outcome && <Badge tone={outcome.tone}>{outcome.label}</Badge>}{!user && <CopyButton text={m.content} />}</div>
    </div>
  </div>;
}

/** 以完整 token 为挂载边界，同一角色更换 scope 也会清空敏感状态。 */
export default function Chat() {
  const [token, setToken] = useState(getToken("customer"));
  useEffect(() => onTokenChange(() => setToken(getToken("customer"))), []);
  return <ChatWorkspace key={token ?? "signed-out"} token={token} />;
}

function ChatWorkspace({ token }: { token: string | null }) {
  const { meta } = useMeta();
  const toast = useToast();
  const demo = meta?.auth_mode === "demo" ? getDemo() : null;
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [steps, setSteps] = useState<StepEvent[]>([]);
  const [sources, setSources] = useState<KnowledgeSource[]>([]);
  const [sourcesReceived, setSourcesReceived] = useState(false);
  const [turnStart, setTurnStart] = useState<number | null>(null);
  const [turnEnd, setTurnEnd] = useState<number | null>(null);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [busyNote, setBusyNote] = useState<string | null>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [ordersLoading, setOrdersLoading] = useState(false);
  const [ordersError, setOrdersError] = useState<string | null>(null);
  const [orderId, setOrderId] = useState<string | null>(null);
  const [ticketVersion, setTicketVersion] = useState(0);
  const [ticketId, setTicketId] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<"orders" | "service" | "history" | null>(null);
  const [histories, setHistories] = useState<ConversationSummary[]>([]);
  const [historyLoading, setHistoryLoading] = useState(Boolean(token));
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [restoring, setRestoring] = useState(Boolean(token));
  const [refundConfirmation, setRefundConfirmation] = useState<RefundConfirmation | null>(null);
  const [refundBusy, setRefundBusy] = useState(false);
  const [refundError, setRefundError] = useState<string | null>(null);
  const confirming = useRef(false);
  const currentConversation = useRef<string | null>(null);
  const confirmationController = useRef<AbortController | null>(null);
  const storageKey = historyKey(token);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const chatController = useRef<AbortController | null>(null);
  const ordersController = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const sending = useRef(false);
  const historyController = useRef<AbortController | null>(null);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; chatController.current?.abort(); ordersController.current?.abort(); historyController.current?.abort(); confirmationController.current?.abort(); }; }, []);
  function remember(id: string) { if (storageKey) { try { sessionStorage.setItem(storageKey, id); } catch { /* 无浏览器存储时仍可手动打开历史。 */ } } }
  const loadHistory = useCallback(async (signal?: AbortSignal) => {
    if (!token) return [];
    setHistoryLoading(true);
    try {
      const rows = await fetchMyConversations(signal);
      if (mounted.current && !signal?.aborted && getToken("customer") === token) { setHistories(rows); setHistoryError(null); }
      return rows;
    } catch (err) {
      if (mounted.current && !signal?.aborted) setHistoryError(friendlyError(err));
      return [];
    } finally { if (mounted.current && !signal?.aborted) setHistoryLoading(false); }
  }, [token]);
  async function openConversation(id: string, signal?: AbortSignal) {
    if (sending.current || confirming.current) return;
    setRestoring(true); setHistoryError(null);
    try {
      const [detail, proposal] = await Promise.all([fetchMyConversation(id, signal), fetchRefundConfirmation(id, signal)]);
      if (!mounted.current || signal?.aborted || getToken("customer") !== token) return;
      const saved = restoreHistory(detail);
      setMessages(saved.messages); setSteps(saved.steps); setSources(saved.sources); setSourcesReceived(saved.sourcesReceived);
      setTurnStart(saved.startedAt); setTurnEnd(saved.endedAt); setConversationId(id); setTicketId(null); remember(id); setDrawer(null);
      currentConversation.current = id; setRefundConfirmation(proposal); setRefundError(null);
    } catch (err) { if (mounted.current && !signal?.aborted) { setHistoryError(friendlyError(err)); setDrawer("history"); } }
    finally { if (mounted.current && !signal?.aborted) setRestoring(false); }
  }
  useEffect(() => {
    if (!token) return;
    const controller = new AbortController(); historyController.current = controller;
    void (async () => {
      const rows = await loadHistory(controller.signal);
      if (controller.signal.aborted) return;
      let saved: string | null = null;
      try { saved = storageKey ? sessionStorage.getItem(storageKey) : null; } catch { /* 直接使用服务端历史。 */ }
      const id = saved === "" ? null : rows.find((row) => row.id === saved)?.id ?? rows[0]?.id;
      if (id) await openConversation(id, controller.signal);
      else setRestoring(false);
    })();
    return () => controller.abort();
  }, [token, loadHistory, storageKey]);
  const loadOrders = useCallback(async () => {
    if (!token) return;
    ordersController.current?.abort();
    const controller = new AbortController(); ordersController.current = controller;
    setOrdersLoading(true);
    try {
      const rows = await fetchOrders(controller.signal);
      if (mounted.current && !controller.signal.aborted && getToken("customer") === token) { setOrders(rows); setOrdersError(null); setOrderId((id) => rows.some((o) => o.id === id) ? id : rows[0]?.id ?? null); }
    } catch (err) { if (mounted.current && !controller.signal.aborted) setOrdersError(friendlyError(err)); }
    finally { if (mounted.current && !controller.signal.aborted) setOrdersLoading(false); }
  }, [token]);
  useEffect(() => { void loadOrders(); }, [loadOrders]);
  useEffect(() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" }); }, [messages, streaming]);
  useEffect(() => { const el = inputRef.current; if (el) { el.style.height = "auto"; el.style.height = `${Math.min(el.scrollHeight, 160)}px`; } }, [input]);

  function reset() { currentConversation.current = null; setMessages([]); setSteps([]); setSources([]); setSourcesReceived(false); setTurnStart(null); setTurnEnd(null); setConversationId(null); setTicketId(null); setRefundConfirmation(null); setRefundError(null); remember(""); }
  async function decideConfirmation(decision: "confirm" | "cancel") {
    const proposal = refundConfirmation;
    if (!proposal || confirming.current || !token || currentConversation.current !== proposal.conversation_id) return;
    confirming.current = true; setRefundBusy(true); setRefundError(null);
    const controller = new AbortController(); confirmationController.current = controller;
    const alive = () => mounted.current && !controller.signal.aborted && getToken("customer") === token;
    try {
      await decideRefund(proposal.conversation_id, proposal.id, decision, controller.signal);
      if (!alive()) return;
      confirming.current = false;
      setRefundConfirmation(null);
      await openConversation(proposal.conversation_id, controller.signal);
      void loadOrders(); void loadHistory(); setTicketVersion((v) => v + 1);
    } catch (err) {
      if (!alive()) return;
      setRefundError(friendlyError(err));
      if (err instanceof ApiError && err.status === 409) {
        const current = await fetchRefundConfirmation(proposal.conversation_id, controller.signal).catch(() => proposal);
        if (alive() && !current) { setRefundConfirmation(null); toast(friendlyError(err), "error"); }
      }
    } finally { confirming.current = false; if (alive()) setRefundBusy(false); }
  }
  async function send(value: string) {
    const text = value.trim();
    if (!text || text.length > MAX_CHARS || sending.current || confirming.current || refundConfirmation || restoring || !token) return;
    sending.current = true;
    const controller = new AbortController(); chatController.current = controller;
    const alive = () => mounted.current && !controller.signal.aborted && getToken("customer") === token;
    const userId = uid();
    setMessages((rows) => [...rows, { id: userId, role: "user", content: text, at: Date.now() }]);
    setInput(""); setSteps([]); setSources([]); setSourcesReceived(false); setTurnStart(Date.now()); setTurnEnd(null); setStreaming(true); setDrawer(null);
    const turnSteps: StepEvent[] = [];
    let activeConversation = conversationId;
    const handle = (event: AgentEvent) => {
      if (!alive()) return;
      switch (event.kind) {
        case "conversation": activeConversation = event.conversation_id; currentConversation.current = event.conversation_id; setConversationId(event.conversation_id); remember(event.conversation_id); break;
        case "step": turnSteps.push(event); setSteps([...turnSteps]); break;
        case "message": setMessages((rows) => [...rows, { id: uid(), role: event.role, content: event.content, at: Date.now(), outcome: outcomeOf(turnSteps) }]); break;
        case "error": setMessages((rows) => [...rows, { id: uid(), role: "assistant", content: event.message, at: Date.now(), error: true }]); break;
        case "knowledge_sources": setSources(event.sources); setSourcesReceived(true); break;
        case "ticket_update":
        case "workflow_status": setTicketId(event.ticket_id); setTicketVersion((v) => v + 1); break;
      }
    };
    try {
      // 只有执行前拒绝的 503 自动重试；流已开始后的异常不重新提交副作用。
      for (let attempt = 0; ; attempt++) {
        if (!alive()) return;
        try { await streamChat(text, conversationId, handle, controller.signal); break; }
        catch (err) {
          if (!(err instanceof ApiError && err.status === 503) || attempt >= 2) throw err;
          const wait = (err.retryAfter ?? 5) * (1 + Math.random() * 0.5);
          if (!alive()) return;
          setBusyNote(`服务繁忙，将在 ${Math.round(wait)} 秒后重试…`);
          await new Promise<void>((resolve) => { const timer = setTimeout(resolve, wait * 1000); controller.signal.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true }); });
          if (alive()) setBusyNote(null);
        }
      }
    } catch (err) {
      if (!alive()) return;
      const msg = friendlyError(err);
      if (err instanceof ApiError && [409, 429].includes(err.status)) { toast(msg, "error"); setMessages((rows) => rows.filter((m) => m.id !== userId)); setInput(text); }
      else setMessages((rows) => [...rows, { id: uid(), role: "assistant", content: msg, at: Date.now(), error: true }]);
      if (err instanceof ApiError && err.status === 404) setConversationId(null);
    } finally {
      sending.current = false;
      if (alive()) {
        setBusyNote(null); setStreaming(false); setTurnEnd(Date.now()); setTicketVersion((v) => v + 1);
        void loadOrders(); void loadHistory(); inputRef.current?.focus();
        if (activeConversation) {
          try { const proposal = await fetchRefundConfirmation(activeConversation, controller.signal); if (alive() && currentConversation.current === activeConversation) { setRefundConfirmation(proposal); setRefundError(null); } }
          catch (err) { if (alive()) toast(`退款确认信息读取失败，可在历史对话中重新打开：${friendlyError(err)}`, "error"); }
        }
      }
    }
  }
  const selectedOrder = orders.find((o) => o.id === orderId) ?? null;
  const tooLong = input.length > MAX_CHARS;
  const servicePanel = <div className="service-content">
    <section className="sidebar-progress" aria-label="处理进度">
      <div className="flex items-center justify-between px-4 pt-4"><h2 className="flex items-center gap-2 text-base font-semibold"><Activity size={18} className="text-brand" />处理进度</h2><Badge tone={streaming ? "info" : "neutral"}>{streaming ? "正在处理" : steps.length ? "本轮记录" : "等待咨询"}</Badge></div>
      <AgentPanel steps={steps} streaming={streaming} startedAt={turnStart} endedAt={turnEnd} showTitle={false} />
    </section>
    {token ? <CustomerTickets refreshKey={ticketVersion} suggestedId={ticketId} onChanged={() => void loadOrders()} /> : <p className="text-sm text-muted">登录后查看您的工单。</p>}
    <KnowledgeSources sources={sources} received={sourcesReceived} />
    <p className="border-t border-line pt-4 text-xs leading-relaxed text-subtle">当前仅模拟售后受理，不涉及真实资金或物流。是否通过申请，以业务规则和工单事实为准。</p>
  </div>;

  return <div className="workbench">
    <div className="workspace-main">
      <div className="workspace-heading"><div><p className="eyebrow">SERVICE DESK / 售后服务</p><h1 className="mt-1 text-xl font-semibold tracking-tight">您好，今天有什么需要帮助？</h1></div><div className="flex items-center gap-1"><Button size="sm" variant="ghost" onClick={() => setDrawer("orders")} disabled={!token || restoring}><Package size={15} /> 订单<span className="hidden sm:inline">列表</span></Button><Button size="sm" variant="ghost" className="lg:hidden" onClick={() => setDrawer("service")}><ClipboardList size={15} /> 进度与工单</Button></div></div>
      {ordersError && <p role="alert" className="mb-3 text-sm text-bad">订单查询失败：{ordersError} <button className="underline" onClick={() => void loadOrders()}>重试</button></p>}
      <OrderSummary order={selectedOrder} orders={orders} onSelect={setOrderId} onAsk={(text) => void send(text)} disabled={streaming || restoring || !token} />
      <section className="chat-card" aria-label="售后对话">
        <div className="flex items-center justify-between border-b border-line/70 px-5 py-3"><div className="flex items-center gap-2 text-sm font-semibold"><MessageCircle size={16} className="text-brand" /> 客服对话 <span className="text-xs font-normal text-subtle">{demo?.customer.name || (token ? "当前客户" : "未登录")}</span></div><Button size="sm" variant="ghost" onClick={() => { setDrawer("history"); void loadHistory(); }} disabled={!token || streaming || restoring}><History size={16} /> 历史对话</Button><Button size="sm" variant="ghost" onClick={reset} disabled={streaming || restoring || !messages.length}><RotateCcw size={13} /> 新对话</Button></div>
        <div ref={scrollRef} className="chat-scroll"><div className="mx-auto max-w-4xl space-y-5 px-4 py-6 sm:px-6">
          {!token && <div className="py-12 text-center"><Logo size={44} /><p className="mt-4 text-sm text-muted">请先登录，再开始售后咨询。</p>{meta?.auth_mode === "dev" && <p className="mt-2 text-sm text-subtle">在顶部开发菜单中选择客户身份。</p>}</div>}
          {token && !restoring && !messages.length && <div className="animate-fade-up py-2"><div className="mb-6 flex items-center gap-3"><span className="message-avatar"><Logo size={27} /></span><div><p className="text-base font-semibold">订单问题，我们一起解决。</p><p className="mt-1 text-sm leading-relaxed text-muted">查询订单、排查故障，或告诉我您的换货需求。</p></div></div><div className="grid gap-2 sm:grid-cols-3">{scenarioCards(selectedOrder ? [selectedOrder] : orders).map((card) => <button key={card.key} disabled={streaming || restoring} onClick={() => void send(card.prompt)} className="prompt-card"><ArrowUpRight size={15} className="mb-4 text-brand" /><p className="text-sm font-semibold">{card.title}</p><p className="mt-1 text-xs leading-relaxed text-subtle">{card.blurb}</p></button>)}</div></div>}
          {restoring && <p role="status" className="py-8 text-center text-sm text-muted">正在加载您的历史对话…</p>}{messages.map((m) => <Bubble key={m.id} message={m} />)}
          {streaming && <div className="flex items-center gap-3 text-sm text-muted" role="status"><span className="message-avatar"><Logo size={24} /></span><span className="flex gap-1">{[0, 150, 300].map((delay) => <span key={delay} className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand/60" style={{ animationDelay: `${delay}ms` }} />)}</span>{busyNote ?? statusLine(steps)}</div>}
        </div></div>
        <form className="composer-wrap" onSubmit={(e) => { e.preventDefault(); void send(input); }}><div className={clsx("composer", tooLong && "border-bad")}><label htmlFor="composer" className="sr-only">消息</label><textarea id="composer" ref={inputRef} value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(input); } }} rows={1} disabled={!token || restoring} placeholder={token ? "描述您的问题，或输入订单号…" : "请先选择客户身份"} className="min-h-[42px] flex-1 resize-none bg-transparent px-3 py-2.5 text-sm placeholder:text-subtle focus:outline-none focus-visible:ring-0" /><button type="submit" aria-label="发送消息" disabled={streaming || restoring || !token || !input.trim() || tooLong} className="send-button"><Send size={20} /></button></div><div className="mt-2 flex justify-between px-2 text-xs text-subtle"><span>回车发送 · Shift + 回车换行</span><span className={clsx(tooLong && "text-bad")}>{input.length > 1600 ? `${input.length} / ${MAX_CHARS}` : "业务结果以实际查询为准"}</span></div></form>
      </section>
    </div>
    {refundConfirmation && <RefundConfirmationDialog proposal={refundConfirmation} busy={refundBusy} error={refundError} onDecide={(decision) => void decideConfirmation(decision)} />}
    <ResponsivePanel className="service-panel" open={drawer === "service"} onClose={() => setDrawer(null)} title="处理进度与售后工单">{servicePanel}</ResponsivePanel>
    <Drawer open={drawer === "orders"} onClose={() => setDrawer(null)} title="我的订单"><OrdersPanel orders={orders} loading={ordersLoading} disabled={streaming || restoring} onAsk={(text) => void send(text)} onSelect={(id) => { setOrderId(id); setDrawer(null); }} /></Drawer>
    <Drawer open={drawer === "history"} onClose={() => setDrawer(null)} title="历史对话"><ConversationHistory rows={histories} currentId={conversationId} loading={historyLoading || restoring} error={historyError} onSelect={(id) => void openConversation(id)} onRefresh={() => void loadHistory()} /></Drawer>
  </div>;
}
