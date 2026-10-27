/** 仅本地化展示；协议字段、数据库状态和业务币种始终保持原值。 */
export const LOCALE = "zh-CN";
export const DISPLAY_TIME_ZONE = "Asia/Shanghai";

// SQLite 返回的无时区时间按后端约定视为 UTC，避免浏览器误认为本地时间。
export function parseDate(value: string | number): Date {
  if (typeof value === "number") return new Date(value);
  return new Date(/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/i.test(value)
    ? value
    : /^\d{4}-\d{2}-\d{2}T/.test(value) ? `${value}Z` : value);
}

export const formatTime = (value: string | number) =>
  parseDate(value).toLocaleTimeString(LOCALE, {
    timeZone: DISPLAY_TIME_ZONE, hour: "2-digit", minute: "2-digit", hour12: false,
  });

export const formatDate = (value: string) =>
  parseDate(value).toLocaleDateString(LOCALE, { timeZone: DISPLAY_TIME_ZONE });

export const TOOL_LABELS: Record<string, string> = {
  get_my_profile: "查询客户资料",
  list_orders: "查询订单列表",
  list_my_orders: "查询订单列表",
  get_order: "查询订单详情",
  check_refund_eligibility: "校验退款资格",
  issue_refund: "执行退款",
  escalate_to_human: "转交人工审核",
  get_product: "查询商品信息",
  get_exchange_policy_facts: "查询换货条件",
  search_knowledge: "检索售后资料",
  create_exchange_request: "提交换货申请",
  get_tickets: "查询售后工单",
};

export const toolLabel = (name: string) => TOOL_LABELS[name] ?? "调用工具";

export const ERROR_LABELS: Record<string, string> = {
  order_not_found: "未找到当前账户下的订单",
  ownership_mismatch: "无法访问该订单",
  identity_not_verified: "尚未验证登录身份",
  escalation_not_allowed: "该订单不符合转人工条件",
};

export const PATTERN_LABELS: Record<string, string> = {
  override_policy: "要求忽略规则",
  force_approval: "强制批准退款",
  authority_claim: "声称拥有管理权限",
  role_override: "要求切换角色",
  bypass: "要求跳过校验",
  exfiltration: "试图获取内部指令",
  zero_width_chars: "包含不可见字符",
  base64_payload: "包含可疑编码内容",
  instructions_in_tool_output: "工具数据包含可疑指令",
};

export const patternLabel = (name: string) =>
  PATTERN_LABELS[name] ?? (name.startsWith("fuzzy:") ? "疑似混淆指令" : "可疑指令");
