"""LangGraph 工具调用循环：agent 提出调用，tools 执行，直到模型给出最终回复。

工具内部完成“查询 → 校验 → 决策”，模型即使受到指令注入也无法绕过退款规则。
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.agent.llm import get_chat_model, get_fallback_model, get_model
from app.agent.tools import TOOLS
from app.config import get_settings
from app.observability import log_event

logger = logging.getLogger(__name__)


# add_messages 会把每个节点新产生的消息合并进历史，而不是直接覆盖整个列表。
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


# 每个进程复用已编译的图；客户身份在每次请求的配置中传入，不能缓存进图内。
_cache: dict[bool, tuple] = {}


class PrimaryBreaker:
    """进程级熔断：主模型失败后短暂直连备用模型，冷却结束再尝试主模型。

    避免同一轮的每次工具调用都重复等待主模型超时。
    """

    def __init__(self):
        self.open_until = 0.0

    def is_open(self) -> bool:
        return time.monotonic() < self.open_until

    def trip(self, exc: Exception) -> None:
        cooldown = get_settings().llm_fallback_cooldown_s
        self.open_until = time.monotonic() + cooldown
        log_event(
            logger,
            "primary model failed; using fallback",
            level=logging.WARNING,
            error_type=type(exc).__name__,
            cooldown_s=cooldown,
        )


breaker = PrimaryBreaker()


def get_agent(scripted: bool = False):
    """复用当前模型对应的执行图；scripted=True 时切换到零模型费用的脚本演示。

    每次请求通过 config["configurable"]["tool_ctx"] 单独注入已验证的客户身份。
    """
    if scripted:
        model, fallback = get_model("fake"), None
    else:
        model, fallback = get_chat_model(), get_fallback_model()
    cached = _cache.get(scripted)
    if cached is not None and cached[0] is model and cached[1] is fallback:
        return cached[2]
    agent = _compile(model, fallback)
    _cache[scripted] = (model, fallback, agent)
    return agent


def _compile(model, fallback):
    tools = TOOLS
    # 绑定工具只告诉模型有哪些工具可选；真正执行 Python 函数的是下方的 ToolNode。
    primary = model.bind_tools(tools)
    secondary = fallback.bind_tools(tools) if fallback is not None else None
    tool_node = ToolNode(tools)

    # 每次进入 agent 节点都调用一次模型；同一轮问答可能在执行工具后再次调用模型。
    async def agent_node(state: AgentState) -> dict:
        messages = state["messages"]
        if secondary is None:
            return {"messages": [await primary.ainvoke(messages)]}
        if not breaker.is_open():
            try:
                return {"messages": [await primary.ainvoke(messages)]}
            except Exception as exc:  # 主模型异常时触发熔断；有备用模型才会进入此分支。
                breaker.trip(exc)
        return {"messages": [await secondary.ainvoke(messages)]}

    # 模型返回 tool_calls 就去执行工具；没有工具调用时结束本轮图执行。
    def should_continue(state: AgentState):
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        return END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    # 工具结果以消息形式回到 agent，模型据此决定继续调用工具还是向客户回复。
    graph.add_edge("tools", "agent")
    return graph.compile()
