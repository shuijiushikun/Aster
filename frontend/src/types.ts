export interface StepEvent {
  kind: "step";
  id: number;
  seq: number;
  step_type:
    | "model"
    | "tool_call"
    | "tool_result"
    | "policy_eval"
    | "decision"
    | "injection_flag"
    | "usage"
    | "budget_exhausted"
    | "output_correction"
    | "notice"
    | "trace"
    | "error";
  node: string;
  payload: Record<string, any>;
  created_at: string;
}

export interface MessageEvent {
  kind: "message";
  role: "user" | "assistant";
  content: string;
}

export interface ConversationEvent {
  kind: "conversation";
  conversation_id: string;
}

export interface DoneEvent {
  kind: "done";
}

export interface ErrorEvent {
  kind: "error";
  message: string;
}

export interface KnowledgeSource {
  document_id: string;
  title?: string;
  revision: string;
  chunk_index?: number;
  excerpt: string;
  references?: string[];
  source_path?: string;
}

export type DomainEvent =
  | { kind: "knowledge_sources"; sources: KnowledgeSource[] }
  | { kind: "ticket_update"; ticket_id: string; status: TicketStatus; order_id?: string; reference_code?: string }
  | { kind: "workflow_status"; ticket_id: string; state?: string; status?: string; phase?: string };

export type AgentEvent =
  | StepEvent
  | MessageEvent
  | ConversationEvent
  | DoneEvent
  | ErrorEvent
  | DomainEvent;

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  at: number;
  /** 此条助手消息所结束的对话轮次结果，参见 outcomeOf。 */
  outcome?: Outcome | null;
  error?: boolean;
}

export type Outcome = "approved" | "denied" | "escalated" | "blocked" | "flagged";

export interface ConversationSummary {
  id: string;
  customer_id: string | null;
  customer_name: string | null;
  created_at: string;
  message_count: number;
  last_message: string | null;
}

export interface ConversationDetail {
  id: string;
  customer_id: string | null;
  customer_name: string | null;
  messages: { id: number; role: string; content: string; created_at: string }[];
  events: StepEvent[];
}

export type Scenario =
  | "refundable"
  | "final_sale"
  | "high_value"
  | "out_of_window"
  | "already_refunded";

export interface Order {
  id: string;
  product_name: string;
  amount: number;
  status: string;
  order_date: string | null;
  delivered_date: string | null;
  is_final_sale: boolean;
  refunded: boolean;
  scenario: Scenario | null;
}

export interface DemoSession {
  customer: { id: string; name: string; email: string };
  orders: Order[];
  token: string;
  admin_token: string;
  expires_in: number;
  data_ttl_hours: number;
}

export interface Meta {
  auth_mode: "dev" | "jwt" | "demo";
  model: string;
  /** 主模型 ID，例如 "gpt-5.6-sol-medium"；使用脚本模型时为 null。 */
  model_name?: string | null;
  demo: {
    session_ttl_s: number;
    data_ttl_hours: number;
    /** 沙箱重置后，聊天归档继续保留的时长。 */
    transcript_retention_days?: number;
  } | null;
}

export interface AdminStats {
  conversations: number;
  messages: number;
  decisions: Record<string, number>;
  tokens_used: number;
}

export type TicketStatus =
  | "OPEN"
  | "WAITING_INFO"
  | "PENDING_REVIEW"
  | "APPROVED"
  | "FAILED_RETRYABLE"
  | "COMPLETED"
  | "REJECTED"
  | "FAILED_TERMINAL"
  | "CANCELLED";

export interface TicketSummary {
  id: string;
  order_id: string;
  type: string;
  status: TicketStatus;
  description: string;
  created_at: string;
  updated_at: string;
  last_error_code: string | null;
}

export interface TicketDetail extends TicketSummary {
  reason_type: string | null;
  requested_change: string | null;
  evidence_refs: {
    document_id: string;
    revision: string;
    chunk_index: number;
    excerpt: string;
  }[];
  reference_code: string | null;
  events: {
    id: number;
    kind: string;
    actor_type: string;
    payload_json: Record<string, unknown>;
    created_at: string;
  }[];
}

export interface AdminTicketDetail extends TicketDetail {
  customer_id: string;
  order: {
    id: string;
    product_id: string | null;
    product_name: string;
    status: string;
    refunded: boolean;
    is_final_sale: boolean;
    delivered_date: string | null;
  };
  approval: {
    decision: string;
    admin_subject: string;
    comment: string;
    created_at: string;
  } | null;
  last_error_message: string | null;
}

export interface TicketSupplementRequest {
  request_id: string;
  description: string;
  reason_type?: "size" | "defect";
  requested_change?: string;
}

export interface TicketDecisionRequest {
  request_id: string;
  decision: "approved" | "rejected";
  comment: string;
}

export interface TicketAccepted {
  ticket_id: string;
  status: TicketStatus;
  request_id: string;
}
