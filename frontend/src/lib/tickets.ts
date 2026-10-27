import type { Tone } from "./outcome";
import type { TicketDetail, TicketStatus } from "../types";

export const TICKET_STATUS: Record<TicketStatus, { label: string; tone: Tone }> = {
  OPEN: { label: "申请已提交", tone: "info" },
  WAITING_INFO: { label: "待客户补充", tone: "warn" },
  PENDING_REVIEW: { label: "待人工审批", tone: "warn" },
  APPROVED: { label: "已批准，待办理", tone: "info" },
  FAILED_RETRYABLE: { label: "办理异常，可重试", tone: "bad" },
  COMPLETED: { label: "模拟受理完成", tone: "ok" },
  REJECTED: { label: "申请已拒绝", tone: "neutral" },
  FAILED_TERMINAL: { label: "需人工处理", tone: "bad" },
  CANCELLED: { label: "申请已取消", tone: "neutral" },
};

export const TICKET_GUIDANCE: Record<TicketStatus, string> = {
  OPEN: "申请已经保存，系统将核验材料。如果长时间未更新，可点击“继续办理”。",
  WAITING_INFO: "正在等待客户补齐问题描述、换货原因或更换要求。客户提交后，系统会重新核验。",
  PENDING_REVIEW: "请先核对订单、申请材料和知识依据，再批准或拒绝申请。批准后系统会尝试生成模拟受理单。",
  APPROVED: "审批已经通过，还没有生成受理单。点击“继续办理”，会复核订单并尝试完成模拟受理。",
  FAILED_RETRYABLE: "上次办理遇到临时异常。点击“重试办理”会继续原申请，已有材料和审批结果保留。",
  COMPLETED: "模拟受理单已经生成，本次申请已结束。当前演示不安排实际发货。",
  REJECTED: "申请已被拒绝，处理结束。审批意见和记录保留在下方。",
  FAILED_TERMINAL: "自动办理已经停止，需要人工查看异常原因并另行处理。",
  CANCELLED: "客户已经取消申请，处理结束。",
};

export function resumeLabel(status: TicketStatus): string | null {
  return status === "FAILED_RETRYABLE" ? "重试办理" : ["OPEN", "APPROVED"].includes(status) ? "继续办理" : null;
}

export function orderStatusLabel(status: string): string {
  return ({ delivered: "已送达", shipped: "运输中", pending: "待处理", processing: "处理中", cancelled: "已取消" } as Record<string, string>)[status] ?? "待核实";
}

export function ticketEventText(event: TicketDetail["events"][number]): { title: string; description: string; actor: string } {
  const p = event.payload_json;
  const state = TICKET_STATUS[p.status as TicketStatus]?.label;
  const fields: Record<string, string> = { description: "问题描述", reason_type: "换货原因", requested_change: "更换要求" };
  const missing = Array.isArray(p.missing_fields) ? p.missing_fields.map((f) => fields[String(f)] ?? "申请材料").join("、") : "";
  const rows: Record<string, [string, string]> = {
    CREATED: ["客户提交换货申请", missing ? `还需补充：${missing}。` : `申请已保存${state ? `，${state}` : ""}。`],
    SUPPLEMENTED: ["客户补充申请材料", "已保存补充内容，系统会重新核验申请。"],
    CANCELLED: ["客户取消申请", "这次换货申请已结束。"],
    DECIDED: [p.decision === "approved" ? "管理员批准申请" : p.decision === "rejected" ? "管理员拒绝申请" : "管理员完成审批", p.decision === "approved" ? "审批通过，接下来由系统复核并生成模拟受理单。" : "审批结果已保存，可查看审批意见。"],
    COMPLETED: ["系统生成模拟受理单", p.reference_code ? `受理单号：${String(p.reference_code)}` : "模拟受理已完成。"],
    RESUME_REQUESTED: ["管理员发起继续办理", ["COMPLETED", "CANCELLED", "REJECTED"].includes(String(p.status)) ? "请求时工单已经结束，只核对已有结果，没有再次办理换货。" : "请求系统检查已保存的材料和审批结果，继续未完成的处理。"],
    VALIDATED: ["系统完成材料核验", state ? `核验后状态：${state}。` : "已根据订单与业务规则核验。"],
    EXECUTION_FAILED: ["受理时遇到异常", state ? `当前状态：${state}，请查看异常说明。` : "请查看异常说明后处理。"],
    POLICY_REJECTED: ["申请未通过业务规则", "系统核验后终止申请，请查看工单说明。"],
    REFUND_CANCELLED: ["退款后终止换货申请", "订单已退款，这次换货申请随之结束。"],
  };
  const [title, description] = rows[event.kind] ?? ["工单记录更新", state ? `当前状态：${state}。` : "详细数据保留在技术详情中。"];
  return { title, description, actor: ({ customer: "客户", admin: "管理员", system: "系统" } as Record<string, string>)[event.actor_type] ?? "系统" };
}
