"""可选的智能体对话 Opik 追踪。

设置 `OPIK_URL_OVERRIDE`（自行部署）或 `OPIK_API_KEY`（Comet 托管）后，
每轮智能体对话都会作为一条追踪记录发送给 Opik，包含模型调用及其词元用量、
预估费用、工具调用和执行图中的步骤。同一会话的各轮追踪共用一个线程
（`thread_id` 等于会话 ID）。

每轮结束后，将决策、输出纠正、模型降级和错误等结果记录为 0/1
反馈分数，使看板能够按时间展示发生比例，并按结果分析费用和耗时
（见 `scripts/opik_dashboards.py`）。

本模块只负责分析追踪。审计记录仍由 `ReasoningEvent` 表保存，实时管理事件流
仍由 Redis 发布/订阅传递，词元预算仍由 `agent/runner.py` 执行。
追踪失败会记录日志，但不会导致当前对话失败。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from app.agent.prompts import prompt_version
from app.config import get_settings
from app.observability import log_event

logger = logging.getLogger(__name__)

_DECISIONS = ("approved", "denied", "escalated")

_client = None


def quiet_sdk() -> None:
    """关闭 Opik SDK 自身的错误上报和产品使用分析。

    必须在首次导入 `opik` 前执行：SDK 导入时会用 Comet 的 DSN 调用
    `sentry_sdk.init()`，从而替换本项目的 Sentry 客户端（`SENTRY_DSN`），
    并将本项目的错误发往 Comet。需要恢复上报时，应显式设置相关环境变量。
    """
    os.environ.setdefault("OPIK_SENTRY_ENABLE", "false")
    os.environ.setdefault("OPIK_ANALYTICS_ENABLE", "false")


def fix_anthropic_cache_accounting() -> bool:
    """修正 Opik 对 Anthropic 提示词缓存词元的重复计数（opik 2.2.95）。

    LangChain 的 `usage_metadata.input_tokens` 已包含缓存读取和写入词元，
    但 Opik 将其映射为 Anthropic 原始的 `input_tokens`（该字段不含缓存词元），
    随后又加上 `cache_read_input_tokens`，导致每次缓存命中同时按原价和缓存价
    重复估算费用。本项目的每次 Anthropic 调用都会缓存系统提示词和工具说明。

    仅在检测到上游仍有重复计数时应用补丁；Opik 修复映射后自动停用。
    返回是否实际应用了补丁。
    """
    from opik.integrations.langchain.provider_usage_extractors.langchain_run_helpers import (
        langchain_usage,
    )

    usage_cls = langchain_usage.LangChainUsage
    if getattr(usage_cls, "_acme_cache_fix", False):
        return True
    probe = usage_cls.from_original_usage_dict(
        {
            "input_tokens": 100,
            "output_tokens": 1,
            "total_tokens": 101,
            "input_token_details": {"cache_read": 40},
        }
    ).map_to_anthropic_usage()
    if probe.get("input_tokens") != 100:
        return False  # 上游已经修复，无需应用补丁。

    original = usage_cls.map_to_anthropic_usage

    def map_to_anthropic_usage(self):
        usage = original(self)
        cached = (usage.get("cache_read_input_tokens") or 0) + (
            usage.get("cache_creation_input_tokens") or 0
        )
        usage["input_tokens"] = max(0, usage["input_tokens"] - cached)
        return usage

    usage_cls.map_to_anthropic_usage = map_to_anthropic_usage
    usage_cls._acme_cache_fix = True
    return True


def opik_host() -> str | None:
    """确定追踪数据的发送地址。

    仅配置 API 密钥且未覆盖 URL 时，显式使用 Comet 托管的 Opik，
    防止本地 `~/.opik.config` 中的其他配置悄悄将数据重定向到自行部署的实例。
    """
    settings = get_settings()
    if settings.opik_url_override:
        return settings.opik_url_override
    if settings.opik_api_key:
        from opik.config import OPIK_URL_CLOUD

        return OPIK_URL_CLOUD
    return None


def _get_client():
    """首次使用时创建并复用进程级 Opik 客户端；未启用时返回 None。"""
    global _client
    settings = get_settings()
    if _client is None and settings.opik_enabled:
        quiet_sdk()
        import opik

        fix_anthropic_cache_accounting()

        _client = opik.Opik(
            project_name=settings.opik_project_name,
            workspace=settings.opik_workspace or None,
            host=opik_host(),
            api_key=settings.opik_api_key or None,
        )
        # OpikTracer 通过全局客户端记录追踪。
        opik.set_global_client(_client)
    return _client


def configure() -> None:
    try:
        if _get_client() is not None:
            log_event(logger, "opik tracing enabled", project=get_settings().opik_project_name)
    except Exception as exc:  # 配置错误不能阻止应用运行。
        log_event(logger, "opik setup failed", level=logging.WARNING, error=str(exc)[:300])


def flush() -> None:
    if _client is not None:
        try:
            _client.flush(timeout=5)
        except Exception as exc:
            log_event(logger, "opik flush failed", level=logging.WARNING, error=str(exc)[:300])


def tracer_for_turn(conversation_id: str, customer_id: str, *, scripted: bool):
    """为本轮创建 `OpikTracer` 回调；未启用追踪时返回 None。"""
    settings = get_settings()
    try:
        if _get_client() is None:
            return None
        from opik.integrations.langchain import OpikTracer

        provider = "fake" if scripted else settings.llm_provider
        version = prompt_version(unified=settings.agent_v2_enabled and not scripted)
        tags = [f"provider:{provider}", f"prompt:{version}"]
        tags.append("scripted" if scripted else "live")
        if settings.is_demo:
            tags.append("demo")
        return OpikTracer(
            project_name=settings.opik_project_name,
            thread_id=conversation_id,
            tags=tags,
            metadata={
                "customer_id": customer_id,
                "env": settings.env,
                "prompt_version": version,
            },
            # 同一进程内有多轮对话并发执行，应避免修改共享的 Opik 上下文栈。
            opik_context_read_only_mode=True,
        )
    except Exception as exc:
        log_event(logger, "opik tracer setup failed", level=logging.WARNING, error=str(exc)[:300])
        return None


@dataclass
class TurnOutcome:
    decision: str | None = None
    corrected: bool = False
    fallback: bool = False
    error: bool = False


def finish_turn(tracer, outcome: TurnOutcome) -> dict | None:
    """为本轮追踪记录结果分数，并向时间线返回 `{trace_id, url?}`。

    公开演示的访客无法访问 Opik 工作区，因此省略追踪 URL。
    """
    if tracer is None:
        return None
    settings = get_settings()
    try:
        traces = tracer.created_traces()
        if not traces:
            return None
        trace_id = traces[-1].id
        flags = {f"refund_{d}": outcome.decision == d for d in _DECISIONS}
        flags |= {
            "sanitizer_correction": outcome.corrected,
            "fallback_used": outcome.fallback,
            "turn_error": outcome.error,
        }
        _get_client().log_traces_feedback_scores(
            [
                {
                    "id": trace_id,
                    "name": name,
                    "value": float(value),
                    "project_name": settings.opik_project_name,
                }
                for name, value in flags.items()
            ]
        )
        info = {"trace_id": trace_id}
        if not settings.is_demo:
            from opik import url_helpers
            from opik.config import OPIK_URL_CLOUD

            info["url"] = url_helpers.get_project_url_by_trace_id(
                trace_id, settings.opik_url_override or OPIK_URL_CLOUD
            )
        return info
    except Exception as exc:
        log_event(logger, "opik scoring failed", level=logging.WARNING, error=str(exc)[:300])
        return None
