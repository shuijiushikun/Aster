"""后端到商城服务的短时签名上下文，不接受前端 JWT 或模型提供的客户身份。"""

import time

import jwt
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import Principal
from app.config import Settings

ISSUER = "support-backend"
AUDIENCE = "commerce-mcp"
TOKEN_TTL_S = 60


def service_secret(settings: Settings) -> str:
    secret = settings.commerce_mcp_service_secret
    if len(secret.encode()) < 32:
        raise ValueError("COMMERCE_MCP_SERVICE_SECRET must contain at least 32 bytes")
    return secret


def mint_service_token(principal: Principal, settings: Settings) -> str:
    if principal.role != "customer" or not principal.sub.strip():
        raise ValueError("MCP reads require an authenticated customer")
    now = int(time.time())
    return jwt.encode(
        {
            "sub": principal.sub,
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + TOKEN_TTL_S,
        },
        service_secret(settings),
        algorithm="HS256",
    )


class ServiceContextMiddleware:
    def __init__(self, app: ASGIApp, secret: str):
        self.app = app
        self.secret = secret

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scheme, _, token = Headers(scope=scope).get("authorization", "").partition(" ")
        try:
            if scheme.lower() != "bearer" or not token.strip():
                raise jwt.InvalidTokenError()
            claims = jwt.decode(
                token.strip(),
                self.secret,
                algorithms=["HS256"],
                audience=AUDIENCE,
                issuer=ISSUER,
                options={"require": ["sub", "exp", "iat", "aud", "iss"]},
            )
            subject = claims["sub"]
            if not isinstance(subject, str) or not subject.strip() or len(subject) > 128:
                raise jwt.InvalidTokenError()
            if claims["exp"] - claims["iat"] > TOKEN_TTL_S:
                raise jwt.InvalidTokenError()
        except (jwt.PyJWTError, TypeError, ValueError):
            response = JSONResponse(
                {"error": "IDENTITY_NOT_VERIFIED", "message": "商城服务身份无效或已过期。"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["commerce_customer_id"] = subject
        await self.app(scope, receive, send)
