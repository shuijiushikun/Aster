"""身份认证：验证宿主网站签发的 JWT。

客户身份只取自验证通过的令牌（`sub` 为客户 ID）。智能体不会根据聊天文本
“验证”身份，因此用户无法通过输入他人的邮箱冒充对方。

支持以下验证方式：
- 使用共享密钥（`JWT_SECRET`）验证 HS256 签名；
- 通过签发方的 JWKS 接口（`JWT_JWKS_URL`）验证 RS256/ES256 签名。

令牌声明包括：`sub`（客户或管理员 ID）、可选的 `role`（默认为 "customer"，
也可为 "admin"）、必填的 `exp`，以及配置后需要验证的 `iss`/`aud`。
管理员令牌可以携带 `scope`（客户 ID），将可见范围限制为该客户的会话。
公开演示会签发这种受限管理员令牌，使每位访客只能查看自己的智能体执行记录。

`AUTH_MODE=dev` 时额外提供 `POST /api/dev/token`，供本地演示和压力测试签发令牌；
生产环境不提供此路由。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, Request

from app.config import get_settings

ROLE_CUSTOMER = "customer"
ROLE_ADMIN = "admin"


@dataclass(frozen=True)
class Principal:
    sub: str
    role: str
    # 仅用于管理员令牌：将可见范围限制为该客户的数据。
    scope: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


class AuthError(Exception):
    pass


@lru_cache
def _jwks_client(url: str) -> jwt.PyJWKClient:
    # 在进程内缓存密钥；遇到未知的 `kid` 时重新获取。
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)


def decode_token(token: str) -> Principal:
    settings = get_settings()
    options = {"require": ["exp", "sub"]}
    kwargs: dict = {}
    if settings.jwt_audience:
        kwargs["audience"] = settings.jwt_audience
    else:
        options["verify_aud"] = False
    if settings.jwt_issuer:
        kwargs["issuer"] = settings.jwt_issuer
    try:
        if settings.jwt_jwks_url:
            key = _jwks_client(settings.jwt_jwks_url).get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=["RS256", "ES256"], options=options, **kwargs
            )
        else:
            secret = settings.effective_jwt_secret
            if not secret:
                raise AuthError("server has no JWT verification key configured")
            claims = jwt.decode(
                token, secret, algorithms=["HS256"], options=options, **kwargs
            )
    except jwt.PyJWTError as exc:
        raise AuthError(str(exc)) from exc

    role = claims.get("role", ROLE_CUSTOMER)
    if role not in (ROLE_CUSTOMER, ROLE_ADMIN):
        raise AuthError("unknown role")
    scope = claims.get("scope")
    return Principal(sub=str(claims["sub"]), role=role, scope=str(scope) if scope else None)


def mint_token(
    sub: str, role: str = ROLE_CUSTOMER, ttl_s: int | None = None, scope: str | None = None
) -> str:
    """使用配置的密钥签发 HS256 令牌，供开发、测试和压力测试使用。"""
    settings = get_settings()
    now = int(time.time())
    claims = {
        "sub": sub,
        "role": role,
        "iat": now,
        "exp": now + (ttl_s or settings.dev_token_ttl_s),
    }
    if scope:
        claims["scope"] = scope
    if settings.jwt_issuer:
        claims["iss"] = settings.jwt_issuer
    if settings.jwt_audience:
        claims["aud"] = settings.jwt_audience
    return jwt.encode(claims, settings.effective_jwt_secret, algorithm="HS256")


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token:
        return token.strip()
    return None


def principal_from_request(request: Request) -> Principal | None:
    """尽可能解析令牌且不向外抛出异常，供生成限流键和记录日志使用。"""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    token = bearer_token(request)
    if not token:
        return None
    try:
        principal = decode_token(token)
    except AuthError:
        return None
    request.state.principal = principal
    return principal


async def get_principal(request: Request) -> Principal:
    token = bearer_token(request)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="请先登录。",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        principal = decode_token(token)
    except AuthError:
        raise HTTPException(
            status_code=401,
            detail="登录凭证无效或已过期，请重新登录。",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    request.state.principal = principal
    return principal


async def require_customer(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.role != ROLE_CUSTOMER:
        raise HTTPException(status_code=403, detail="需要客户身份才能使用此功能。")
    return principal


async def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员身份才能使用此功能。")
    return principal
