import clsx from "clsx";
import { Activity, ArrowUpRight, Check, ChevronDown, ClipboardList, Copy, MessageCircle, Package, RotateCcw, Send, User } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, fetchOrders, friendlyError, streamChat } from "../api";
import { getToken, onTokenChange } from "../auth";
import AgentPanel from "../components/AgentPanel";
import CustomerTickets from "../components/CustomerTickets";
import KnowledgeSources from "../components/KnowledgeSources";
import Logo from "../components/Logo";
import Markdown from "../components/Markdown";
import OrdersPanel, { OrderSummary } from "../components/OrdersPanel";
import { useToast } from "../components/Toaster";
import { Badge, Button, Drawer, IconButton, ResponsivePanel } from "../components/ui";
import { getDemo } from "../lib/demo";
import { useMeta } from "../lib/meta";
import { formatTime, toolLabel } from "../lib/locale";
import { OUTCOME_META, outcomeOf } from "../lib/outcome";
import { scenarioCards } from "../lib/scenarios";
import type { AgentEvent, ChatMessage, DomainEvent, KnowledgeSource, Order, StepEvent } from "../types";

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
      <div className={clsx("mt-1 flex items-center gap-2 text-[10px] text-subtle", user && "justify-end")}><span>{formatTime(m.at)}</span>{outcome && <Badge tone={outcome.tone}>{outcome.label}</Badge>}{!user && <CopyButton text={m.content} />}</div>
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
  const [domains, setDomains] = useState<DomainEvent[]>([]);
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
  const [drawer, setDrawer] = useState<"orders" | "service" | null>(null);
  const [progressOpen, setProgressOpen] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const chatController = useRef<AbortController | null>(null);
  const ordersController = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const sending = useRef(false);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; chatController.current?.abort(); ordersController.current?.abort(); }; }, []);
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

  function reset() { setMessages([]); setSteps([]); setDomains([]); setSources([]); setSourcesReceived(false); setTurnStart(null); setTurnEnd(null); setConversationId(null); setTicketId(null); }
  async function send(value: string) {
    const text = value.trim();
    if (!text || text.length > MAX_CHARS || sending.current || !token) return;
    sending.current = true;
    const controller = new AbortController(); chatController.current = controller;
    const alive = () => mounted.current && !controller.signal.aborted && getToken("customer") === token;
    const userId = uid();
    setMessages((rows) => [...rows, { id: userId, role: "user", content: text, at: Date.now() }]);
    setInput(""); setSteps([]); setDomains([]); setSources([]); setSourcesReceived(false); setTurnStart(Date.now()); setTurnEnd(null); setStreaming(true); setDrawer(null);
    const turnSteps: StepEvent[] = [];
    const handle = (event: AgentEvent) => {
      if (!alive()) return;
      switch (event.kind) {
        case "conversation": setConversationId(event.conversation_id); break;
        case "step": turnSteps.push(event); setSteps([...turnSteps]); break;
        case "message": setMessages((rows) => [...rows, { id: uid(), role: event.role, content: event.content, at: Date.now(), outcome: outcomeOf(turnSteps) }]); break;
        case "error": setMessages((rows) => [...rows, { id: uid(), role: "assistant", content: event.message, at: Date.now(), error: true }]); break;
        case "knowledge_sources": setSources(event.sources); setSourcesReceived(true); setDomains((rows) => [...rows, event]); break;
        case "ticket_update":
        case "workflow_status": setTicketId(event.ticket_id); setTicketVersion((v) => v + 1); setDomains((rows) => [...rows, event]); break;
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
      if (alive()) { setBusyNote(null); setStreaming(false); setTurnEnd(Date.now()); setTicketVersion((v) => v + 1); void loadOrders(); inputRef.current?.focus(); }
    }
  }
  const selectedOrder = orders.find((o) => o.id === orderId) ?? null;
  const tooLong = input.length > MAX_CHARS;
  const servicePanel = <div className="space-y-7 p-5">
    <div className="flex items-center gap-3 border-b border-line pb-5"><span className="flex h-11 w-11 items-center justify-center rounded-full bg-brand/10"><Logo size={27} /></span><div><h2 className="text-sm font-semibold">售后服务助手</h2><p className="mt-1 text-[11px] text-subtle">让每一步处理都有依据</p></div><Badge className="ml-auto" tone="brand">在线</Badge></div>
    {token ? <CustomerTickets refreshKey={ticketVersion} suggestedId={ticketId} onChanged={() => void loadOrders()} /> : <p className="text-xs text-muted">登录后查看您的工单。</p>}
    <KnowledgeSources sources={sources} received={sourcesReceived} />
    <p className="border-t border-line pt-4 text-[10px] leading-relaxed text-subtle">当前仅模拟售后受理，不涉及真实资金或物流。是否通过申请，以业务规则和工单事实为准。</p>
  </div>;

  return <div className="workbench">
    <div className="workspace-main">
      <div className="workspace-heading"><div><p className="eyebrow">SERVICE DESK / 售后服务</p><h1 className="mt-1 text-xl font-semibold tracking-tight">您好，今天有什么需要帮助？</h1></div><div className="flex items-center gap-1"><Button size="sm" variant="ghost" onClick={() => setDrawer("orders")} disabled={!token}><Package size={15} /> 订单<span className="hidden sm:inline">列表</span></Button><Button size="sm" variant="ghost" className="xl:hidden" onClick={() => setDrawer("service")}><ClipboardList size={15} /> 工单与来源</Button></div></div>
      {ordersError && <p role="alert" className="mb-3 text-xs text-bad">订单查询失败：{ordersError} <button className="underline" onClick={() => void loadOrders()}>重试</button></p>}
      <OrderSummary order={selectedOrder} orders={orders} onSelect={setOrderId} onAsk={(text) => void send(text)} disabled={streaming || !token} />
      <section className="chat-card" aria-label="售后对话">
        <div className="flex items-center justify-between border-b border-line/70 px-5 py-3"><div className="flex items-center gap-2 text-sm font-semibold"><MessageCircle size={16} className="text-brand" /> 客服对话 <span className="text-[10px] font-normal text-subtle">{demo?.customer.name || (token ? "当前客户" : "未登录")}</span></div><Button size="sm" variant="ghost" onClick={reset} disabled={streaming || !messages.length}><RotateCcw size={13} /> 新对话</Button></div>
        <div ref={scrollRef} className="chat-scroll"><div className="mx-auto max-w-4xl space-y-5 px-4 py-6 sm:px-6">
          {!token && <div className="py-12 text-center"><Logo size={44} /><p className="mt-4 text-sm text-muted">请先登录，再开始售后咨询。</p>{meta?.auth_mode === "dev" && <p className="mt-2 text-xs text-subtle">在顶部开发菜单中选择客户身份。</p>}</div>}
          {token && !messages.length && <div className="animate-fade-up py-2"><div className="mb-6 flex items-center gap-3"><span className="message-avatar"><Logo size={27} /></span><div><p className="text-base font-semibold">订单问题，我们一起解决。</p><p className="mt-1 text-xs leading-relaxed text-muted">查询订单、排查故障，或告诉我您的换货需求。</p></div></div><div className="grid gap-2 sm:grid-cols-3">{scenarioCards(selectedOrder ? [selectedOrder] : orders).map((card) => <button key={card.key} disabled={streaming} onClick={() => void send(card.prompt)} className="prompt-card"><ArrowUpRight size={15} className="mb-4 text-brand" /><p className="text-xs font-semibold">{card.title}</p><p className="mt-1 text-[11px] leading-relaxed text-subtle">{card.blurb}</p></button>)}</div></div>}
          {messages.map((m) => <Bubble key={m.id} message={m} />)}
          {streaming && <div className="flex items-center gap-3 text-xs text-muted" role="status"><span className="message-avatar"><Logo size={24} /></span><span className="flex gap-1">{[0, 150, 300].map((delay) => <span key={delay} className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand/60" style={{ animationDelay: `${delay}ms` }} />)}</span>{busyNote ?? statusLine(steps)}</div>}
        </div></div>
        <form className="composer-wrap" onSubmit={(e) => { e.preventDefault(); void send(input); }}><div className={clsx("composer", tooLong && "border-bad")}><label htmlFor="composer" className="sr-only">消息</label><textarea id="composer" ref={inputRef} value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(input); } }} rows={1} disabled={!token} placeholder={token ? "描述您的问题，或输入订单号…" : "请先选择客户身份"} className="min-h-[42px] flex-1 resize-none bg-transparent px-3 py-2.5 text-sm placeholder:text-subtle focus:outline-none focus-visible:ring-0" /><button type="submit" aria-label="发送消息" disabled={streaming || !token || !input.trim() || tooLong} className="send-button"><Send size={20} /></button></div><div className="mt-2 flex justify-between px-2 text-[10px] text-subtle"><span>回车发送 · Shift + 回车换行</span><span className={clsx(tooLong && "text-bad")}>{input.length > 1600 ? `${input.length} / ${MAX_CHARS}` : "业务结果以实际查询为准"}</span></div></form>
      </section>
    </div>
    <ResponsivePanel className="service-panel" open={drawer === "service"} onClose={() => setDrawer(null)} title="工单与知识来源">{servicePanel}</ResponsivePanel>
    <section className={clsx("progress-panel", progressOpen && "is-open")} aria-label="真实处理进度"><button className="progress-toggle" aria-expanded={progressOpen} onClick={() => setProgressOpen((open) => !open)}><span className="flex items-center gap-3"><span className="rounded-full bg-brand/10 p-2 text-brand"><Activity size={18} /></span><span className="text-left"><span className="block text-sm font-semibold">处理进度</span><span className="mt-0.5 block text-[10px] font-normal text-subtle">{steps.length ? `${steps.length} 个真实执行事件` : "发送消息后展示实际处理步骤"}{domains.length ? ` · ${domains.length} 条业务更新` : ""}</span></span></span><span className="flex items-center gap-2 text-xs text-subtle">{streaming ? "正在处理" : steps.length ? "查看记录" : "等待咨询"}<ChevronDown size={16} className={clsx("transition-transform", progressOpen && "rotate-180")} /></span></button>
      {progressOpen && <div className="progress-content"><AgentPanel steps={steps} streaming={streaming} startedAt={turnStart} endedAt={turnEnd} showTitle={false} /><div className="domain-events">{domains.map((event, i) => <div key={i} className="rounded-xl bg-surface-2 p-3 text-xs text-muted">{event.kind === "knowledge_sources" ? `检索返回 ${event.sources.length} 条知识引用` : event.kind === "ticket_update" ? `收到工单状态更新，正在查询最新事实（${event.ticket_id}）` : `收到工作流更新（${event.state || event.phase || event.status || "状态已更新"}），请查看最新工单。`}</div>)}</div></div>}
    </section>
    <Drawer open={drawer === "orders"} onClose={() => setDrawer(null)} title="我的订单"><OrdersPanel orders={orders} loading={ordersLoading} disabled={streaming} onAsk={(text) => void send(text)} onSelect={(id) => { setOrderId(id); setDrawer(null); }} /></Drawer>
  </div>;
}
