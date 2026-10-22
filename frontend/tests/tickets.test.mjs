import assert from "node:assert/strict";
import test from "node:test";
import { build } from "esbuild";

globalThis.window = { addEventListener() {}, ACME_SUPPORT_TOKEN: "customer-test", ACME_ADMIN_TOKEN: "admin-test" };
const bundle = await build({ entryPoints: ["src/api.ts"], bundle: true, write: false, platform: "node", format: "esm", define: { "import.meta.env": "{}" } });
const api = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);
const originalFetch = globalThis.fetch;

test("客户补充与同体重试保留请求编号、客户身份和取消信号", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, ...init });
    return new Response(JSON.stringify({ ticket_id: "t1", status: "WAITING_INFO", request_id: "same-request" }), { status: 202 });
  };
  try {
    const body = { request_id: "same-request", description: "耳机无法开机", reason_type: "defect", requested_change: "同型号" };
    const controller = new AbortController();
    const receipt = await api.supplementMyTicket("ticket/1", body, controller.signal);
    await api.supplementMyTicket("ticket/1", body, controller.signal);
    assert.equal(calls[0].url, "/api/me/tickets/ticket%2F1/supplement");
    assert.equal(calls[0].headers.get("Authorization"), "Bearer customer-test");
    assert.equal(calls[0].signal, controller.signal);
    assert.equal(calls[0].body, calls[1].body);
    assert.deepEqual(JSON.parse(calls[0].body), body);
    assert.equal(receipt.status, "WAITING_INFO");
  } finally { globalThis.fetch = originalFetch; }
});

test("工单结构化业务错误保留中文信息和 HTTP 冲突状态", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: { code: "ticket_state_conflict", message: "工单状态已改变，请刷新后重试。" } }), { status: 409 });
  try {
    await assert.rejects(api.cancelMyTicket("t1", { request_id: "cancel-1" }), (err) => err instanceof api.ApiError && err.status === 409 && err.message === "工单状态已改变，请刷新后重试。");
  } finally { globalThis.fetch = originalFetch; }
});

test("客户详情和管理员详情使用独立身份与服务端权限边界", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => { calls.push({ url, token: init.headers.get("Authorization") }); return new Response("{}"); };
  try {
    await api.fetchMyTicket("t1");
    await api.fetchAdminTicket("t1");
    assert.deepEqual(calls, [
      { url: "/api/me/tickets/t1", token: "Bearer customer-test" },
      { url: "/api/admin/tickets/t1", token: "Bearer admin-test" },
    ]);
  } finally { globalThis.fetch = originalFetch; }
});
