from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# 开发环境使用的默认密钥；生产环境启动时会拒绝使用此值。
DEV_JWT_SECRET = "dev-insecure-secret-change-me-0123456789"


def _async_db_url(url: str) -> str:
    """将数据库 URL 转换为对应的异步驱动形式（asyncpg / aiosqlite）。"""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return _asyncpg_query("postgresql+asyncpg://" + url[len("postgresql://") :])
    if url.startswith("sqlite:///"):
        return "sqlite+aiosqlite:///" + url[len("sqlite:///") :]
    return url


def _asyncpg_query(url: str) -> str:
    """将 Neon/Supabase 等 URL 中的 libpq 查询参数转换为 asyncpg 支持的形式。

    asyncpg 使用 `ssl=` 而非 `sslmode=`，且不接受 `channel_binding` 等
    仅适用于 libpq 的选项。
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    params = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "sslmode":
            params.append(("ssl", value))
        elif key not in ("channel_binding", "options"):
            params.append((key, value))
    return urlunsplit(parts._replace(query=urlencode(params)))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # "dev" 允许开发令牌接口、进程内 Redis 替代方案和宽松默认值。
    # "prod" 要求配置真实密钥、Redis 和 Postgres。
    env: str = "dev"

    # 模型供应商："anthropic"、"openai"、"gemini"、"omniroute"
    # （兼容 OpenAI 的代理服务）或 "fake"（供压力测试使用的脚本模型）。
    llm_provider: str = "anthropic"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    openai_model: str = "gpt-4o"
    gemini_model: str = "gemini-3.8-flash"
    # 兼容 OpenAI 的代理服务（如 OmniRoute）。模型 ID 决定推理强度：
    # 智能体退款决策使用 `…-medium`；语气评估等低成本辅助任务
    # 使用 OMNIROUTE_JUDGE_MODEL。
    omniroute_base_url: str = ""
    omniroute_api_key: str = ""
    omniroute_model: str = "cx/gpt-5.6-sol-medium"
    omniroute_judge_model: str = "cx/gpt-5.6-luna-low"
    # 供应商无法连接时尽快切换；响应缓慢时仍由请求超时配置限制等待时间。
    llm_connect_timeout_s: float = 5.0
    # 主模型失败后，在这段冷却时间内直接调用备用模型。
    llm_fallback_cooldown_s: float = 30.0
    # 主供应商出错时使用的可选备用供应商（如 "openai"）。
    llm_fallback_provider: str = ""
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2
    # 脚本模型参数（LLM_PROVIDER=fake）。
    fake_llm_latency_ms: int = 1500
    fake_llm_error_rate: float = 0.0

    # 数据存储：将普通 postgresql:// / sqlite:/// URL 转换为异步驱动形式。
    database_url: str = f"sqlite:///{BACKEND_ROOT / 'var' / 'app.db'}"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_s: float = 10.0
    # 开发便利选项：启动时建表并加载预置数据。
    # 在 compose/k8s 中，数据库迁移由独立任务执行。
    seed_on_startup: bool = False

    # Redis：开发或测试环境留空时使用进程内 fakeredis，仅支持单进程。
    redis_url: str = ""

    # 身份认证：令牌由宿主网站签发。
    # "dev" 为本地身份选择器；"jwt" 为宿主网站签发令牌；
    # "demo" 为公开沙箱，匿名访客获得临时客户身份。
    auth_mode: str = "dev"
    jwt_secret: str = ""
    jwt_jwks_url: str = ""
    jwt_issuer: str = ""
    jwt_audience: str = ""
    dev_token_ttl_s: int = 8 * 3600

    # 公开演示（AUTH_MODE=demo）。
    demo_session_rate_limit: str = "5/hour"  # 每个客户端 IP 创建沙箱的频率上限。
    demo_token_ttl_s: int = 2 * 3600
    demo_data_ttl_hours: int = 24  # 沙箱客户数据在此小时数后清理。
    # 所有访客共用的每日词元上限（0 表示不设上限）。
    # 达到上限后，后续对话使用脚本模型。
    demo_global_daily_token_budget: int = 0
    # 创建沙箱时可选的 Cloudflare Turnstile 机器人验证。
    turnstile_secret: str = ""

    # HTTP 请求配置。
    cors_origins: str = ""  # 以逗号分隔的来源白名单。
    max_request_bytes: int = 16_384
    # 会向 X-Forwarded-For 追加地址的前置代理数量（Cloud Run 为 1）。
    # 用于按 IP 限流；0 表示使用直接连接方的地址。
    trusted_proxy_hops: int = 0

    # 退款规则参数：集中配置，供规则引擎和规则文档共用。
    return_window_days: int = 30
    escalation_threshold: float = 500.0

    # 请求、模型用量与运行边界。
    max_message_chars: int = 2000
    max_conversation_turns: int = 30
    max_conversation_tokens: int = 60000
    customer_daily_token_budget: int = 300_000
    agent_recursion_limit: int = 12
    max_output_tokens: int = 1024
    chat_rate_limit: str = "10/minute"
    # 背压控制：限制所有 Pod 正在处理的智能体对话轮数。
    max_concurrent_turns: int = 200
    turn_timeout_s: float = 120.0

    # 数据保留任务使用的保留天数。
    retention_days: int = 90

    # 日志、指标和追踪配置。
    log_level: str = "INFO"
    log_json: bool = True
    sentry_dsn: str = ""
    # 设置后，GET /metrics 需要携带 `Authorization: Bearer <token>`。
    # 仅集群内 Prometheus 可访问 Pod 时可留空；服务公开时（如 Cloud Run）应设置。
    metrics_token: str = ""
    # Opik 追踪和看板（app/tracing.py）：设置 OPIK_URL_OVERRIDE（自行部署）
    # 或 OPIK_API_KEY（Comet 托管）后才会启用。
    opik_url_override: str = ""
    opik_api_key: str = ""
    opik_workspace: str = ""
    opik_project_name: str = "refund-agent"

    @model_validator(mode="after")
    def _check_prod(self) -> "Settings":
        if self.env == "prod":
            problems = []
            if self.auth_mode not in ("jwt", "demo"):
                problems.append("AUTH_MODE must be 'jwt' or 'demo'")
            if not (self.jwt_jwks_url or self.jwt_secret):
                problems.append("JWT_SECRET or JWT_JWKS_URL is required")
            if self.auth_mode == "demo" and (self.jwt_jwks_url or len(self.jwt_secret) < 32):
                # 演示环境自行签发令牌，因此需要足够强度的 HS256 密钥。
                problems.append("AUTH_MODE=demo needs JWT_SECRET (>= 32 chars) and no JWKS")
            if self.jwt_secret == DEV_JWT_SECRET:
                problems.append("JWT_SECRET must not be the dev secret")
            if not self.redis_url:
                problems.append("REDIS_URL is required")
            if self.async_database_url.startswith("sqlite"):
                problems.append("DATABASE_URL must point at Postgres")
            if problems:
                raise ValueError("invalid production config: " + "; ".join(problems))
        return self

    @property
    def async_database_url(self) -> str:
        return _async_db_url(self.database_url)

    @property
    def effective_jwt_secret(self) -> str:
        if self.jwt_secret:
            return self.jwt_secret
        # 仅供本地开发和演示使用；生产环境启动时必须配置真实 JWT_SECRET。
        return DEV_JWT_SECRET if self.auth_mode in ("dev", "demo") else ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def opik_enabled(self) -> bool:
        return bool(self.opik_url_override or self.opik_api_key)

    @property
    def is_demo(self) -> bool:
        return self.auth_mode == "demo"

    @property
    def has_llm_key(self) -> bool:
        if self.llm_provider == "anthropic":
            return bool(self.anthropic_api_key)
        if self.llm_provider == "openai":
            return bool(self.openai_api_key)
        if self.llm_provider == "gemini":
            return bool(self.gemini_api_key)
        if self.llm_provider == "omniroute":
            return bool(self.omniroute_api_key and self.omniroute_base_url)
        return self.llm_provider == "fake"


@lru_cache
def get_settings() -> Settings:
    return Settings()
