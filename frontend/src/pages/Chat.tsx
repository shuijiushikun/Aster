import clsx from "clsx";
import { Activity, ArrowUp, Check, Copy, Package, RotateCcw, User } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, fetchOrders, friendlyError, streamChat } from "../api";
import { getToken, onTokenChange } from "../auth";
import AgentPanel from "../components/AgentPanel";
import Logo from "../components/Logo";
import Markdown from "../components/Markdown";
import OrdersPanel from "../components/OrdersPanel";
import { useToast } from "../components/Toaster";
import { Badge, Button, Drawer, IconButton } from "../components/ui";
import { getDemo } from "../lib/demo";
import { useMeta } from "../lib/meta";
import { formatTime, toolLabel } from "../lib/locale";
import { OUTCOME_META, outcomeOf } from "../lib/outcome";
import { GENERIC_PROMPTS, scenarioCards, type ScenarioCard } from "../lib/scenarios";
import type { AgentEvent, ChatMessage, Order, StepEvent } from "../types";

const MAX_CHARS = 2000;
const uid = () => Math.random().toString(36).slice(2);

function statusLine(steps: StepEvent[]): string {
  const last = [...steps]
    .reverse()
    .find((s) => s.step_type !== "usage" && s.step_type !== "trace");
  if (!last) return "正在处理…";
  switch (last.step_type) {
    case "tool_call":
      return `正在${toolLabel(last.payload.tool)}…`;
    case "policy_eval":
      return "正在校验退款规则…";
    case "decision":
      return "正在整理回复…";
    case "injection_flag":
      return "已标记可疑指令，继续按规则处理…";
    default:
      return "正在处理…";
  }
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <IconButton
      label={done ? "已复制" : "复制消息"}
      className="h-6 w-6"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        } catch {
          /* 剪贴板不可用时忽略复制失败。 */
        }
      }}
    >
      {done ? <Check size={12} /> : <Copy size={12} />}
    </IconButton>
  );
}

function Bubble({ m }: { m: ChatMessage }) {
  const time = formatTime(m.at);
  if (m.role === "user") {
    return (
      <div className="flex animate-fade-up justify-end gap-2.5">
        <div className="max-w-[85%]">
          <div className="whitespace-pre-wrap rounded-2xl rounded-br-md bg-brand-solid px-4 py-2.5 text-sm text-on-brand shadow-sm">
            {m.content}
          </div>
          <div className="mt-1 text-right text-[10px] text-subtle">{time}</div>
        </div>
        <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-surface-2 text-muted">
          <User size={14} />
        </span>
      </div>
    );
  }
  const outcome = m.outcome ? OUTCOME_META[m.outcome] : null;
  return (
    <div className="group flex animate-fade-up gap-2.5">
      <span className="mt-0.5 shrink-0">
        <Logo size={28} />
      </span>
      <div className="min-w-0 max-w-[85%]">
        <div
          className={clsx(
            "rounded-2xl rounded-bl-md border px-4 py-2.5 text-sm shadow-sm",
            m.error ? "border-bad/30 bg-bad/5" : "border-line bg-surface",
          )}
        >
          <Markdown>{m.content}</Markdown>
        </div>
        <div className="mt-1 flex items-center gap-2 text-[10px] text-subtle">
          <span>{time}</span>
          {outcome && <Badge tone={outcome.tone}>{outcome.label}</Badge>}
          <span className="opacity-0 transition-opacity group-hover:opacity-100">
            <CopyButton text={m.content} />
          </span>
        </div>
      </div>
    </div>
  );
}

function Typing({ label }: { label: string }) {
  return (
    <div className="flex animate-fade-up gap-2.5" aria-live="polite">
      <span className="mt-0.5 shrink-0">
        <Logo size={28} />
      </span>
      <div className="flex items-center gap-3 rounded-2xl rounded-bl-md border border-line bg-surface px-4 py-3 text-xs text-muted">
        <span className="flex gap-1" aria-hidden>
          {[0, 150, 300].map((d) => (
            <span
              key={d}
              className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand"
              style={{ animationDelay: `${d}ms` }}
            />
          ))}
        </span>
        {label}
      </div>
    </div>
  );
}

function ScenarioGrid({ cards, onPick }: { cards: ScenarioCard[]; onPick: (p: string) => void }) {
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {cards.map((c, i) => {
        const expect = c.expect === "info" ? null : OUTCOME_META[c.expect];
        return (
          <button
            key={c.key}
            onClick={() => onPick(c.prompt)}
            style={{ animationDelay: `${i * 40}ms` }}
            className="group animate-fade-up rounded-xl border border-line bg-surface p-3.5 text-left shadow-sm transition-all hover:-translate-y-0.5 hover:border-brand/50 hover:shadow-md"
          >
            <div className="flex items-start justify-between gap-2">
              <span className="text-sm font-semibold group-hover:text-brand">{c.title}</span>
              {expect && (
                <Badge tone={expect.tone} className="shrink-0">
                  预期：{expect.label}
                </Badge>
              )}
            </div>
            {c.blurb && <p className="mt-1 text-xs text-muted">{c.blurb}</p>}
            <p className="mt-2 line-clamp-2 font-mono text-[11px] text-subtle">“{c.prompt}”</p>
          </button>
        );
      })}
    </div>
  );
}

