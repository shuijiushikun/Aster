import clsx from "clsx";
import {
  ArrowLeft,
  CheckCircle2,
  Coins,
  MessagesSquare,
  Search,
  UserRoundCog,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  fetchConversation,
  fetchConversations,
  fetchStats,
  friendlyError,
  subscribeToConversation,
} from "../api";
import { getToken, onTokenChange } from "../auth";
import Markdown from "../components/Markdown";
import AdminTickets from "../components/AdminTickets";
import ReasoningTimeline from "../components/ReasoningTimeline";
import { Badge, LiveDot, Skeleton } from "../components/ui";
import { useMeta } from "../lib/meta";
import { plainText } from "../lib/text";
import { formatDate, parseDate } from "../lib/locale";
import type { Tone } from "../lib/outcome";
import type {
  AdminStats,
  AgentEvent,
  ConversationDetail,
  ConversationSummary,
  StepEvent,
} from "../types";

function Kpi({
  icon: Icon,
  label,
  value,
  tone = "neutral",
}: {
  icon: LucideIcon;
  label: string;
  value: number | string | undefined;
  tone?: Tone;
}) {
  const color = {
    ok: "text-ok bg-ok/10",
    bad: "text-bad bg-bad/10",
    warn: "text-warn bg-warn/10",
    info: "text-info bg-info/10",
    brand: "text-brand bg-brand/10",
    neutral: "text-muted bg-surface-2",
  }[tone];
  return (
    <div className="flex items-center gap-3 rounded-xl border border-line bg-surface px-3.5 py-3 shadow-sm">
      <span className={clsx("flex h-9 w-9 shrink-0 items-center justify-center rounded-lg", color)}>
        <Icon size={17} />
      </span>
      <div className="min-w-0">
        <div className="truncate text-[11px] font-medium text-subtle">{label}</div>
        <div className="text-lg font-semibold tabular-nums leading-tight">
          {value === undefined ? <Skeleton className="mt-1 h-4 w-10" /> : value}
        </div>
      </div>
    </div>
  );
}

function ago(iso: string): string {
  const s = Math.max(0, (Date.now() - parseDate(iso).getTime()) / 1000);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return formatDate(iso);
}

