import clsx from "clsx";
import {
  AlertTriangle,
  Brain,
  CircleDollarSign,
  ExternalLink,
  Gauge,
  Info,
  ShieldAlert,
  ShieldCheck,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import type { Outcome, StepEvent } from "../types";
import { OUTCOME_META, money, type Tone } from "../lib/outcome";
import { ERROR_LABELS, patternLabel, toolLabel } from "../lib/locale";
import Markdown from "./Markdown";
import { Badge } from "./ui";

const STEP_META: Record<string, { label: string; icon: LucideIcon; tone: Tone }> = {
  model: { label: "模型回复", icon: Brain, tone: "neutral" },
  tool_call: { label: "工具调用", icon: Wrench, tone: "info" },
  tool_result: { label: "工具结果", icon: Wrench, tone: "neutral" },
  policy_eval: { label: "规则校验", icon: ShieldCheck, tone: "warn" },
  decision: { label: "处理结果", icon: CircleDollarSign, tone: "ok" },
  injection_flag: { label: "疑似指令注入", icon: ShieldAlert, tone: "bad" },
  error: { label: "发生错误", icon: AlertTriangle, tone: "bad" },
  usage: { label: "词元用量", icon: Gauge, tone: "neutral" },
  budget_exhausted: { label: "用量已达上限", icon: Gauge, tone: "warn" },
  output_correction: { label: "回复已纠正", icon: ShieldAlert, tone: "bad" },
  notice: { label: "系统通知", icon: Info, tone: "info" },
  trace: { label: "调用追踪", icon: ExternalLink, tone: "brand" },
};

// 用量与追踪链接只在管理端展示，客户面板聚焦业务执行步骤。
const INTERNAL_STEPS = new Set(["usage", "trace"]);

const DOT: Record<Tone, string> = {
  ok: "bg-ok/15 text-ok",
  bad: "bg-bad/15 text-bad",
  warn: "bg-warn/15 text-warn",
  info: "bg-info/15 text-info",
  brand: "bg-brand/15 text-brand",
  neutral: "bg-surface-2 text-muted",
};

function Json({ value, label = "技术详情" }: { value: unknown; label?: string }) {
  return (
    <details className="group mt-1.5">
      <summary className="cursor-pointer select-none text-[11px] font-medium text-subtle hover:text-muted">
        {label}
      </summary>
      <pre className="mt-1 max-h-56 overflow-auto rounded-md bg-surface-2 p-2 font-mono text-[11px] leading-relaxed text-muted">
        {JSON.stringify(value, null, 2)}
      </pre>
    </details>
  );
}

export function DecisionBadge({ decision }: { decision?: string }) {
  if (!decision) return null;
  const meta = OUTCOME_META[decision as Outcome];
  return <Badge tone={meta?.tone ?? "neutral"}>{meta?.label ?? "未知结果"}</Badge>;
}

function argsSummary(args: Record<string, unknown> | undefined): string {
  if (!args) return "";
  return Object.entries(args)
    .map(([k, v]) => `${({ order_id: "订单号", reason: "说明" } as Record<string, string>)[k] ?? k}：${String(v)}`)
    .join(", ");
}

function StepBody({ step }: { step: StepEvent }) {
  const p = step.payload || {};
  switch (step.step_type) {
    case "model":
      return (
        <div className="text-[13px] text-fg/90">
          <Markdown>{p.text ?? ""}</Markdown>
        </div>
      );
    case "tool_call":
      return (
        <div className="break-words text-xs leading-5 text-info">
          {toolLabel(p.tool)}{p.args && Object.keys(p.args).length > 0 ? `（${argsSummary(p.args)}）` : ""}
          <Json value={{ tool: p.tool, args: p.args }} />
        </div>
      );
    case "policy_eval":
    case "decision": {
      const r = p.result || {};
      return (
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-muted">{toolLabel(p.tool)}</span>
            {step.step_type === "policy_eval" ? (
              <Badge tone={r.eligible ? "info" : "bad"}>
                {r.requires_escalation ? "需人工审核" : r.eligible ? "符合退款条件" : "不符合退款条件"}
              </Badge>
            ) : <DecisionBadge decision={r.decision} />}
            {typeof r.amount === "number" && (
              <span className="text-xs text-muted">{money(r.amount)}</span>
            )}
          </div>
          {Array.isArray(r.reasons) && r.reasons.length > 0 && (
            <ul className="mt-1.5 list-disc space-y-0.5 pl-4 text-xs text-muted">
              {r.reasons.map((reason: string, i: number) => (
                <li key={i}>{reason}</li>
              ))}
            </ul>
          )}
          <Json value={{ tool: p.tool, result: r }} />
        </div>
      );
    }
    case "tool_result": {
      const r = p.result || {};
      return (
        <div>
          <span className="text-xs text-muted">{toolLabel(p.tool)}</span>
          {r.error && (
            <Badge tone="bad" className="ml-2">
              {ERROR_LABELS[String(r.error)] ?? "工具执行失败"}
            </Badge>
          )}
          <Json value={r} label="原始结果" />
        </div>
      );
    }
    case "injection_flag":
      return (
        <p className="text-xs text-bad">
          检测到：{(p.patterns || []).map(patternLabel).join("、")}。处理结果仍由退款规则决定。
        </p>
      );
    case "usage":
      return (
        <p className="font-mono text-[11px] text-subtle">
          {p.status === "unknown" ? "本次调用用量未知" : <>输入 {p.input_tokens ?? "未知"} · 输出 {p.output_tokens ?? "未知"}</>}
          {p.model ? ` · ${p.model}` : ""}
        </p>
      );
    case "notice":
      return <p className="text-xs text-info">{p.message}</p>;
    case "budget_exhausted":
      return <p className="text-xs text-warn">当前会话或账户用量已达上限，请新建对话或联系人工客服。</p>;
    case "output_correction":
      return <p className="text-xs text-bad">{p.reason}</p>;
    case "error":
      return <p className="text-xs text-bad">模型调用未完成，请稍后重试。</p>;
    case "trace":
      return p.url ? (
        <a
          href={p.url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 text-xs font-medium text-brand hover:underline"
        >
          在 Opik 中查看 <ExternalLink size={12} />
        </a>
      ) : (
        <code className="font-mono text-[11px] text-subtle">{p.trace_id}</code>
      );
    default:
      return <Json value={p} />;
  }
}

export default function ReasoningTimeline({
  steps,
  hideUsage = false,
}: {
  steps: StepEvent[];
  hideUsage?: boolean;
}) {
  const shown = hideUsage ? steps.filter((s) => !INTERNAL_STEPS.has(s.step_type)) : steps;
  if (shown.length === 0) {
    return <p className="px-1 py-8 text-center text-sm text-subtle">暂无执行步骤。</p>;
  }
  return (
    <ol className="relative space-y-3 before:absolute before:bottom-2 before:left-[13px] before:top-2 before:w-px before:bg-line">
      {shown.map((step) => {
        const meta = STEP_META[step.step_type] || STEP_META.tool_result;
        const Icon = meta.icon;
        return (
          <li key={`${step.id}-${step.seq}`} className="relative flex animate-fade-up gap-3">
            <span
              className={clsx(
                "relative z-10 mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full ring-4 ring-surface",
                DOT[meta.tone],
              )}
            >
              <Icon size={14} />
            </span>
            <div className="min-w-0 flex-1 pb-1">
              <div className="flex items-center justify-between gap-2">
                <span className="text-[11px] font-semibold uppercase tracking-wide text-subtle">
                  {meta.label}
                </span>
                <span className="font-mono text-[10px] text-subtle">#{step.seq}</span>
              </div>
              <div className="mt-0.5">
                <StepBody step={step} />
              </div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
