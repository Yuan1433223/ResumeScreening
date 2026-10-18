# -*- coding: utf-8 -*-
"""单用户认证：签名 Cookie 会话 + pbkdf2 密码哈希（默认关闭，可 env 开启）。

安全模型（P1 单用户本地工作台；与前端冻结契约见 app/api.py 的 /api/auth/* 注释）：
- 会话：itsdangerous.URLSafeTimedSerializer 签名的无状态 Cookie（不落库、不引
  SessionMiddleware）。Cookie 名 ``rs_session``，HttpOnly + SameSite=Lax。
  密钥取 ``AUTH_SECRET_KEY``；未配置时由 ``AUTH_PASSWORD`` 确定性派生并告警——
  生产环境必须显式配置 ``AUTH_SECRET_KEY`` 并做独立密钥管理（KMS）。
- 密码：不落库。``AUTH_USERNAME`` / ``AUTH_PASSWORD`` 来自 env；验证用标准库
  ``hashlib.pbkdf2_hmac``（SHA-256，固定 app salt）对输入密码哈希后与配置密码
  的哈希恒定时间比对。此处固定 app salt 仅为单用户本地演示；生产应改为 per-user
  随机 salt 落库 + 独立密钥管理。
- 关闭态（``AUTH_ENABLED=0``）：``get_current_user`` 直接放行返回 "local"，
  业务路由不拦截；/api/auth/* 三端点仍存在但空操作。
- 惰性读取：所有开关/凭据/密钥都在请求时从 ``src.config`` 模块常量读取（不在本
  模块 import 时固化），便于测试 monkeypatch config 后即时生效。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Optional

from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from src import config

logger = logging.getLogger(__name__)

# 会话 Cookie 名与有效期（12h）；httponly / samesite=lax 由端点 set_cookie 兜底。
COOKIE_NAME = "rs_session"
SESSION_MAX_AGE = 12 * 3600

# pbkdf2 固定 app salt（单用户演示）。生产应 per-user 随机 salt 落库。
_APP_PBKDF2_SALT = b"resumescreening/v1"
_PBKDF2_ITERATIONS = 200_000

# 派生密钥告警只打一次（避免每请求刷屏）。
_secret_warned = False


def auth_enabled() -> bool:
    """惰性返回认证开关（读 config.AUTH_ENABLED，测试 monkeypatch 即时生效）。"""
    return bool(config.AUTH_ENABLED)


# ---------------------------------------------------------------------------
# 密码哈希（pbkdf2_hmac，标准库，不引 argon2）
# ---------------------------------------------------------------------------
def _hash_password(password: str) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        _APP_PBKDF2_SALT,
        _PBKDF2_ITERATIONS,
    )


def verify_password(password: str, expected: str) -> bool:
    """对输入密码哈希后与配置密码哈希恒定时间比对；任一为空直接 False。"""
    if not password or not expected:
        return False
    return hmac.compare_digest(_hash_password(password), _hash_password(expected))


# ---------------------------------------------------------------------------
# 会话：签名 Cookie（无服务端存储）
# ---------------------------------------------------------------------------
def _serializer() -> URLSafeTimedSerializer:
    """惰性构造签名器：密钥优先 AUTH_SECRET_KEY，否则派生自 AUTH_PASSWORD。"""
    global _secret_warned
    secret = (config.AUTH_SECRET_KEY or "").strip()
    if not secret:
        # 确定性派生：同密码同密钥；生产必须显式配置 AUTH_SECRET_KEY。
        secret = hashlib.sha256(
            (config.AUTH_PASSWORD or "").encode("utf-8") + b"::session-secret"
        ).hexdigest()
        if not _secret_warned:
            logger.warning(
                "AUTH_SECRET_KEY 未配置：会话签名密钥派生自 AUTH_PASSWORD（确定性）。"
                " 生产环境应显式配置 AUTH_SECRET_KEY 并做独立密钥管理。"
            )
            _secret_warned = True
    return URLSafeTimedSerializer(secret, salt="rs-session")


def create_session(username: str) -> str:
    """生成签名会话 token（登录成功后由端点 set_cookie 下发）。"""
    return _serializer().dumps({"u": username})


def read_session(token: str) -> Optional[str]:
    """校验并返回用户名；签名非法/过期返回 None。"""
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    username = data.get("u")
    return username if isinstance(username, str) and username else None


# ---------------------------------------------------------------------------
# fail-fast 与 FastAPI 依赖
# ---------------------------------------------------------------------------
def ensure_auth_configured() -> None:
    """fail-fast：开启态但缺用户名/密码 → 启动即抛错，绝不带弱配置运行。

    由 app/api.py 在装配 app 时调用（导入即校验）。关闭态不校验。
    """
    if auth_enabled() and (not config.AUTH_USERNAME or not config.AUTH_PASSWORD):
        raise RuntimeError(
            "AUTH_ENABLED=1 但未配置 AUTH_USERNAME / AUTH_PASSWORD："
            "拒绝以空凭据启动认证。请在 .env 中同时配置两者。"
        )


def get_current_user(request: Request) -> str:
    """FastAPI 依赖：校验会话 Cookie。

    - 关闭态：直接放行返回 "local"（业务路由不拦截）。
    - 开启态：读 rs_session cookie 并验签；失败/缺失/过期一律 401。
    """
    if not auth_enabled():
        return "local"
    token = request.cookies.get(COOKIE_NAME, "")
    username = read_session(token)
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="未认证"
        )
    return username
