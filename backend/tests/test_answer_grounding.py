"""最终知识回答必须给出可在本轮资料中定位的依据。"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from app.agent.answer_grounding import ground_answer

SOURCE = {"document_id": "SIZE", "revision": "1", "chunk_index": 0,
          "title": "尺码说明", "model_code": "TSHIRT-01", "excerpt": "L码胸宽54厘米，肩宽46厘米。资料未提供袖长。"}


class Caller:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def call(self, messages, config, **kwargs):
        self.calls.append((messages, kwargs))
        if isinstance(self.response, BaseException):
            raise self.response
        return AIMessage(json.dumps(self.response, ensure_ascii=False))


def claim(text="L码的胸宽为54厘米、肩宽为46厘米。", quote="L码胸宽54厘米，肩宽46厘米。", **changes):
    return {"document_id": "SIZE", "revision": "1", "chunk_index": 0, "text": text, "quote": quote, **changes}


async def test_supported_answer_is_rendered_from_validated_claims():
    caller = Caller({"claims": [claim()], "missing": []})
    result = await ground_answer("L码胸宽肩宽多少", {"status": "ok", "sources": [SOURCE]}, caller, {})
    assert result.status == "supported"
    assert "54厘米" in result.text and "尺码说明" in result.text
    assert len(caller.calls) == 1 and caller.calls[0][1]["json_mode"]


@pytest.mark.parametrize("change", [{"quote": "袖长99厘米"}, {"document_id": "ANOTHER"},
    {"revision": "old"}, {"chunk_index": 9}, {"text": "L码袖长99厘米。"}, {"text": "已获得CERT-999认证。"}])
async def test_forged_quote_scope_or_number_fails_closed(change):
    caller = Caller({"claims": [claim(**change)], "missing": []})
    result = await ground_answer("L码袖长多少", {"status": "ok", "sources": [SOURCE]}, caller, {})
    assert result.status == "unavailable"
    assert "99厘米" not in result.text and not result.sources


async def test_related_document_without_requested_fact_is_explained_precisely():
    caller = Caller({"claims": [], "missing": ["L码的袖长"]})
    result = await ground_answer("L码袖长", {"status": "ok", "sources": [SOURCE]}, caller, {})
    assert result.status == "insufficient"
    assert "袖长" in result.text and "服务故障" not in result.text


async def test_partial_answer_keeps_supported_facts_and_marks_missing_fact():
    caller = Caller({"claims": [claim()], "missing": ["袖长"]})
    result = await ground_answer("L码胸宽肩宽袖长", {"status": "ok", "sources": [SOURCE]}, caller, {})
    assert result.status == "partial"
    assert "54" in result.text and "袖长" in result.text and "尚未" in result.text


@pytest.mark.parametrize("response", [RuntimeError("provider failed"), {"status": "supported", "claims": [], "missing": []}])
async def test_invalid_or_failed_verification_is_not_treated_as_missing_knowledge(response):
    result = await ground_answer("L码尺寸", {"status": "ok", "sources": [SOURCE]}, Caller(response), {})
    assert result.status == "unavailable" and "核对" in result.text


async def test_cancellation_propagates():
    with pytest.raises(asyncio.CancelledError):
        await ground_answer("L码", {"status": "ok", "sources": [SOURCE]}, Caller(asyncio.CancelledError()), {})


async def test_model_identifiers_adjacent_to_chinese_are_not_parameter_numbers():
    caller = Caller({"claims": [claim(text="与TSHIRT-01的尺码表比较，胸宽54厘米。")], "missing": []})
    result = await ground_answer("比较尺码", {"status": "ok", "sources": [SOURCE]}, caller, {})
    assert result.status == "supported"


async def test_only_one_repair_is_allowed_before_rendering_answer():
    class Repair(Caller):
        async def call(self, *args, **kwargs):
            if not self.calls:
                self.response = {"claims": [claim(quote="伪造原文")], "missing": []}
            else:
                self.response = {"claims": [claim()], "missing": []}
            return await super().call(*args, **kwargs)

    caller = Repair(None)
    result = await ground_answer("L码尺寸", {"status": "ok", "sources": [SOURCE]}, caller, {})
    assert result.status == "supported" and len(caller.calls) == 2
