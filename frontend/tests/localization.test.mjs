import assert from "node:assert/strict";
import test from "node:test";
import { build } from "esbuild";

// 使用 Vite 已依赖的打包器加载 TS，避免为少量格式与错误处理测试增加测试框架。
globalThis.window = { addEventListener() {} };
async function load(entry) {
  const result = await build({
    entryPoints: [entry], bundle: true, write: false, platform: "node", format: "esm",
    define: { "import.meta.env": "{}" },
  });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString("base64")}`);
}
const locale = await load("src/lib/locale.ts");
const { money, OUTCOME_META, outcomeOf } = await load("src/lib/outcome.ts");
const { ApiError, friendlyError } = await load("src/api.ts");
const { resumeLabel, ticketEventText } = await load("src/lib/tickets.ts");
const { historyKey, restoreHistory } = await load("src/lib/chatHistory.ts");

test("终态和等待材料或审批时不提供恢复按钮，异常办理保留重试入口", () => {
  for (const status of ["COMPLETED", "CANCELLED", "REJECTED", "WAITING_INFO", "PENDING_REVIEW", "FAILED_TERMINAL"]) {
    assert.equal(resumeLabel(status), null);
  }
  assert.equal(resumeLabel("APPROVED"), "继续办理");
  assert.equal(resumeLabel("FAILED_RETRYABLE"), "重试办理");
  const event = ticketEventText({ kind: "RESUME_REQUESTED", actor_type: "admin", payload_json: { status: "COMPLETED" } });
  assert.match(event.description, /没有再次办理/);
  assert.equal(event.actor, "管理员");
});

test("不同客户的会话指针分开，恢复最后一轮的步骤和真实检索来源", () => {
  const token = (sub) => `header.${Buffer.from(JSON.stringify({ sub })).toString("base64url")}.signature`;
  assert.notEqual(historyKey(token("customer-a")), historyKey(token("customer-b")));
  assert.equal(historyKey("invalid"), null);
  const source = { document_id: "HP01", excerpt: "连接排查", revision: "1" };
  const saved = restoreHistory({ messages: [
    { id: 1, role: "user", content: "旧问题", created_at: "2026-10-23T01:00:00Z" },
    { id: 2, role: "user", content: "新问题", created_at: "2026-10-23T01:01:00Z" },
  ], events: [
    { message_id: 1, step_type: "tool_result", payload: { tool: "search_knowledge", result: { ok: true, data: { sources: [{ document_id: "OLD" }] } } } },
    { message_id: 2, step_type: "tool_result", payload: { tool: "search_knowledge", result: { ok: true, data: { sources: [source] } } } },
  ] });
  assert.equal(saved.messages.length, 2);
  assert.equal(saved.steps.length, 1);
  assert.deepEqual(saved.sources, [source]);
});

test("新 MCP 与历史退款结果按实际办理决定展示，资格通过和未知结果不显示成功", () => {
  const step = (step_type, result) => ({ step_type, payload: { result } });
  assert.equal(outcomeOf([step("decision", { ok: true, data: { decision: "approved" } })]), "approved");
  assert.equal(outcomeOf([step("decision", { decision: "approved" })]), "approved");
  assert.equal(outcomeOf([step("policy_eval", { ok: true, data: { decision: "approved" } })]), null);
  assert.equal(outcomeOf([step("decision", { ok: false, error: { code: "OUTCOME_UNKNOWN" } })]), null);
  assert.equal(outcomeOf([step("tool_result", { ok: false, error: { code: "NOT_FOUND" } })]), "blocked");
});

test("UTC 和无时区时间均按北京时间显示，不随执行机器时区漂移", () => {
  assert.equal(locale.formatTime("2026-10-15T00:05:00Z"), "08:05");
  assert.equal(locale.formatTime("2026-10-15T00:05:00"), "08:05");
  assert.equal(locale.formatTime("2026-10-15T08:05:00+08:00"), "08:05");
  assert.equal(locale.formatDate("2026-10-14T20:00:00Z"), "2026/10/15");
});

test("金额显示保留美元含义与两位小数", () => {
  assert.equal(money(500), "500.00 美元");
  assert.equal(money(1299.5), "1,299.50 美元");
});

test("内部工具名和状态值通过中文标签展示", () => {
  assert.equal(locale.toolLabel("issue_refund"), "执行退款");
  assert.equal(OUTCOME_META.approved.label, "退款已批准");
  assert.equal(locale.ERROR_LABELS.order_not_found, "未找到当前账户下的订单");
});

test("未知服务异常不会把内部细节泄露给用户", () => {
  for (const error of [new Error("secret-api-key"), new ApiError(500, "secret-database")]) {
    assert.equal(friendlyError(error), "服务暂时出现异常，请稍后重试。");
  }
  assert.equal(friendlyError(new ApiError(422, "internal validation details")), "输入格式不正确，请检查后重试。");
});

test("新旧后端的限流提示都提供中文说明", () => {
  assert.equal(friendlyError(new ApiError(429, "会话已达上限，请新建对话。")), "会话已达上限，请新建对话。");
  assert.match(friendlyError(new ApiError(429, "Rate limit exceeded")), /请求过于频繁/);
  assert.match(friendlyError(new ApiError(503, "upstream unavailable", 8)), /8 秒/);
});
