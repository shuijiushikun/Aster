"""后端到商城服务的短时签名上下文，不接受前端 JWT 或模型提供的客户身份。"""

import time

import jwt
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.after_sales.intent import TurnIntent
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


def mint_service_token(principal: Principal, settings: Settings, *,
                       conversation_id: str | None = None, message_id: int | None = None,
                       intent: dict | None = None) -> str:
    if principal.role != "customer" or not principal.sub.strip():
        raise ValueError("MCP reads require an authenticated customer")
    if intent is not None:
        if not conversation_id or type(message_id) is not int or message_id < 1:
            raise ValueError("intent requires a persisted turn")
        intent = TurnIntent.model_validate(intent).model_dump()
    now = int(time.time())
    return jwt.encode(
        {
            "sub": principal.sub,
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + TOKEN_TTL_S,
            **({"conversation_id": conversation_id, "message_id": message_id}
               if conversation_id is not None else {}),
            **({"intent": intent} if intent is not None else {}),
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
            if "conversation_id" in claims and (
                not isinstance(claims["conversation_id"], str)
                or not 1 <= len(claims["conversation_id"]) <= 128
                or type(claims.get("message_id")) is not int
                or claims["message_id"] < 1
            ):
                raise jwt.InvalidTokenError()
            if "intent" in claims:
                if "conversation_id" not in claims:
                    raise jwt.InvalidTokenError()
                claims["intent"] = TurnIntent.model_validate(claims["intent"]).model_dump()
        except (jwt.PyJWTError, TypeError, ValueError):
            response = JSONResponse(
                {"error": "IDENTITY_NOT_VERIFIED", "message": "商城服务身份无效或已过期。"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["commerce_customer_id"] = subject
        scope["state"]["commerce_turn"] = {
            key: claims[key] for key in ("conversation_id", "message_id", "intent") if key in claims
        }
        await self.app(scope, receive, send)
