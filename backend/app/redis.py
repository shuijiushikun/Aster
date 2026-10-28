"""Redis 共享状态：限流、会话对话轮次锁、全局并发信号量和客户每日词元预算。

需要在不同 Pod 之间保持一致的状态统一放在这里，使后端能够运行多个无状态副本。
未设置 `REDIS_URL` 时使用进程内 fakeredis，仅供开发和测试使用；生产环境会拒绝
这种配置。
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone

from redis.asyncio import Redis

from app.config import get_settings

_client: Redis | None = None


def get_redis() -> Redis:
    global _client
    if _client is None:
        settings = get_settings()
        if settings.redis_url:
            _client = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                max_connections=256,
                socket_timeout=5,
                socket_connect_timeout=5,
                health_check_interval=30,
            )
        else:
            from fakeredis import FakeAsyncRedis

            _client = FakeAsyncRedis(decode_responses=True)
    return _client


def set_redis(client: Redis | None) -> None:
    """替换 Redis 客户端，供测试使用。"""
    global _client
    _client = client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# 固定窗口限流，支持 "10/minute"、"5/second"、"100/hour"、"1000/day" 等格式。
# ---------------------------------------------------------------------------

_PERIODS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
_LIMIT_RE = re.compile(r"^\s*(\d+)\s*/\s*(second|minute|hour|day)s?\s*$")


def parse_limit(spec: str) -> tuple[int, int]:
    m = _LIMIT_RE.match(spec)
    if not m:
        raise ValueError(f"bad rate limit spec: {spec!r}")
    return int(m.group(1)), _PERIODS[m.group(2)]


async def hit_rate_limit(key: str, spec: str) -> tuple[bool, int]:
    """计入一次请求，返回（是否允许、距离窗口重置的秒数）。"""
    limit, period = parse_limit(spec)
    window = int(time.time()) // period
    rkey = f"rl:{key}:{period}:{window}"
    r = get_redis()
    pipe = r.pipeline()
    pipe.incr(rkey)
    pipe.expire(rkey, period + 1)
    count, _ = await pipe.execute()
    reset_in = period - int(time.time()) % period
    return count <= limit, reset_in


# ---------------------------------------------------------------------------
# 会话轮次锁：同一会话每次只允许处理一轮智能体对话。
# ---------------------------------------------------------------------------

_RELEASE_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


async def acquire_turn_lock(conversation_id: str, ttl_s: float) -> str | None:
    token = uuid.uuid4().hex
    ok = await get_redis().set(
        f"lock:conv:{conversation_id}", token, nx=True, px=int(ttl_s * 1000)
    )
    return token if ok else None


async def release_turn_lock(conversation_id: str, token: str) -> None:
    await get_redis().eval(_RELEASE_LOCK, 1, f"lock:conv:{conversation_id}", token)


async def owns_turn_lock(conversation_id: str, token: str) -> bool:
    return await get_redis().get(f"lock:conv:{conversation_id}") == token


async def acquire_workflow_lock(ticket_id: str, ttl_s: float) -> str | None:
    token = uuid.uuid4().hex
    ok = await get_redis().set(f"workflow:{ticket_id}", token, nx=True, px=int(ttl_s * 1000))
    return token if ok else None


async def release_workflow_lock(ticket_id: str, token: str) -> None:
    await get_redis().eval(_RELEASE_LOCK, 1, f"workflow:{ticket_id}", token)


# ---------------------------------------------------------------------------
# 全局对话并发信号量：记录自动过期，避免异常退出后长期占用名额。
# ---------------------------------------------------------------------------

_SEM_KEY = "sem:turns"
_ACQUIRE_SEM = """
local now = tonumber(ARGV[1])
redis.call('zremrangebyscore', KEYS[1], '-inf', now - tonumber(ARGV[2]))
if redis.call('zcard', KEYS[1]) < tonumber(ARGV[3]) then
  redis.call('zadd', KEYS[1], now, ARGV[4])
  return 1
end
return 0
"""


async def acquire_turn_slot(limit: int, ttl_s: float) -> str | None:
    token = uuid.uuid4().hex
    ok = await get_redis().eval(
        _ACQUIRE_SEM, 1, _SEM_KEY, time.time(), ttl_s, limit, token
    )
    return token if ok else None


async def release_turn_slot(token: str) -> None:
    await get_redis().zrem(_SEM_KEY, token)


async def turns_in_flight() -> int:
    return int(await get_redis().zcard(_SEM_KEY))


# ---------------------------------------------------------------------------
# 每位客户的每日词元预算。
# ---------------------------------------------------------------------------

# 同时累计所有客户的全局总用量，供公开演示执行费用上限
# （`DEMO_GLOBAL_DAILY_TOKEN_BUDGET`）。
_GLOBAL = "__all__"


def _budget_key(customer_id: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"budget:{customer_id}:{day}"


async def customer_tokens_today(customer_id: str) -> int:
    return int(await get_redis().get(_budget_key(customer_id)) or 0)


async def global_tokens_today() -> int:
    return await customer_tokens_today(_GLOBAL)


async def add_customer_tokens(customer_id: str, tokens: int) -> None:
    pipe = get_redis().pipeline()
    for key in (_budget_key(customer_id), _budget_key(_GLOBAL)):
        pipe.incrby(key, tokens)
        pipe.expire(key, 2 * 86400)
    await pipe.execute()
