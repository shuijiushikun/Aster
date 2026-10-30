"""按真实提示词入口验证辅助数据、连续历史和检索证据的预算优先级。"""

import copy
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent import context
from app.agent.answer_grounding import ground_answer
from app.agent.runtime import ModelCalls
from app.config import get_settings
from app.mcp.schemas import KnowledgeSearch, ReadResult
from tests.test_memory_continuity import MemoryModel, history


def source(number=1, detail=""):
    return {"document_id": f"KB-{number}", "revision": "1", "chunk_index": 0,
            "title": f"资料{number}", "model_code": "HP-01", "category": "digital",
            "excerpt": f"HP-01续航为8小时。{detail}", "score": 1 / number,
            "score_kind": "rerank", "source_path": "manual.md", "references": []}


def retrieval_pair(sources):
    request = AIMessage("", tool_calls=[{"name": "search_knowledge", "args": {"query": "续航"}, "id": "kb-call"}])
    result = {"ok": True, "data": {"status": "ok", "query": "续航", "model_code": "HP-01",
              "sources": sources, "warnings": [], "evidence_token": None}, "error": None}
    reply = ToolMessage(json.dumps(result, ensure_ascii=False), name="search_knowledge", tool_call_id="kb-call")
    return request, reply


def set_limit(monkeypatch, limit):
    settings = get_settings()
    monkeypatch.setattr(settings, "context_max_tokens", limit + settings.max_output_tokens + settings.context_token_margin)


class AnswerModel:
    def __init__(self, number=1, *, missing=False, quote="HP-01续航为8小时。"):
        self.number, self.missing, self.quote = number, missing, quote
        self.messages = []

    async def ainvoke(self, messages):
        self.messages.append(list(messages))
        claims = [] if self.missing else [{"document_id": f"KB-{self.number}", "revision": "1", "chunk_index": 0,
                                         "quote": self.quote, "text": "HP-01的续航为8小时。"}]
        return AIMessage(json.dumps({"claims": claims, "missing": ["续航"] if self.missing else []}, ensure_ascii=False))


def test_uncovered_history_is_protected_even_without_database_metadata():
    with pytest.raises(context.ContextBudgetExceeded):
        context.build_context([HumanMessage("重要历史 " * 1000), AIMessage("待办事项"), HumanMessage("继续")],
                              "规则", max_tokens=2200)


def test_catalog_labels_can_be_removed_but_ticket_description_and_request_remain(monkeypatch):
    facts = {"owner_customer_id": "CUST-1", "related_tickets": [{"id": "TKT-1", "status": "WAITING_INFO",
             "description": "左耳没有声音", "requested_change": "换同型号黑色"}],
             "catalog_products": [{"model_code": "HP-01", "category": "digital", "name": "商品名称 " * 1000}]}
    messages = context.build_context([HumanMessage("继续")], "规则", facts=facts,
        memory={"customer_id": "CUST-1", "conversation_id": "CID", "current_message_id": 1})
    set_limit(monkeypatch, 500)
    result = context.fit_messages(messages)
    data = context.context_data(result)["facts"]
    assert data["related_tickets"] == facts["related_tickets"]
    assert data["catalog_products"] == [{"model_code": "HP-01", "category": "digital"}]
    assert context.context_data(messages)["facts"] == facts


def test_real_retrieval_tool_excerpt_is_limited_without_changing_canonical_result(monkeypatch):
    monkeypatch.setattr(get_settings(), "context_evidence_max_tokens", 25)
    request, reply = retrieval_pair([source(detail="原文😀 " * 200)])
    original = reply.content
    result = context.fit_messages([HumanMessage("续航多少"), request, reply])
    payload = json.loads(result[-1].content)
    excerpt = payload["data"]["sources"][0]["excerpt"]
    assert context.context_tokens([HumanMessage(excerpt)]) < 60
    assert source(detail="原文😀 " * 200)["excerpt"].startswith(excerpt)
    assert "context_excerpt_truncated" in payload["data"]["warnings"]
    assert reply.content == original
    assert result[-2] == request and result[-1].tool_call_id == "kb-call"


