"""Per-turn model accounting, independent of parent/subgraph message propagation."""

from dataclasses import dataclass, field
from uuid import uuid4

from langchain_core.runnables import RunnableConfig


@dataclass
class TurnRuntime:
    usage: list[dict] = field(default_factory=list)


async def invoke_model(model, messages, config: RunnableConfig, *, node: str):
    # Minimal scripted models predate RunnableConfig. Real LangChain models inherit
    # Runnable and receive callbacks/config so tracing also covers specialist calls.
    from langchain_core.runnables import Runnable

    runtime = config.get("configurable", {}).get("turn_runtime")
    message = None
    try:
        if isinstance(model, Runnable):
            message = await model.ainvoke(messages, config=config)
        else:
            message = await model.ainvoke(messages)
        if not message.id:
            message.id = str(uuid4())
    finally:
        if isinstance(runtime, TurnRuntime):
            usage = message.usage_metadata if message is not None else None
            meta = (message.response_metadata or {}) if message is not None else {}
            runtime.usage.append(
                {
                    "call_id": str(uuid4()),
                    "node": node,
                    "model": meta.get("model") or meta.get("model_name") or "",
                    "status": "reported" if usage is not None else "unknown",
                    "input_tokens": usage.get("input_tokens") if usage is not None else None,
                    "output_tokens": usage.get("output_tokens") if usage is not None else None,
                }
            )
    return message


class ModelCalls:
    """The same fallback policy and accounting gateway for every V2 model call."""

    def __init__(self, model, fallback=None):
        self.model, self.fallback = model, fallback

    async def call(self, messages, config, *, node, tools=None, json_mode=False):
        from app.agent.graph import breaker

        def bound(model):
            if tools is not None:
                return model.bind_tools(tools)
            # OpenAI-compatible providers support JSON mode. Other providers still
            # get the JSON schema in the prompt and undergo identical validation.
            from langchain_openai import ChatOpenAI

            if json_mode and isinstance(model, ChatOpenAI):
                return model.bind(response_format={"type": "json_object"})
            return model

        if self.fallback is None or not breaker.is_open():
            try:
                return await invoke_model(bound(self.model), messages, config, node=node)
            except Exception as exc:
                if self.fallback is None:
                    raise
                breaker.trip(exc)
        return await invoke_model(bound(self.fallback), messages, config, node=node)
