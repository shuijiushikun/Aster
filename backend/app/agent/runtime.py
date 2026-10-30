"""Per-turn model accounting, independent of parent/subgraph message propagation."""

from dataclasses import dataclass, field
from uuid import uuid4

from langchain_core.runnables import RunnableConfig


@dataclass
class TurnRuntime:
    usage: list[dict] = field(default_factory=list)


async def invoke_model(model, messages, config: RunnableConfig, *, node: str, tools=None):
    # Minimal scripted models predate RunnableConfig. Real LangChain models inherit
    # Runnable and receive callbacks/config so tracing also covers specialist calls.
    from langchain_core.runnables import Runnable

    from app.agent.context import fit_messages

    options = getattr(model, "kwargs", {})
    options = options if isinstance(options, dict) else {}
    messages = fit_messages(messages, tools=tools if tools is not None else options.get("tools"),
                            response_format=options.get("response_format"))
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
    """Shared history preparation, model fallback and usage accounting."""

    def __init__(self, model, fallback=None):
        self.model, self.fallback = model, fallback

    async def call(self, messages, config, *, node, tools=None, json_mode=False):
        from app.agent.graph import breaker
        from app.agent.memory import prepare_model_context

        def bound(model):
            if tools is not None:
                return model.bind_tools(tools)
            # OpenAI-compatible providers support JSON mode. Other providers still
            # get the JSON schema in the prompt and undergo identical validation.
            from langchain_openai import ChatOpenAI

            if json_mode and isinstance(model, ChatOpenAI):
                return model.bind(response_format={"type": "json_object"})
            return model

        primary = bound(self.model)
        secondary = bound(self.fallback) if self.fallback is not None else None
        candidate = secondary if secondary is not None and breaker.is_open() else primary
        options = getattr(candidate, "kwargs", {})
        options = options if isinstance(options, dict) else {}
        messages = await prepare_model_context(messages, config, self,
            tools=tools if tools is not None else options.get("tools"), response_format=options.get("response_format"))
        # 补摘要也可能触发熔断；整理完成后重新选择实际请求的模型。
        if secondary is None or not breaker.is_open():
            try:
                return await invoke_model(primary, messages, config, node=node, tools=tools)
            except Exception as exc:
                from app.agent.context import ContextBudgetExceeded
                from app.db.turns import StaleTurn

                if isinstance(exc, (ContextBudgetExceeded, StaleTurn)):
                    raise
                if self.fallback is None:
                    raise
                breaker.trip(exc)
        return await invoke_model(secondary, messages, config, node=node, tools=tools)
