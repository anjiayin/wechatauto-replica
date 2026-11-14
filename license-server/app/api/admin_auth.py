# -*- coding: utf-8 -*-
"""管理端鉴权接口（A1–A4）。

* 登录失败锁定：连续 ``LOGIN_MAX_FAILED`` 次 → 锁 ``LOGIN_LOCK_MINUTES`` 分钟，
  锁定期内即使密码正确也返回 423；
* 用户名不存在时也跑一次假哈希，避免用响应时间枚举账号；
* 改密成功后吊销该用户**除当前会话外**的全部会话（别处登录立刻失效）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request, Response

from app import db, ratelimit, security, signing
from app.api.deps import require_admin, require_csrf
from app.config import get_config
from app.errors import ApiError
from app.models import ChangePasswordIn, LoginIn
from app.repo import admins, audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/api", tags=["管理端鉴权"])


@router.post("/login", summary="A1 登录")
def login(body: LoginIn, request: Request, response: Response) -> Dict[str, Any]:
    """登录：返回用户信息与 CSRF 令牌，并下发 HttpOnly 会话 Cookie。"""
    cfg = get_config()
    ip = security.get_client_ip(request)
    user_agent = security.get_user_agent(request)
    if not ratelimit.login_limiter().allow(ip):
        logger.warning("登录限流命中：ip=%s", ip)
        raise ApiError("rate_limited", "登录尝试过于频繁，请 1 分钟后再试", 429)

    username = str(body.username or "").strip().lower()
    password = str(body.password or "")
    moment = signing.now()

    # 1) 读账号（dict 出事务后仍可用）
    with db.transaction() as cur:
        row = admins.get_by_username(cur, username)

    if row is None:
        # 防用户名枚举 / 时序差异：仍跑一次哈希
        security.dummy_verify(password)
        with db.transaction() as cur:
            audit.write(cur, now=moment, actor=username, actor_id=0,
                        action="login_failed", target_type="admin",
                        target_id=username, ip=ip, note="账号不存在")
        logger.warning("登录失败（账号不存在）：%s ip=%s", username, ip)
        raise ApiError("login_failed", "用户名或密码不正确", 401)

    locked_until = row.get("locked_until")
    if locked_until is not None and locked_until > moment:
        remain_sec = (locked_until - moment).total_seconds()
        minutes = max(1, int(remain_sec // 60) + 1)
        raise ApiError("account_locked",
                       f"账号已锁定，请 {minutes} 分钟后重试", 423)

    if not int(row.get("status") or 0):
        raise ApiError("account_disabled", "账号已停用，请联系管理员", 403)

    stored_hash = str(row.get("password_hash") or "")
    if not security.verify_password(password, stored_hash):
        with db.transaction() as cur:
            result = admins.record_login_failed(
                cur, int(row["id"]), moment, cfg.login_max_failed,
                cfg.login_lock_minutes)
            audit.write(cur, now=moment, actor=username,
                        actor_id=int(row["id"]), action="login_failed",
                        target_type="admin", target_id=username, ip=ip,
                        note=f"密码错误（第 {result['failed_count']} 次）")
        logger.warning("登录失败（密码错误）：%s ip=%s（第 %d 次）",
                       username, ip, result["failed_count"])
        raise ApiError("login_failed", "用户名或密码不正确", 401)

    # 2) 登录成功：写会话 + 清失败计数（同一事务）
    token = security.new_session_token()
    csrf_token = security.new_csrf_token()
    with db.transaction() as cur:
        admins.record_login_success(cur, int(row["id"]), moment, ip)
        if security.needs_rehash(stored_hash):
            cur.execute("UPDATE admin_user SET password_hash = %s WHERE id = %s",
                        (security.hash_password(password), int(row["id"])))
            logger.info("已升级管理员 %s 的密码哈希迭代次数", username)
        admins.create_session(cur, user_id=int(row["id"]), username=username,
                              token=token, csrf_token=csrf_token, ip=ip,
                              user_agent=user_agent, now=moment)
        audit.write(cur, now=moment, actor=username, actor_id=int(row["id"]),
                    action="login", target_type="admin", target_id=username,
                    ip=ip, note="登录成功")

    security.set_session_cookie(response, token)
    logger.info("登录成功：%s ip=%s", username, ip)
    return {
        "ok": True,
        "user": {
            "id": int(row["id"]),
            "username": str(row.get("username") or ""),
            "display_name": str(row.get("display_name") or ""),
            "role": str(row.get("role") or "admin"),
            "must_change_password": bool(int(row.get("must_change_password") or 0)),
        },
        "csrf_token": csrf_token,
    }


@router.post("/logout", summary="A2 登出",
             dependencies=[Depends(require_admin), Depends(require_csrf)])
def logout(request: Request, response: Response) -> Dict[str, Any]:
    """吊销当前会话并清 Cookie。"""
    session = request.state.session
    user = request.state.user
    moment = signing.now()
    with db.transaction() as cur:
        admins.revoke_session(cur, str(session["token_hash"]), moment)
        audit.write(cur, now=moment, actor=str(user.get("username") or ""),
                    actor_id=int(user.get("id") or 0), action="logout",
                    target_type="admin",
                    target_id=str(user.get("username") or ""),
                    ip=security.get_client_ip(request), note="登出")
    security.clear_session_cookie(response)
    return {"ok": True}


@router.get("/me", summary="A3 当前用户",
            dependencies=[Depends(require_admin)])
def me(request: Request) -> Dict[str, Any]:
    """取当前用户与 CSRF 令牌（刷新页面时恢复前端状态）。"""
    user = request.state.user
    return {
        "ok": True,
        "user": {k: user[k] for k in
                 ("id", "username", "display_name", "role",
                  "must_change_password")},
        "csrf_token": str(user.get("csrf_token") or ""),
    }


@router.post("/password", summary="A4 修改密码",
             dependencies=[Depends(require_admin), Depends(require_csrf)])
def change_password(body: ChangePasswordIn, request: Request
                    ) -> Dict[str, Any]:
    """修改密码：校验原密码 → 校验策略 → 更新 → 踢掉其它会话。"""
    user = request.state.user
    session = request.state.session
    moment = signing.now()
    with db.transaction() as cur:
        row = admins.get_by_id(cur, int(user["id"]))
        if row is None:
            raise ApiError("unauthorized", "账号不存在，请重新登录", 401)
        if not security.verify_password(body.old_password,
                                       str(row.get("password_hash") or "")):
            raise ApiError("old_password_wrong", "原密码不正确", 400)
        new_password = security.validate_password_policy(body.new_password)
        if new_password == str(body.old_password or ""):
            raise ApiError("invalid_param", "新密码不能与原密码相同", 400)
        admins.update_password(cur, int(user["id"]),
                               security.hash_password(new_password), moment,
                               must_change=False)
        removed = admins.revoke_all_sessions(
            cur, int(user["id"]), moment,
            except_token_hash=str(session["token_hash"]))
        audit.write(cur, now=moment, actor=str(user.get("username") or ""),
                    actor_id=int(user["id"] or 0), action="change_password",
                    target_type="admin",
                    target_id=str(user.get("username") or ""),
                    ip=security.get_client_ip(request),
                    note=f"修改密码，踢掉其它 {removed} 个会话")
    logger.info("管理员 %s 修改密码（其它会话 %d 个已失效）",
                user.get("username"), removed)
    return {"ok": True, "revoked_sessions": removed}
