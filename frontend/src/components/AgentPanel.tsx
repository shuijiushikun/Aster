import { Activity } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { OUTCOME_META, outcomeOf } from "../lib/outcome";
import type { StepEvent } from "../types";
import ReasoningTimeline from "./ReasoningTimeline";
import { Badge, LiveDot } from "./ui";

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg bg-surface-2 px-2.5 py-2">
      <div className="text-xs font-medium text-subtle">{label}</div>
      <div className="mt-0.5 font-mono text-sm font-semibold tabular-nums">{value}</div>
    </div>
  );
}

/** 实时展示当前或上一轮智能体对话的统计数据和步骤时间线。 */
export default function AgentPanel({
  steps,
  streaming,
  startedAt,
  endedAt,
  showTitle = true,
}: {
  steps: StepEvent[];
  streaming: boolean;
  startedAt: number | null;
  endedAt: number | null;
  showTitle?: boolean;
}) {
  const [now, setNow] = useState(Date.now());
  const timelineRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!streaming) return;
    const t = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(t);
  }, [streaming]);

  useEffect(() => {
    const el = timelineRef.current;
    el?.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [steps.length]);

  const usage = steps.filter((s) => s.step_type === "usage");
  const unknownUsage = usage.some((s) => s.payload.status === "unknown" || s.payload.input_tokens == null || s.payload.output_tokens == null);
  const tokens = usage
    .reduce((n, s) => n + (s.payload.input_tokens ?? 0) + (s.payload.output_tokens ?? 0), 0);
  const tools = steps.filter((s) => s.step_type === "tool_call").length;
  const elapsed = startedAt ? ((streaming ? now : endedAt ?? now) - startedAt) / 1000 : 0;
  const outcome = outcomeOf(steps);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="space-y-3 border-b border-line p-4">
        <div className="flex items-center justify-between">
          {showTitle ? (
            <div className="flex items-center gap-2 text-sm font-semibold">
              <Activity size={16} className="text-brand" /> 智能体执行过程
            </div>
          ) : (
            <span className="text-sm text-subtle">最近一轮</span>
          )}
          {streaming ? (
            <span className="flex items-center gap-1.5 text-xs font-semibold text-bad">
              <LiveDot /> 实时
            </span>
          ) : outcome ? (
            <Badge tone={OUTCOME_META[outcome].tone}>{OUTCOME_META[outcome].label}</Badge>
          ) : null}
        </div>
        <div className="grid grid-cols-3 gap-2">
          <Stat label="工具调用" value={tools} />
          <Stat label="token 用量" value={!usage.length ? "未记录" : unknownUsage ? `${tokens.toLocaleString("zh-CN")} + 未知` : tokens.toLocaleString("zh-CN")} />
          <Stat label="耗时" value={`${elapsed.toFixed(1)} 秒`} />
        </div>
      </div>
      <div ref={timelineRef} className="min-h-0 flex-1 overflow-y-auto p-4">
        {steps.length === 0 && !streaming ? (
          <div className="px-2 py-10 text-center">
            <p className="text-sm font-medium">暂无执行记录</p>
            <p className="mt-1 text-sm text-subtle">
              发送消息后，这里会实时展示工具调用、规则校验和处理结果。
            </p>
          </div>
        ) : (
          <ReasoningTimeline steps={steps} hideUsage />
        )}
      </div>
    </div>
  );
}
