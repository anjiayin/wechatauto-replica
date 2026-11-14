# -*- coding: utf-8 -*-
"""管理端鉴权依赖：会话校验、CSRF 双提交、强制改密拦截。

认证方式（两种，Cookie 优先）：
1. ``Cookie: ls_session=<token>``：管理台使用，**写操作必须带 CSRF 头**；
2. ``Authorization: Bearer <token>``：运维脚本/curl 使用，**不校验 CSRF**
   （没有浏览器自动携带 Cookie 的风险）。
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, Optional

from fastapi import Request

from app import db, security, signing
from app.config import get_config
from app.errors import ApiError
from app.repo import admins

logger = logging.getLogger(__name__)

#: 写操作（需要 CSRF 校验的方法）
_UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _extract_token(request: Request) -> tuple[str, bool]:
    """取会话令牌，返回 ``(token, 是否来自 Cookie)``。"""
    cfg = get_config()
    cookie_token = request.cookies.get(cfg.cookie_name) or ""
    if cookie_token:
        return cookie_token.strip(), True
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip(), False
    return "", False


def _public_user(row: Dict[str, Any]) -> Dict[str, Any]:
    """账号行 → 对外用户字典（不含密码哈希）。"""
    return {
        "id": int(row.get("id") or 0),
        "username": str(row.get("username") or ""),
        "display_name": str(row.get("display_name") or ""),
        "role": str(row.get("role") or "admin"),
        "must_change_password": bool(int(row.get("must_change_password") or 0)),
    }


def require_admin(request: Request) -> Dict[str, Any]:
    """会话校验依赖（返回当前用户字典，同时把会话挂到 ``request.state``）。"""
    token, via_cookie = _extract_token(request)
    if not token:
        raise ApiError("unauthorized", "请先登录（会话不存在或已过期）", 401)

    token_hash = security.hash_token(token)
    moment = signing.now()
    cfg = get_config()
    with db.transaction() as cur:
        session = admins.get_session_by_token(cur, token_hash)
        if session is None or session.get("revoked_at"):
            raise ApiError("unauthorized", "会话已失效，请重新登录", 401)
        if session["expires_at"] <= moment:
            raise ApiError("unauthorized", "会话已过期，请重新登录", 401)

        user_row = admins.get_by_id(cur, int(session["user_id"]))
        if user_row is None:
            raise ApiError("unauthorized", "账号不存在，请重新登录", 401)
        if not int(user_row.get("status") or 0):
            raise ApiError("forbidden", "账号已被停用，请联系管理员", 403)

        # 滑动续期：剩余寿命不足一半时顺延，但不超过 created_at + SESSION_MAX_DAYS
        ttl = timedelta(hours=cfg.session_ttl_hours)
        remaining = session["expires_at"] - moment
        if remaining < ttl / 2:
            hard_limit = session["created_at"] + timedelta(
                days=cfg.session_max_days)
            new_expires = min(moment + ttl, hard_limit)
            if new_expires > session["expires_at"]:
                admins.touch_session(cur, int(session["id"]), moment,
                                     new_expires)
                session["expires_at"] = new_expires

    request.state.session = session
    request.state.via_cookie = via_cookie
    user = _public_user(user_row)
    user["csrf_token"] = str(session.get("csrf_token") or "")
    request.state.user = user
    return user


def require_csrf(request: Request) -> None:
    """CSRF 双提交校验（仅 Cookie 认证路径的写操作生效）。

    GET/HEAD/OPTIONS 不校验（这类请求在代码里保证不改数据）。
    """
    if request.method.upper() not in _UNSAFE_METHODS:
        return
    session = getattr(request.state, "session", None)
    if session is None:
        raise ApiError("unauthorized", "请先登录", 401)
    if not getattr(request.state, "via_cookie", False):
        return                                     # Bearer 认证免 CSRF
    header = request.headers.get("x-csrf-token") or ""
    if not security.compare_token(str(session.get("csrf_token") or ""), header):
        logger.warning("CSRF 校验失败：%s %s", request.method, request.url.path)
        raise ApiError("csrf_invalid", "页面令牌已失效，请刷新页面后重试", 403)


def require_password_changed(request: Request) -> None:
    """``must_change_password=1`` 时只放行 me/password/logout。"""
    user = getattr(request.state, "user", None)
    if user is None:
        raise ApiError("unauthorized", "请先登录", 401)
    if user.get("must_change_password"):
        raise ApiError("must_change_password", "首次登录必须先修改密码", 403)


def current_user(request: Request) -> Dict[str, Any]:
    """取当前用户（依赖缓存，可直接在路由里复用）。"""
    user: Optional[Dict[str, Any]] = getattr(request.state, "user", None)
    if user is None:
        user = require_admin(request)
    return user


def get_client_ip(request: Request) -> str:
    """客户端 IP（按 ``LS_TRUST_PROXY`` 决定是否信任代理头）。"""
    return security.get_client_ip(request)