def test_real_retrieval_tool_drops_lower_ranked_sources_and_keeps_json_and_pair(monkeypatch):
    monkeypatch.setattr(get_settings(), "context_evidence_max_tokens", 4000)
    sources = [source(i, "document details " * 350) for i in range(1, 4)]
    request, reply = retrieval_pair(sources)
    messages = [SystemMessage("规则"), HumanMessage("续航多少"), request, reply]
    _, single = retrieval_pair(sources[:1])
    set_limit(monkeypatch, context.context_tokens(messages[:-1] + [single]) + 80)
    result = context.fit_messages(messages)
    data = json.loads(result[-1].content)["data"]
    assert [s["document_id"] for s in data["sources"]] == ["KB-1"]
    assert data["sources"][0] == sources[0]
    assert data["status"] == "ok" and "context_sources_omitted" in data["warnings"]
    assert result[:-1] == messages[:-1]
    assert len(json.loads(reply.content)["data"]["sources"]) == 3
    assert context.context_tokens(result) <= context.input_token_limit()
    assert ReadResult[KnowledgeSearch].model_validate_json(result[-1].content).ok


def test_best_retrieval_source_and_business_tool_result_are_required(monkeypatch):
    monkeypatch.setattr(get_settings(), "context_evidence_max_tokens", 4000)
    request, reply = retrieval_pair([source(detail="document details " * 350)])
    set_limit(monkeypatch, 400)
    with pytest.raises(context.ContextBudgetExceeded):
        context.fit_messages([HumanMessage("问题"), request, reply])
    business = ToolMessage('{"decision":"approved","refund_recorded":true,"amount":80}',
                           name="issue_refund", tool_call_id="refund")
    with pytest.raises(context.ContextBudgetExceeded):
        context.fit_messages([HumanMessage("问题" * 500), business])


async def test_grounding_request_obeys_budget_on_its_real_payload(monkeypatch):
    monkeypatch.setattr(get_settings(), "context_evidence_max_tokens", 4000)
    set_limit(monkeypatch, 3000)
    model = AnswerModel()
    evidence = {"status": "ok", "sources": [source(i, "document details " * 700) for i in range(1, 4)]}
    original = copy.deepcopy(evidence)
    result = await ground_answer("HP-01续航多少", evidence, ModelCalls(model), {})
    assert result.status == "supported"
    payload = json.loads(model.messages[0][1].content)
    assert [s["document_id"] for s in payload["sources"]] == ["KB-1"]
    assert context.context_tokens(model.messages[0], response_format={"type": "json_object"}) <= context.input_token_limit()
    assert evidence == original


async def test_grounding_checks_only_sources_and_excerpts_the_model_saw(monkeypatch):
    monkeypatch.setattr(get_settings(), "context_evidence_max_tokens", 25)
    model = AnswerModel(quote="稍后才写到的续航为8小时。")
    evidence = {"sources": [source(detail="其他说明 " * 200 + "稍后才写到的续航为8小时。")], "model_code": "HP-01"}
    result = await ground_answer("续航", evidence, ModelCalls(model), {})
    assert result.status == "unavailable" and result.reason == "invalid_citation"
    assert len(model.messages) == 2
    assert "稍后才写到" not in model.messages[0][1].content


async def test_missing_answer_after_evidence_omission_reports_budget_instead_of_no_knowledge(monkeypatch):
    monkeypatch.setattr(get_settings(), "context_evidence_max_tokens", 4000)
    set_limit(monkeypatch, 3000)
    evidence = {"sources": [source(i, "document details " * 700) for i in range(1, 4)]}
    result = await ground_answer("续航", evidence, ModelCalls(AnswerModel(missing=True)), {})
    assert result.status == "unavailable" and result.reason == "context_budget"


async def test_grounding_rejects_a_citation_to_an_omitted_source(monkeypatch):
    monkeypatch.setattr(get_settings(), "context_evidence_max_tokens", 4000)
    set_limit(monkeypatch, 3000)
    model = AnswerModel(number=2)
    evidence = {"sources": [source(i, "document details " * 700) for i in range(1, 4)]}
    result = await ground_answer("续航", evidence, ModelCalls(model), {})
    assert result.status == "unavailable" and result.reason == "invalid_citation"
    assert len(model.messages) == 2
    assert all([s["document_id"] for s in json.loads(messages[1].content)["sources"]] == ["KB-1"]
               for messages in model.messages)


async def test_grounding_repair_feedback_is_budgeted_and_valid_answer_still_succeeds(monkeypatch):
    monkeypatch.setattr(get_settings(), "context_evidence_max_tokens", 4000)
    set_limit(monkeypatch, 3000)

    class Repair(AnswerModel):
        async def ainvoke(self, messages):
            self.quote = "错误引文" if not self.messages else "HP-01续航为8小时。"
            return await super().ainvoke(messages)

    model = Repair()
    result = await ground_answer("续航", {"sources": [source(i, "document details " * 700)
        for i in range(1, 4)]}, ModelCalls(model), {})
    assert result.status == "supported" and len(model.messages) == 2
    assert all(context.context_tokens(messages, response_format={"type": "json_object"})
               <= context.input_token_limit() for messages in model.messages)


