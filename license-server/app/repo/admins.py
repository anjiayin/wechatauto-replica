# -*- coding: utf-8 -*-
"""管理员与服务端会话数据访问。

会话安全要点：
* 库里**只存** ``sha256(token)``，即使库泄露也无法冒充；
* ``csrf_token`` 随会话行存储，写操作走双提交校验；
* 滑动续期有硬上限 ``created_at + SESSION_MAX_DAYS``（防无限续命）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app import security
from app.config import get_config
from app.db import to_int


# ---------------------------------------------------------------------------
# 管理员账号
# ---------------------------------------------------------------------------
def get_by_username(cur: Any, username: str) -> Optional[Dict[str, Any]]:
    """按用户名取账号（登录用）。"""
    cur.execute("SELECT * FROM admin_user WHERE username = %s LIMIT 1",
                (str(username or "").strip().lower(),))
    return cur.fetchone()


def get_by_id(cur: Any, user_id: int) -> Optional[Dict[str, Any]]:
    """按主键取账号（会话校验用）。"""
    cur.execute("SELECT * FROM admin_user WHERE id = %s LIMIT 1",
                (int(user_id),))
    return cur.fetchone()


def count_all(cur: Any) -> int:
    """管理员数量（首启建号判断用）。"""
    cur.execute("SELECT COUNT(*) AS total FROM admin_user")
    return to_int((cur.fetchone() or {}).get("total"))


def create(cur: Any, *, username: str, password_hash: str,
           display_name: str = "", role: str = "admin",
           must_change_password: bool = False, now: datetime
           ) -> Dict[str, Any]:
    """新建管理员账号。"""
    name = str(username or "").strip().lower()[:32]
    cur.execute(
        "INSERT INTO admin_user "
        "(username, password_hash, display_name, role, status, "
        " must_change_password, failed_count, locked_until, last_login_at, "
        " last_login_ip, password_updated_at, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, 1, %s, 0, NULL, NULL, '', %s, %s, %s)",
        (name, password_hash, str(display_name or "")[:64],
         str(role or "admin")[:16], 1 if must_change_password else 0,
         now, now, now),
    )
    return get_by_username(cur, name) or {}


def update_password(cur: Any, user_id: int, password_hash: str,
                    now: datetime, must_change: bool = False) -> None:
    """更新密码哈希（同时清零失败计数与锁）。"""
    cur.execute(
        "UPDATE admin_user SET password_hash = %s, must_change_password = %s, "
        "       failed_count = 0, locked_until = NULL, password_updated_at = %s, "
        "       updated_at = %s WHERE id = %s",
        (password_hash, 1 if must_change else 0, now, now, int(user_id)),
    )


def record_login_success(cur: Any, user_id: int, now: datetime,
                         ip: str) -> None:
    """登录成功：清零失败计数、清锁、记录时间与 IP。"""
    cur.execute(
        "UPDATE admin_user SET failed_count = 0, locked_until = NULL, "
        "       last_login_at = %s, last_login_ip = %s, updated_at = %s "
        "WHERE id = %s",
        (now, str(ip or "")[:45], now, int(user_id)),
    )


def record_login_failed(cur: Any, user_id: int, now: datetime,
                        max_failed: int, lock_minutes: int) -> Dict[str, Any]:
    """登录失败：累加计数，达到阈值则锁定。

    Returns:
        ``{"failed_count": n, "locked_until": datetime|None}``。
    """
    cur.execute("SELECT failed_count FROM admin_user WHERE id = %s FOR UPDATE",
                (int(user_id),))
    row = cur.fetchone() or {}
    failed = to_int(row.get("failed_count")) + 1
    locked_until: Optional[datetime] = None
    if failed >= int(max_failed):
        locked_until = now + timedelta(minutes=int(lock_minutes))
    cur.execute(
        "UPDATE admin_user SET failed_count = %s, locked_until = %s, "
        "       updated_at = %s WHERE id = %s",
        (failed, locked_until, now, int(user_id)),
    )
    return {"failed_count": failed, "locked_until": locked_until}


def unlock(cur: Any, user_id: int, now: datetime) -> None:
    """手工解锁（运维脚本/测试用）。"""
    cur.execute("UPDATE admin_user SET failed_count = 0, locked_until = NULL, "
                "updated_at = %s WHERE id = %s", (now, int(user_id)))


def set_status(cur: Any, user_id: int, status: int, now: datetime) -> None:
    """启用/停用账号。"""
    cur.execute("UPDATE admin_user SET status = %s, updated_at = %s "
                "WHERE id = %s", (1 if status else 0, now, int(user_id)))


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
def create_session(cur: Any, *, user_id: int, username: str, token: str,
                   csrf_token: str, ip: str, user_agent: str,
                   now: datetime) -> Dict[str, Any]:
    """新建会话（令牌只存 sha256）。"""
    cfg = get_config()
    expires_at = now + timedelta(hours=cfg.session_ttl_hours)
    cur.execute(
        "INSERT INTO admin_session "
        "(token_hash, csrf_token, user_id, username, ip, user_agent, "
        " created_at, last_active_at, expires_at, revoked_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)",
        (security.hash_token(token), str(csrf_token)[:32], int(user_id),
         str(username or "")[:32], str(ip or "")[:45],
         str(user_agent or "")[:255], now, now, expires_at),
    )
    return {
        "token": token,
        "csrf_token": csrf_token,
        "expires_at": expires_at,
        "user_id": int(user_id),
        "username": str(username or ""),
    }


def get_session_by_token(cur: Any, token_hash: str) -> Optional[Dict[str, Any]]:
    """按令牌哈希取会话（唯一索引精确命中）。"""
    cur.execute("SELECT * FROM admin_session WHERE token_hash = %s LIMIT 1",
                (str(token_hash or ""),))
    return cur.fetchone()


def touch_session(cur: Any, session_id: int, now: datetime,
                  new_expires_at: datetime) -> None:
    """滑动续期：更新 ``last_active_at`` 与 ``expires_at``。"""
    cur.execute("UPDATE admin_session SET last_active_at = %s, expires_at = %s "
                "WHERE id = %s", (now, new_expires_at, int(session_id)))


def revoke_session(cur: Any, token_hash: str, now: datetime) -> None:
    """吊销单个会话（登出）。"""
    cur.execute("UPDATE admin_session SET revoked_at = %s "
                "WHERE token_hash = %s AND revoked_at IS NULL",
                (now, str(token_hash or "")))


def revoke_all_sessions(cur: Any, user_id: int, now: datetime,
                        except_token_hash: str = "") -> int:
    """吊销某用户的全部会话（改密踢人），可排除当前会话。"""
    if except_token_hash:
        cur.execute("UPDATE admin_session SET revoked_at = %s "
                    "WHERE user_id = %s AND revoked_at IS NULL "
                    "AND token_hash <> %s",
                    (now, int(user_id), str(except_token_hash)))
    else:
        cur.execute("UPDATE admin_session SET revoked_at = %s "
                    "WHERE user_id = %s AND revoked_at IS NULL",
                    (now, int(user_id)))
    return int(cur.rowcount or 0)


def cleanup_expired(cur: Any, now: datetime) -> int:
    """删除已过期/已吊销的会话（启动时执行）。"""
    cur.execute("DELETE FROM admin_session "
                "WHERE expires_at < %s OR revoked_at IS NOT NULL", (now,))
    return int(cur.rowcount or 0)


def list_sessions(cur: Any, user_id: int, now: datetime
                  ) -> List[Dict[str, Any]]:
    """某用户的有效会话列表（运维查看用）。"""
    cur.execute(
        "SELECT id, ip, user_agent, created_at, last_active_at, expires_at "
        "FROM admin_session WHERE user_id = %s AND revoked_at IS NULL "
        "AND expires_at > %s ORDER BY id DESC",
        (int(user_id), now),
    )
    return list(cur.fetchall() or [])
