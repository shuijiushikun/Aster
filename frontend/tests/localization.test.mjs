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
const { money, OUTCOME_META } = await load("src/lib/outcome.ts");
const { ApiError, friendlyError } = await load("src/api.ts");

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
