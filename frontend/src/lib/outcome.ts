import type { Outcome, StepEvent } from "../types";

// 表示无权访问订单的工具错误；故意将未知 ID 与他人订单作同样处理。
const BLOCKING_ERRORS = new Set(["ownership_mismatch", "identity_not_verified", "order_not_found"]);

/** 汇总一轮智能体对话中确定性规则引擎的处理结果。 */
export function outcomeOf(steps: StepEvent[]): Outcome | null {
  let decided: Outcome | null = null; // issue_refund / escalate_to_human 返回的实际决策。
  let evaluated: Outcome | null = null; // check_refund_eligibility 返回的资格判断。
  let blocked = false;
  for (const s of steps) {
    const r = s.payload?.result;
    if (s.step_type === "decision" && r?.decision) decided = r.decision as Outcome;
    else if (s.step_type === "policy_eval" && r?.decision) evaluated = r.decision as Outcome;
    // 规则拒绝将订单转人工；若后续存在实际决策，以实际决策为准。
    else if (s.step_type === "decision" && r?.error === "escalation_not_allowed") evaluated ??= "denied";
    if (BLOCKING_ERRORS.has(r?.error)) blocked = true;
  }
  if (decided) return decided;
  if (blocked) return "blocked";
  // 资格检查已判定拒绝或需要转人工，智能体因此没有直接办理退款。
  if (evaluated === "denied" || evaluated === "escalated") return evaluated;
  if (steps.some((s) => s.step_type === "injection_flag")) return "flagged";
  return null;
}

export const OUTCOME_META: Record<Outcome, { label: string; tone: Tone }> = {
  approved: { label: "退款已批准", tone: "ok" },
  denied: { label: "退款已拒绝", tone: "bad" },
  escalated: { label: "已转人工审核", tone: "warn" },
  blocked: { label: "访问已拦截", tone: "bad" },
  flagged: { label: "已标记可疑注入", tone: "warn" },
};

export type Tone = "ok" | "bad" | "warn" | "info" | "brand" | "neutral";

export const money = (n: number) =>
  `${n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} 美元`;