export default function Chat() {
  const { meta } = useMeta();
  const toast = useToast();
  const demo = meta?.auth_mode === "demo" ? getDemo() : null;

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [steps, setSteps] = useState<StepEvent[]>([]);
  const [turnStart, setTurnStart] = useState<number | null>(null);
  const [turnEnd, setTurnEnd] = useState<number | null>(null);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [signedIn, setSignedIn] = useState(Boolean(getToken("customer")));
  const [busyNote, setBusyNote] = useState<string | null>(null);
  const [orders, setOrders] = useState<Order[]>(demo?.orders ?? []);
  const [ordersLoading, setOrdersLoading] = useState(false);
  const [drawer, setDrawer] = useState<"orders" | "agent" | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const reset = useCallback(() => {
    setMessages([]);
    setSteps([]);
    setTurnStart(null);
    setTurnEnd(null);
    setConversationId(null);
  }, []);

  const loadOrders = useCallback(async () => {
    if (!getToken("customer")) return setOrders([]);
    setOrdersLoading(true);
    try {
      setOrders(await fetchOrders());
    } catch {
      /* 请求失败时，面板保留上一次的状态。 */
    } finally {
      setOrdersLoading(false);
    }
  }, []);

  // 身份变化时清空当前会话，避免把上一位客户的消息带入新账户。
  useEffect(
    () =>
      onTokenChange(() => {
        setSignedIn(Boolean(getToken("customer")));
        reset();
        void loadOrders();
      }),
    [reset, loadOrders],
  );
  useEffect(() => {
    void loadOrders();
  }, [loadOrders]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, streaming]);

  // 输入内容变多时自动增高文本框，达到上限后改为框内滚动。
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }, [input]);

  async function send(text: string) {
    text = text.trim();
    if (!text || text.length > MAX_CHARS || streaming || !signedIn) return;
    // 先在本地显示用户消息，再发给后端；本地显示并不代表后端已保存或处理成功。
    setMessages((m) => [...m, { id: uid(), role: "user", content: text, at: Date.now() }]);
    setInput("");
    setSteps([]);
    setTurnStart(Date.now());
    setTurnEnd(null);
    setStreaming(true);
    setDrawer(null);

    // 每轮重新收集执行步骤；它们用于右侧时间线和结果徽章，聊天气泡单独存放。
    const turnSteps: StepEvent[] = [];
    const handle = (event: AgentEvent) => {
      switch (event.kind) {
        case "conversation":
          // 记住后端会话 ID，下一次发送时沿用，后端才能加载本轮之前的对话。
          setConversationId(event.conversation_id);
          break;
        case "step":
          // 工具调用、规则校验和模型回复等步骤到达后立即更新右侧执行面板。
          turnSteps.push(event);
          setSteps([...turnSteps]);
          break;
        case "message":
          // message 才是整轮最终回复；step 中的中途说明不直接作为新的聊天气泡。
          setMessages((m) => [
            ...m,
            {
              id: uid(),
              role: "assistant",
              content: event.content,
              at: Date.now(),
              outcome: outcomeOf(turnSteps),
            },
          ]);
          break;
        case "error":
          setMessages((m) => [
            ...m,
            { id: uid(), role: "assistant", content: event.message, at: Date.now(), error: true },
          ]);
          break;
      }
    };

    try {
      // 503 表示请求在执行前被限流，可以按 Retry-After 重试，避免重复退款。
      for (let attempt = 0; ; attempt++) {
        try {
          await streamChat(text, conversationId, handle);
          break;
        } catch (err) {
          if (!(err instanceof ApiError && err.status === 503) || attempt >= 2) throw err;
          const wait = (err.retryAfter ?? 5) * (1 + Math.random() * 0.5);
          setBusyNote(`服务繁忙，将在 ${Math.round(wait)} 秒后重试…`);
          await new Promise((r) => setTimeout(r, wait * 1000));
          setBusyNote(null);
        }
      }
    } catch (err) {
      const msg = friendlyError(err);
      if (err instanceof ApiError && [409, 429].includes(err.status)) {
        toast(msg, "error");
        setMessages((m) => m.slice(0, -1));
        setInput(text);
      } else {
        setMessages((m) => [
          ...m,
          { id: uid(), role: "assistant", content: msg, at: Date.now(), error: true },
        ]);
      }
      if (err instanceof ApiError && err.status === 404) setConversationId(null);
    } finally {
      setBusyNote(null);
      setStreaming(false);
      setTurnEnd(Date.now());
      // 一轮结束后重新查询数据库中的订单状态，让左侧及时显示是否已退款。
      void loadOrders();
      inputRef.current?.focus();
    }
  }

  const cards = orders.some((o) => o.scenario) ? scenarioCards(orders) : GENERIC_PROMPTS;
  const tooLong = input.length > MAX_CHARS;

  const ordersPanel = (
    <OrdersPanel orders={orders} loading={ordersLoading} onAsk={send} disabled={streaming} />
  );
  const agentPanel = (
    <AgentPanel steps={steps} streaming={streaming} startedAt={turnStart} endedAt={turnEnd} />
  );

  return (
    <div className="flex h-full">
      {signedIn && (
        <aside className="hidden w-72 shrink-0 flex-col border-r border-line bg-surface/50 xl:flex">
          <div className="flex items-center gap-2 border-b border-line px-4 py-3 text-sm font-semibold">
            <Package size={16} className="text-brand" /> 我的订单
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">{ordersPanel}</div>
        </aside>
      )}

      <section className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-line px-4 py-2">
          <div className="hidden min-w-0 flex-1 truncate text-xs text-subtle sm:block">
            {demo ? (
              <>
                当前客户：<span className="font-medium text-fg">{demo.customer.name}</span>{" "}
                <span className="font-mono">({demo.customer.id})</span>
              </>
            ) : signedIn ? (
              "已登录"
            ) : (
              "未登录"
            )}
          </div>
          {signedIn && (
            <Button size="sm" variant="ghost" className="ml-auto xl:hidden sm:ml-0" onClick={() => setDrawer("orders")}>
              <Package size={14} /> 订单
            </Button>
          )}
          <Button size="sm" variant="ghost" className="lg:hidden" onClick={() => setDrawer("agent")}>
            <Activity size={14} /> 执行过程
            {steps.length > 0 && <Badge tone="brand">{steps.length}</Badge>}
          </Button>
          <Button size="sm" variant="ghost" onClick={reset} disabled={streaming || !messages.length}>
            <RotateCcw size={14} /> 新对话
          </Button>
        </div>

        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto max-w-3xl space-y-5 px-4 py-6">
            {!signedIn && (
              <div className="mt-16 text-center text-sm text-muted">
                请先登录，再与客服对话。
                {meta?.auth_mode === "dev" && "（本地开发：请在上方“开发”菜单中选择客户。）"}
              </div>
            )}

            {signedIn && messages.length === 0 && (
              <div className="animate-fade-up pt-4">
                <div className="mb-6 flex items-center gap-3">
                  <Logo size={40} />
                  <div>
                    <h1 className="text-lg font-semibold">您好，有什么订单问题需要帮助？</h1>
                    <p className="text-sm text-muted">
                      选择场景体验退款流程，也可以直接输入您的问题。
                    </p>
                  </div>
                </div>
                <ScenarioGrid cards={cards} onPick={send} />
              </div>
            )}

            {messages.map((m) => (
              <Bubble key={m.id} m={m} />
            ))}
            {streaming && <Typing label={busyNote ?? statusLine(steps)} />}
          </div>
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            void send(input);
          }}
          className="border-t border-line bg-surface/60 px-4 py-3"
        >
          <div className="mx-auto max-w-3xl">
            <div
              className={clsx(
                "flex items-end gap-2 rounded-2xl border bg-surface p-2 shadow-sm transition-colors focus-within:border-brand/60",
                tooLong ? "border-bad/60" : "border-line",
              )}
            >
              <label htmlFor="composer" className="sr-only">
                消息
              </label>
              <textarea
                id="composer"
                ref={inputRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    void send(input);
                  }
                }}
                rows={1}
                disabled={!signedIn}
                placeholder={signedIn ? "查询订单或申请退款…" : "请先登录"}
                className="max-h-44 min-h-[40px] flex-1 resize-none bg-transparent px-2 py-2 text-sm placeholder:text-subtle focus:outline-none focus-visible:ring-0 focus-visible:ring-offset-0"
              />
              <button
                type="submit"
                aria-label="发送消息"
                disabled={streaming || !signedIn || !input.trim() || tooLong}
                className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-brand-solid text-on-brand transition-opacity hover:opacity-90 disabled:opacity-30"
              >
                <ArrowUp size={18} />
              </button>
            </div>
            <div className="mt-1.5 flex justify-between px-1 text-[10px] text-subtle">
              <span>回车发送 · Shift+回车换行</span>
              <span className={clsx(tooLong && "font-semibold text-bad")}>
                {input.length > MAX_CHARS * 0.8 && `${input.length}/${MAX_CHARS}`}
              </span>
            </div>
          </div>
        </form>
      </section>

      <aside className="hidden w-96 shrink-0 border-l border-line bg-surface/50 lg:block">
        {agentPanel}
      </aside>

      <Drawer open={drawer === "orders"} onClose={() => setDrawer(null)} title="我的订单">
        {ordersPanel}
      </Drawer>
      <Drawer open={drawer === "agent"} onClose={() => setDrawer(null)} title="智能体执行过程">
        <AgentPanel
          steps={steps}
          streaming={streaming}
          startedAt={turnStart}
          endedAt={turnEnd}
          showTitle={false}
        />
      </Drawer>
    </div>
  );
}