async def test_grounding_keeps_the_last_dialogue_pair_when_optional_older_history_is_trimmed(monkeypatch):
    set_limit(monkeypatch, 2000)
    model = AnswerModel()
    dialogue = [{"role": "user", "content": "older context " * 1000},
                {"role": "assistant", "content": "older reply " * 1000},
                {"role": "user", "content": "HP-01的参数"},
                {"role": "assistant", "content": "您要问哪个参数？"}]
    result = await ground_answer("续航", {"sources": [source()]}, ModelCalls(model), {}, history=dialogue)
    assert result.status == "supported"
    payload = json.loads(model.messages[0][1].content)
    assert payload["history"] == dialogue[-2:]
    assert len(dialogue) == 4


async def test_grounding_mandatory_overflow_has_a_distinct_result_and_no_model_call(monkeypatch):
    set_limit(monkeypatch, 500)
    model = AnswerModel()
    result = await ground_answer("续航", {"sources": [source()]}, ModelCalls(model), {})
    assert result.status == "unavailable" and result.reason == "context_budget"
    assert not model.messages


async def memory_and_knowledge(monkeypatch):
    from app.agent.memory import load_context
    from app.agent.runner import create_conversation

    monkeypatch.setattr(get_settings(), "context_evidence_max_tokens", 4000)
    cid = await create_conversation("CUST-001")
    ids = await history(cid, 12, detail="historical details " * 20)
    ids += await history(cid, 1, start=13)
    messages = await load_context("CUST-001", cid, current_message_id=ids[-1], system_prompt="规则")
    messages += list(retrieval_pair([source(i, "document details " * 250) for i in (1, 2)]))
    # 一次摘要可以腾出空间；删除第二条证据也能腾出空间，用来验证优先顺序。
    set_limit(monkeypatch, context.context_tokens(messages) - 200)
    return cid, ids, messages


async def test_database_history_is_summarized_before_removing_retrieval_sources(engine, monkeypatch):
    _, ids, messages = await memory_and_knowledge(monkeypatch)
    model = MemoryModel()
    await ModelCalls(model).call(messages, {}, node="agent")
    assert [node for node, _ in model.calls] == ["summary", "agent"]
    prepared = model.calls[-1][1]
    assert len(json.loads(prepared[-1].content)["data"]["sources"]) == 2
    assert sum(m.id == f"db:{ids[-1]}" for m in prepared) == 1
    assert prepared[-2:] == messages[-2:]


@pytest.mark.parametrize("failure", [RuntimeError("summary unavailable"), "timeout"])
async def test_failed_summary_can_trim_evidence_while_retaining_every_uncovered_message(engine, monkeypatch, failure):
    from app.db import session as db
    from app.db.models import ConversationSummaryRecord

    cid, ids, messages = await memory_and_knowledge(monkeypatch)
    if failure == "timeout":
        monkeypatch.setattr(get_settings(), "summary_timeout_s", 0.2)
    model = MemoryModel(failure)
    await ModelCalls(model).call(messages, {}, node="agent")
    prepared = model.calls[-1][1]
    assert model.calls[-1][0] == "agent"
    assert [m.id for m in prepared if m.additional_kwargs.get("context_role") == "history"] == [
        f"db:{mid}" for mid in ids[:-1]]
    assert len(json.loads(prepared[-1].content)["data"]["sources"]) == 1
    assert len(json.loads(messages[-1].content)["data"]["sources"]) == 2
    async with db.SessionLocal() as session:
        assert await session.get(ConversationSummaryRecord, cid) is None


async def test_auxiliary_summary_is_removed_before_requesting_new_summary(engine, monkeypatch):
    _, _, messages = await memory_and_knowledge(monkeypatch)
    data = context.context_data(messages)
    data["summaries"].append({"conversation_id": "RELATED", "summary_text": "关联记录 " * 1000})
    messages[1] = context._data_message(data, messages[1].additional_kwargs["memory_context"])
    # 去掉关联摘要后，原历史和全部证据可以直接容纳。
    compact = messages[:1] + [context._data_message({**data, "summaries": []})] + messages[2:]
    set_limit(monkeypatch, context.context_tokens(compact) + 20)
    model = MemoryModel()
    await ModelCalls(model).call(messages, {}, node="agent")
    assert [node for node, _ in model.calls] == ["agent"]
    assert not context.context_data(model.calls[0][1])["summaries"]
    assert len(json.loads(model.calls[0][1][-1].content)["data"]["sources"]) == 2