export default function Admin() {
  const { meta } = useMeta();
  const [conversations, setConversations] = useState<ConversationSummary[] | null>(null);
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [adminToken, setAdminToken] = useState(getToken("admin"));
  const [view, setView] = useState<"conversations" | "tickets">("conversations");
  const hasToken = Boolean(adminToken);
  const timelineRef = useRef<HTMLDivElement>(null);

  useEffect(() => onTokenChange(() => setAdminToken(getToken("admin"))), []);

  useEffect(() => {
    if (!hasToken) return;
    let active = true;
    const load = async () => {
      try {
        const [list, s] = await Promise.all([fetchConversations(), fetchStats()]);
        if (!active) return;
        setConversations(list);
        setStats(s);
        setError(null);
        // 桌面端默认打开最近的会话。
        setSelectedId((id) => id ?? (window.innerWidth >= 1024 ? list[0]?.id ?? null : null));
      } catch (err) {
        if (active) setError(friendlyError(err));
      }
    };
    load();
    const t = setInterval(load, 5000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, [hasToken]);

  useEffect(() => {
    if (!selectedId) return;
    let active = true;
    setLive(false);
    setDetail(null);
    fetchConversation(selectedId)
      .then((d) => active && setDetail(d))
      .catch((err) => setError(friendlyError(err)));

    const handle = (event: AgentEvent) => {
      setLive(true);
      if (event.kind === "step") {
        setDetail((d) =>
          d && d.id === selectedId ? { ...d, events: mergeStep(d.events, event) } : d,
        );
      } else if (event.kind === "message") {
        setDetail((d) =>
          d && d.id === selectedId
            ? {
                ...d,
                messages: [
                  ...d.messages,
                  {
                    id: Date.now(),
                    role: event.role,
                    content: event.content,
                    created_at: new Date().toISOString(),
                  },
                ],
              }
            : d,
        );
      }
    };
    const close = subscribeToConversation(selectedId, handle);
    return () => {
      active = false;
      close();
    };
  }, [selectedId]);

  useEffect(() => {
    timelineRef.current?.scrollTo({ top: timelineRef.current.scrollHeight, behavior: "smooth" });
  }, [detail?.events.length]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!conversations || !q) return conversations;
    return conversations.filter((c) =>
      [c.customer_name, c.customer_id, c.last_message, c.id].some((v) =>
        v?.toLowerCase().includes(q),
      ),
    );
  }, [conversations, query]);

  if (!hasToken) {
    return (
      <div className="flex h-full items-center justify-center px-6 text-center text-sm text-muted">
        请先开启管理员身份。
        {meta?.auth_mode === "dev" && "（本地开发：请打开上方“开发”菜单中的管理开关。）"}
      </div>
    );
  }

  const d = stats?.decisions ?? {};
  const scoped = meta?.auth_mode === "demo";

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-line px-4 py-4 sm:px-5">
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <div>
            <h1 className="text-base font-semibold">管理控制台</h1>
            <p className="text-[10px] text-subtle">时间按北京时间显示</p>
            <p className="text-xs text-subtle">
              {scoped
                ? "查看对话记录与执行过程；当前身份只能访问自己的演示数据。"
                : "查看所有会话的对话记录、执行过程与退款结果。"}
            </p>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
          <Kpi icon={MessagesSquare} label="会话总数" value={stats?.conversations} tone="brand" />
          <Kpi icon={CheckCircle2} label="已批准" value={stats ? d.approved ?? 0 : undefined} tone="ok" />
          <Kpi icon={XCircle} label="已拒绝" value={stats ? d.denied ?? 0 : undefined} tone="bad" />
          <Kpi icon={UserRoundCog} label="转人工" value={stats ? d.escalated ?? 0 : undefined} tone="warn" />
          <Kpi icon={Coins} label="模型词元用量" value={stats?.tokens_used.toLocaleString("zh-CN")} tone="info" />
        </div>
        <nav className="mt-3 flex gap-2" aria-label="管理台视图">
          {([ ["conversations", "会话记录"], ["tickets", "工单审批"] ] as const).map(([key, label]) => (
            <button key={key} onClick={() => setView(key)} aria-pressed={view === key}
              className={clsx("rounded-lg px-3 py-1.5 text-xs font-medium", view === key ? "bg-surface-2 text-fg" : "text-muted hover:bg-surface-2")}>
              {label}
            </button>
          ))}
        </nav>
      </div>

      <div className={clsx("min-h-0 flex-1 flex-col", view === "tickets" ? "flex" : "hidden")}>
        <AdminTickets key={adminToken} />
      </div>
      <div className={clsx("min-h-0 flex-1 lg:grid-cols-[300px_1fr]", view === "conversations" ? "grid" : "hidden")}>
        <aside
          className={clsx(
            "min-h-0 flex-col border-r border-line bg-surface/50",
            selectedId ? "hidden lg:flex" : "flex",
          )}
        >
          <div className="border-b border-line p-3">
            <label className="flex items-center gap-2 rounded-lg border border-line bg-surface px-2.5 py-1.5 focus-within:border-brand/60">
              <Search size={14} className="text-subtle" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索会话"
                aria-label="搜索会话"
                className="w-full bg-transparent text-sm placeholder:text-subtle focus:outline-none focus-visible:ring-0 focus-visible:ring-offset-0"
              />
            </label>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {error && <p className="p-4 text-xs text-bad">{error}</p>}
            {conversations === null && !error && (
              <div className="space-y-2 p-3">
                {[0, 1, 2].map((i) => (
                  <Skeleton key={i} className="h-16" />
                ))}
              </div>
            )}
            {filtered?.length === 0 && (
              <div className="p-6 text-center text-sm text-subtle">
                {query ? "没有匹配的会话。" : "暂无会话。"}
                {!query && (
                  <div className="mt-2">
                    <Link to="/chat" className="font-medium text-brand hover:underline">
                      前往客服页开始对话
                    </Link>
                  </div>
                )}
              </div>
            )}
            {filtered?.map((c) => (
              <button
                key={c.id}
                onClick={() => setSelectedId(c.id)}
                className={clsx(
                  "block w-full border-b border-line/70 px-4 py-3 text-left transition-colors",
                  selectedId === c.id
                    ? "border-l-2 border-l-brand bg-brand/5"
                    : "hover:bg-surface-2",
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium">
                    {c.customer_name || "未知客户"}
                  </span>
                  <span className="shrink-0 text-[11px] text-subtle">{ago(c.created_at)}</span>
                </div>
                <div className="mt-0.5 truncate text-xs text-muted">{plainText(c.last_message) || "—"}</div>
                <div className="mt-1 flex items-center gap-2 text-[10px] text-subtle">
                  <span className="font-mono">{c.customer_id}</span>·<span>{c.message_count} 条消息</span>
                </div>
              </button>
            ))}
          </div>
        </aside>

        {!selectedId ? (
          <div className="hidden items-center justify-center p-6 text-sm text-subtle lg:flex">
            选择会话，查看智能体的执行过程。
          </div>
        ) : (
          <div className="grid min-h-0 grid-rows-[auto_1fr] md:grid-cols-2 md:grid-rows-1">
            <section className="flex min-h-0 flex-col border-b border-line md:border-b-0 md:border-r">
              <div className="flex items-center gap-2 border-b border-line px-4 py-3">
                <button
                  onClick={() => setSelectedId(null)}
                  className="text-muted hover:text-fg lg:hidden"
                  aria-label="返回会话列表"
                >
                  <ArrowLeft size={16} />
                </button>
                <div>
                  <div className="text-sm font-semibold">对话记录</div>
                  <div className="text-xs text-subtle">
                    {detail?.customer_name ?? "…"} ·{" "}
                    <span className="font-mono">{detail?.customer_id}</span>
                  </div>
                </div>
              </div>
              <div className="max-h-[40vh] min-h-0 flex-1 space-y-3 overflow-y-auto p-4 md:max-h-none">
                {!detail && <Skeleton className="h-24" />}
                {detail?.messages.map((m) => (
                  <div
                    key={m.id}
                    className={clsx("flex", m.role === "user" ? "justify-end" : "justify-start")}
                  >
                    <div
                      className={clsx(
                        "max-w-[90%] rounded-xl px-3 py-2 text-sm",
                        m.role === "user"
                          ? "whitespace-pre-wrap bg-brand-solid text-on-brand"
                          : "border border-line bg-surface",
                      )}
                    >
                      {m.role === "user" ? m.content : <Markdown>{m.content}</Markdown>}
                    </div>
                  </div>
                ))}
              </div>
            </section>

            <section className="flex min-h-0 flex-col">
              <div className="flex items-center justify-between border-b border-line px-4 py-3">
                <div className="text-sm font-semibold">执行过程</div>
                {live ? (
                  <Badge tone="bad">
                    <LiveDot /> 实时
                  </Badge>
                ) : (
                  <span className="text-[11px] text-subtle">{detail?.events.length ?? 0} 个步骤</span>
                )}
              </div>
              <div ref={timelineRef} className="min-h-0 flex-1 overflow-y-auto p-4">
                {detail ? <ReasoningTimeline steps={detail.events} /> : <Skeleton className="h-40" />}
              </div>
            </section>
          </div>
        )}
      </div>
    </div>
  );
}

function mergeStep(events: StepEvent[], step: StepEvent): StepEvent[] {
  if (events.some((e) => e.id === step.id)) return events;
  return [...events, step];
}
