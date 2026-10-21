"""统一封装不同供应商的聊天模型创建过程。

根据供应商配置返回 LangChain 聊天模型。客户端按进程缓存，使不同请求能够
复用连接池中的 HTTP 连接，并明确设置超时和重试参数。

`LLM_PROVIDER=fake` 返回具有模拟延迟的脚本工具调用模型，供压力测试验证
基础设施而不消耗模型词元（见 `app/agent/llm_fake.py`）。
"""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings

FAKE_MODEL_NAME = "scripted-fake"


def _build(provider: str, max_retries: int | None = None):
    settings = get_settings()
    provider = provider.lower()
    retries = settings.llm_max_retries if max_retries is None else max_retries

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        if not settings.anthropic_api_key:
            raise RuntimeError(
                "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set."
            )
        return ChatAnthropic(
            model=settings.anthropic_model,
            api_key=settings.anthropic_api_key,
            temperature=0,
            max_tokens=settings.max_output_tokens,
            timeout=settings.llm_timeout_s,
            max_retries=retries,
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        if not settings.openai_api_key:
            raise RuntimeError("LLM_PROVIDER=openai but OPENAI_API_KEY is not set.")
        return ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            temperature=0,
            max_tokens=settings.max_output_tokens,
            timeout=settings.llm_timeout_s,
            max_retries=retries,
        )

    if provider == "omniroute":
        import httpx
        from langchain_openai import ChatOpenAI

        if not (settings.omniroute_api_key and settings.omniroute_base_url):
            raise RuntimeError(
                "LLM_PROVIDER=omniroute needs OMNIROUTE_BASE_URL and OMNIROUTE_API_KEY."
            )
        # 不传 temperature：GPT-5.x 推理模型仅接受默认值。
        return ChatOpenAI(
            model=settings.omniroute_model,
            base_url=settings.omniroute_base_url,
            api_key=settings.omniroute_api_key,
            max_tokens=settings.max_output_tokens,
            timeout=httpx.Timeout(settings.llm_timeout_s, connect=settings.llm_connect_timeout_s),
            max_retries=retries,
        )

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        if not settings.gemini_api_key:
            raise RuntimeError("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set.")
        # 不传 temperature：Google 建议 Gemini 3 使用默认值 1.0，过低可能导致循环。
        # 工具调用所需的思考签名保存在 AIMessage 中，因此执行图会原样回放这些消息。
        return ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            google_api_key=settings.gemini_api_key,
            max_tokens=settings.max_output_tokens,
            timeout=settings.llm_timeout_s,
            max_retries=retries,
        )

    if provider == "fake":
        from app.agent.llm_fake import ScriptedFakeChatModel

        return ScriptedFakeChatModel(
            latency_ms=settings.fake_llm_latency_ms,
            error_rate=settings.fake_llm_error_rate,
        )

    raise RuntimeError(
        f"Unknown LLM provider '{provider}'. "
        "Use 'anthropic', 'openai', 'gemini', 'omniroute' or 'fake'."
    )


@lru_cache
def get_model(provider: str, max_retries: int | None = None):
    """获取 `provider` 对应的缓存客户端，也用于演示的脚本模式。"""
    return _build(provider, max_retries)


def get_chat_model():
    settings = get_settings()
    if get_fallback_model() is not None:
        # 已配置备用模型时，反复重试不可用的主模型只会延迟切换。
        return get_model(settings.llm_provider, min(settings.llm_max_retries, 1))
    return get_model(settings.llm_provider)


@lru_cache
def get_fallback_model():
    """返回主模型失败时使用的备用模型；未配置时返回 None。"""
    settings = get_settings()
    fallback = settings.llm_fallback_provider
    if not fallback or fallback == settings.llm_provider:
        return None
    return get_model(fallback)


def primary_model_name() -> str:
    """返回主供应商响应中的模型 ID。

    供应商可能附加日期版本，因此与 `response_metadata` 中的值按前缀匹配。
    """
    settings = get_settings()
    return {
        "anthropic": settings.anthropic_model,
        "openai": settings.openai_model,
        "gemini": settings.gemini_model,
        # 代理返回上游模型 ID 时会去掉路由前缀，
        # 例如 `cx/gpt-5.6-sol-medium` 返回为 `gpt-5.6-sol-medium`。
        "omniroute": settings.omniroute_model.rsplit("/", 1)[-1],
        "fake": FAKE_MODEL_NAME,
    }.get(settings.llm_provider, "")


def use_prompt_caching() -> bool:
    """判断是否为系统提示词及其前缀中的工具说明启用 Anthropic 提示词缓存。

    配置真实备用供应商时关闭：同一消息列表会重放给备用模型，其他供应商可能
    拒绝 Anthropic 的 `cache_control` 内容块字段。脚本备用模型 `fake`
    会忽略该字段，因此仍可启用缓存。
    """
    settings = get_settings()
    return settings.llm_provider == "anthropic" and settings.llm_fallback_provider in ("", "fake")
