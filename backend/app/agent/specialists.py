"""Two bounded specialists. No write tool is exposed to either model."""

import json
from typing import Annotated, TypedDict
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages


class SpecialistState(TypedDict):
    messages: Annotated[list, add_messages]
    calls: int


def compile_specialist(caller, tools, name, *, allowed=None, finish_after_tools=False):
    registry = {tool.name: tool for tool in tools}

    async def agent(state: SpecialistState, config: RunnableConfig):
        reply = await caller.call(state["messages"], config, node=name, tools=tools)
        return {"messages": [reply]}

    async def execute(state: SpecialistState, config: RunnableConfig):
        count, results = state.get("calls", 0), []
        for call in state["messages"][-1].tool_calls:
            tool_name, args = call["name"], call.get("args", {})
            if count >= 3:
                result = {"error": "TOOL_LIMIT", "message": "本轮只读工具调用已达上限。"}
            elif tool_name not in registry or (
                allowed is not None and not allowed(tool_name, args)
            ):
                count += 1
                result = {"error": "INVALID_TOOL_ARGUMENT", "message": "请先明确订单号或商品型号。"}
            else:
                count += 1
                try:
                    result = await registry[tool_name].ainvoke(args, config=config)
                except Exception:
                    result = {"error": "TOOL_UNAVAILABLE", "message": "查询暂不可用，请稍后重试。"}
            content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            results.append(
                ToolMessage(
                    content=content, name=tool_name, tool_call_id=call["id"], id=str(uuid4())
                )
            )
        return {"messages": results, "calls": count}

    def next_node(state):
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else END

    graph = StateGraph(SpecialistState)
    graph.add_node("agent", agent)
    graph.add_node("tools", execute)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", next_node, {"tools": "tools", END: END})
    graph.add_conditional_edges(
        "tools",
        lambda state: END if finish_after_tools or state["calls"] >= 3 else "agent",
        {"agent": "agent", END: END},
    )
    return graph.compile()
