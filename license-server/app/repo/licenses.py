# -*- coding: utf-8 -*-
"""许可证数据访问。

**核心不变量 INV-1**：一台机器任意时刻最多一条 ``current_key`` 非空的许可证。
实现靠 ``UNIQUE KEY uk_current_machine(current_key)``（MySQL 唯一索引允许多个
NULL，等价于"部分唯一索引"）：

| 操作 | current_key |
|---|---|
| 新建（首次激活 / 吊销后重发） | = machine_code |
| 续期 / 缩期 / 设到期日 | 不变 |
| 吊销 | NULL |
| 恢复 | = machine_code（冲突 → 409 already_active） |

INV-2：许可证只新增不删除，吊销只写 ``revoked_at``。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pymysql

from app.db import to_int
from app.errors import ApiError
from app.repo import like_pattern

#: 查询列（避免 SELECT * 把列顺序带进业务）
_COLUMNS = (
    "id, license_id, machine_code, customer_id, customer_name, customer_phone, "
    "edition, activation_code, issued_at, expires_at, grace_days, features, "
    "revoked_at, superseded_at, reissued_from, last_seen_at, app_version, os, "
    "note, current_key, created_at, updated_at"
)


def _one(cur: Any, sql: str, params: Tuple[Any, ...]) -> Optional[Dict[str, Any]]:
    """执行查询并返回第一行。"""
    cur.execute(sql, params)
    return cur.fetchone()


def find_current(cur: Any, machine_code: str) -> Optional[Dict[str, Any]]:
    """该机器当前生效的许可证（``current_key = machine_code``）。"""
    return _one(cur, f"SELECT {_COLUMNS} FROM license WHERE current_key = %s "
                     "LIMIT 1", (machine_code,))


def lock_current(cur: Any, machine_code: str) -> Optional[Dict[str, Any]]:
    """同 :func:`find_current`，但加行锁（激活时串行化同机并发）。"""
    return _one(cur, f"SELECT {_COLUMNS} FROM license WHERE current_key = %s "
                     "LIMIT 1 FOR UPDATE", (machine_code,))


def find_latest(cur: Any, machine_code: str) -> Optional[Dict[str, Any]]:
    """该机器最新一条记录（``ORDER BY id DESC LIMIT 1``，覆盖索引 idx_machine_id）。"""
    return _one(cur, f"SELECT {_COLUMNS} FROM license WHERE machine_code = %s "
                     "ORDER BY id DESC LIMIT 1", (machine_code,))


def find_by_license_id(cur: Any, license_id: str) -> Optional[Dict[str, Any]]:
    """按许可证号取一条。"""
    return _one(cur, f"SELECT {_COLUMNS} FROM license WHERE license_id = %s "
                     "LIMIT 1", (str(license_id or "").strip(),))


def find_for_update(cur: Any, license_id: str) -> Optional[Dict[str, Any]]:
    """按许可证号取一条并加行锁（管理端写操作用）。"""
    return _one(cur, f"SELECT {_COLUMNS} FROM license WHERE license_id = %s "
                     "LIMIT 1 FOR UPDATE", (str(license_id or "").strip(),))


def has_other_active(cur: Any, machine_code: str,
                     exclude_license_id: str = "") -> Optional[Dict[str, Any]]:
    """该机器是否已有**另一条**生效许可证（恢复时冲突检测用）。"""
    if exclude_license_id:
        return _one(cur, f"SELECT {_COLUMNS} FROM license "
                         "WHERE current_key = %s AND license_id <> %s LIMIT 1",
                    (machine_code, str(exclude_license_id)))
    return _one(cur, f"SELECT {_COLUMNS} FROM license WHERE current_key = %s "
                     "LIMIT 1", (machine_code,))


def insert(cur: Any, *, license_id: str, machine_code: str, issued_at: datetime,
           expires_at: datetime, customer_id: int = 0, customer_name: str = "",
           customer_phone: str = "", edition: str = "standard",
           activation_code: str = "", grace_days: int = 7,
           features: str = "monitor,reply,ai", reissued_from: str = "",
           last_seen_at: Optional[datetime] = None, app_version: str = "",
           os_name: str = "", note: str = "",
           now: Optional[datetime] = None) -> Dict[str, Any]:
    """新建许可证（``current_key`` 必须写机器码）。

    Raises:
        ApiError: 1062 唯一键冲突（同机已有生效证）→ 409 ``conflict``。
    """
    moment = now or issued_at
    sql = (
        "INSERT INTO license "
        "(license_id, machine_code, customer_id, customer_name, customer_phone, "
        " edition, activation_code, issued_at, expires_at, grace_days, features, "
        " revoked_at, superseded_at, reissued_from, last_seen_at, app_version, "
        " os, note, current_key, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "        NULL, NULL, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    params = (
        str(license_id)[:32], str(machine_code)[:40], int(customer_id),
        str(customer_name or "")[:64], str(customer_phone or "")[:32],
        str(edition or "standard")[:32], str(activation_code or "")[:32],
        issued_at, expires_at, int(grace_days), str(features or "")[:255],
        str(reissued_from or "")[:32], last_seen_at,
        str(app_version or "")[:32], str(os_name or "")[:64],
        str(note or "")[:255], str(machine_code)[:40], moment, moment,
    )
    try:
        cur.execute(sql, params)
    except pymysql.err.IntegrityError as exc:
        text = str(exc)
        if "1062" in text:
            # 同机已有 current_key 非空的记录：数据库兜底拦住了 INV-1 违规
            raise ApiError("conflict",
                           "该机器码正在被激活（并发冲突），请 10 秒后重试",
                           409) from exc
        raise
    return find_by_license_id(cur, license_id) or {}


def update_for_renew(cur: Any, rec_id: int, expires_at: datetime, code: str,
                     customer: Dict[str, Any], item: Dict[str, Any],
                     now: datetime) -> None:
    """续期（激活时带新码）：只改到期日、激活码与客户快照，``issued_at`` 不动。"""
    cur.execute(
        "UPDATE license SET expires_at = %s, activation_code = %s, "
        "       customer_id = %s, customer_name = %s, customer_phone = %s, "
        "       edition = %s, updated_at = %s "
        "WHERE id = %s",
        (expires_at, str(code)[:32], int(customer.get("id") or 0),
         str(customer.get("name") or item.get("customer_name") or "")[:64],
         str(customer.get("phone") or item.get("customer_phone") or "")[:32],
         str(item.get("edition") or "standard")[:32], now, int(rec_id)),
    )


def set_expiry(cur: Any, rec_id: int, expires_at: datetime,
               now: datetime) -> None:
    """只改到期日（延期 / 缩期 / 直接设到期日）。"""
    cur.execute("UPDATE license SET expires_at = %s, updated_at = %s "
                "WHERE id = %s", (expires_at, now, int(rec_id)))


def set_customer(cur: Any, rec_id: int, *, customer_id: int, name: str,
                 phone: str, now: datetime) -> None:
    """补录/修改许可证的客户绑定快照（不触碰授权状态与 current_key）。"""
    cur.execute(
        "UPDATE license SET customer_id = %s, customer_name = %s, "
        "customer_phone = %s, updated_at = %s WHERE id = %s",
        (int(customer_id), str(name or "")[:64], str(phone or "")[:32],
         now, int(rec_id)),
    )


def touch_seen(cur: Any, rec_id: int, now: datetime,
               app_version: str = "") -> None:
    """刷新最近心跳时间与客户端版本（成功心跳才调用）。"""
    cur.execute("UPDATE license SET last_seen_at = %s, app_version = %s, "
                "updated_at = %s WHERE id = %s",
                (now, str(app_version or "")[:32], now, int(rec_id)))


def revoke(cur: Any, rec_id: int, now: datetime) -> None:
    """吊销：写 ``revoked_at`` 并把 ``current_key`` 置 NULL（释放唯一约束）。"""
    cur.execute("UPDATE license SET revoked_at = %s, current_key = NULL, "
                "updated_at = %s WHERE id = %s", (now, now, int(rec_id)))


def restore(cur: Any, rec_id: int, machine_code: str, now: datetime) -> None:
    """恢复：清 ``revoked_at`` 并重新占用 ``current_key``。

    Raises:
        ApiError: 同机已有生效证（1062）→ 409 ``already_active``。
    """
    try:
        cur.execute("UPDATE license SET revoked_at = NULL, current_key = %s, "
                    "updated_at = %s WHERE id = %s",
                    (str(machine_code)[:40], now, int(rec_id)))
    except pymysql.err.IntegrityError as exc:
        if "1062" in str(exc):
            raise ApiError("already_active",
                           "该机器码已有生效授权，无需恢复", 409) from exc
        raise


def supersede(cur: Any, rec_id: int, now: datetime) -> None:
    """把旧证标记为"已被取代"（防御性路径，正常业务不产生）。"""
    cur.execute("UPDATE license SET superseded_at = %s, current_key = NULL, "
                "updated_at = %s WHERE id = %s", (now, now, int(rec_id)))


# ---------------------------------------------------------------------------
# 列表 / 历史
# ---------------------------------------------------------------------------
_STATUS_SQL: Dict[str, str] = {
    "current": "current_key IS NOT NULL",
    "active": "current_key IS NOT NULL AND expires_at > %s",
    "expired": "current_key IS NOT NULL AND expires_at <= %s",
    "revoked": "revoked_at IS NOT NULL",
    "superseded": "revoked_at IS NULL AND superseded_at IS NOT NULL",
}


def _build_where(keyword: str, status: str, phone: str,
                 now: datetime) -> Tuple[str, List[Any]]:
    """构造许可证列表的 WHERE 与参数（全部参数化）。"""
    text = str(keyword or "").strip()
    like = like_pattern(text)
    conds = ["(%s = '' OR machine_code LIKE %s OR customer_name LIKE %s "
             " OR customer_phone LIKE %s OR license_id LIKE %s)"]
    params: List[Any] = [text, like, like, like, like]
    phone_text = str(phone or "").strip()
    if phone_text:
        conds.append("customer_phone LIKE %s")
        params.append(like_pattern(phone_text))
    fragment = _STATUS_SQL.get(str(status or "all"))
    if fragment:
        conds.append(f"({fragment})")
        params.extend([now] * fragment.count("%s"))
    return " AND ".join(conds), params


def list_page(cur: Any, *, keyword: str, status: str, phone: str, page: int,
              size: int, now: datetime) -> Tuple[List[Dict[str, Any]], int]:
    """许可证列表（分页）+ 总数（5.6 无窗口函数，总数单独 COUNT）。"""
    where, params = _build_where(keyword, status, phone, now)
    cur.execute(f"SELECT COUNT(*) AS total FROM license WHERE {where}",
                tuple(params))
    total = to_int((cur.fetchone() or {}).get("total"))

    sql = (f"SELECT {_COLUMNS} FROM license WHERE {where} "
           "ORDER BY id DESC LIMIT %s OFFSET %s")
    cur.execute(sql, tuple(params + [int(size), int((page - 1) * size)]))
    return list(cur.fetchall() or []), total


def history_by_machine(cur: Any, machine_code: str,
                       limit: int = 50) -> List[Dict[str, Any]]:
    """该机器的全部许可证历史（新的在前）。"""
    cur.execute(f"SELECT {_COLUMNS} FROM license WHERE machine_code = %s "
                "ORDER BY id DESC LIMIT %s", (machine_code, int(limit)))
    return list(cur.fetchall() or [])


def active_count_for_machines(cur: Any, machine_codes: List[str],
                              now: datetime) -> Dict[str, int]:
    """批量查询多台机器的生效证数量（列表标注用，避免 N+1）。"""
    if not machine_codes:
        return {}
    placeholders = ", ".join(["%s"] * len(machine_codes))
    sql = (f"SELECT machine_code, COUNT(*) AS c FROM license "
           f"WHERE current_key IS NOT NULL AND machine_code IN ({placeholders}) "
           "GROUP BY machine_code")
    cur.execute(sql, tuple(machine_codes))
    result: Dict[str, int] = {}
    for row in cur.fetchall() or []:
        result[str(row.get("machine_code"))] = to_int(row.get("c"))
    return result


def annotate_role(row: Dict[str, Any], now: datetime,
                  current_owner: Optional[str] = None) -> Dict[str, Any]:
    """给列表行补 ``role``（当前生效/已过期/已吊销/已被取代）与 ``can_restore``。

    语义与参考实现 ``annotate_licenses`` 一致，额外把"生效但过期"细分为
    ``expired``（现场最常见的误判点：过期证仍占着 ``current_key``）。
    """
    out = dict(row)
    revoked = row.get("revoked_at")
    current_key = row.get("current_key")
    expires = row.get("expires_at")
    if revoked:
        out["role"] = "revoked"
        out["can_restore"] = current_owner is None
    elif current_key is None:
        out["role"] = "superseded"
        out["can_restore"] = False
    elif expires is not None and expires <= now:
        out["role"] = "expired"
        out["can_restore"] = False
    else:
        out["role"] = "active"
        out["can_restore"] = False
    return out
