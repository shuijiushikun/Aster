import type { Order, Outcome } from "../types";

export interface ScenarioCard { key: string; title: string; blurb: string; prompt: string; expect: Outcome | "info" }

export const GENERIC_PROMPTS: ScenarioCard[] = [
  { key: "orders", title: "查询我的订单", blurb: "查看订单信息与售后进度", prompt: "请帮我查询我的订单和当前售后进度。", expect: "info" },
  { key: "help", title: "商品使用与排障", blurb: "根据商品查找处理建议", prompt: "我想了解商品的使用与故障排查方法，请先帮我确认订单中的商品。", expect: "info" },
  { key: "exchange", title: "申请换货", blurb: "说明问题，协助核验申请", prompt: "我想申请换货，请帮我查看订单并说明所需材料。", expect: "info" },
];

export function scenarioCards(orders: Order[]): ScenarioCard[] {
  const order = orders.find((o) => !o.refunded) ?? orders[0];
  if (!order) return GENERIC_PROMPTS;
  return GENERIC_PROMPTS.map((card) => ({ ...card, prompt: card.key === "orders" ? `请查询订单 ${order.id} 的情况和售后进度。` : card.key === "help" ? `订单 ${order.id} 的${order.product_name}遇到了问题，请帮我排查。` : `我想为订单 ${order.id} 申请换货，请协助核验并说明所需材料。` }));
}
