# -*- coding: utf-8 -*-
"""客户档案数据访问。

规则（设计文档 §3.1）：
* ``phone`` 是业务主键（唯一索引）；
* 手机号为空**不建档**，只把名称快照留在 code/license 上（``customer_id = 0``）；
* 客户档案只 upsert，不删除（历史许可证快照依赖它）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.db import to_int
from app.repo import like_pattern


def normalize_phone(value: Any) -> str:
    """手机号归一化：去掉空格与横杠（保留 ``+`` 便于存 +86 形式）。"""
    text = str(value or "").strip()
    return text.replace(" ", "").replace("-", "")


def upsert_by_phone(cur: Any, phone: Any, name: Any,
                    now: datetime) -> Dict[str, Any]:
    """按手机号 upsert 客户档案。

    Args:
        cur: 事务游标。
        phone: 手机号（空则返回 ``{"id": 0, ...}``，**不建档**）。
        name: 客户名称（空则保留档案里已有的名字）。
        now: 当前时间（东八区 naive）。

    Returns:
        ``{"id": 客户id, "name": 名称, "phone": 手机号}``。
    """
    phone_text = normalize_phone(phone)
    name_text = str(name or "").strip()[:64]
    if not phone_text:
        return {"id": 0, "name": name_text, "phone": ""}

    cur.execute(
        "INSERT INTO customer (name, phone, note, created_at, updated_at) "
        "VALUES (%s, %s, '', %s, %s) "
        "ON DUPLICATE KEY UPDATE name = IF(%s = '', name, %s), updated_at = %s",
        (name_text, phone_text, now, now, name_text, name_text, now),
    )
    cur.execute("SELECT id, name, phone FROM customer WHERE phone = %s",
                (phone_text,))
    row = cur.fetchone()
    if not row:
        # 理论上不可达（刚插入/更新过）；保守返回无档案，不打断主流程
        return {"id": 0, "name": name_text, "phone": phone_text}
    return {"id": to_int(row.get("id")), "name": str(row.get("name") or ""),
            "phone": str(row.get("phone") or "")}


def get_by_id(cur: Any, customer_id: int) -> Optional[Dict[str, Any]]:
    """按主键取客户档案。"""
    cur.execute("SELECT * FROM customer WHERE id = %s", (int(customer_id),))
    return cur.fetchone()


def search(cur: Any, keyword: str, limit: int = 20) -> List[Dict[str, Any]]:
    """按名称/手机号模糊搜索客户（供激活码生成页自动补全）。"""
    text = str(keyword or "").strip()
    like = like_pattern(text)
    cur.execute(
        "SELECT id, name, phone FROM customer "
        "WHERE (%s = '' OR name LIKE %s OR phone LIKE %s) "
        "ORDER BY id DESC LIMIT %s",
        (text, like, like, int(limit)),
    )
    return list(cur.fetchall() or [])


def list_with_stats(cur: Any, keyword: str, page: int, size: int,
                    now: datetime) -> Tuple[List[Dict[str, Any]], int]:
    """客户列表（含机器数 / 生效数 / 最近到期 / 最近心跳）+ 总数。"""
    text = str(keyword or "").strip()
    like = like_pattern(text)
    where = "WHERE (%s = '' OR c.name LIKE %s OR c.phone LIKE %s)"
    where_params = [text, like, like]

    cur.execute(f"SELECT COUNT(*) AS total FROM customer c {where}",
                tuple(where_params))
    total_row = cur.fetchone() or {}
    total = to_int(total_row.get("total"))

    sql = (
        "SELECT c.id, c.name, c.phone, c.note, c.created_at, c.updated_at, "
        "       COUNT(l.id) AS machine_total, "
        "       SUM(CASE WHEN l.current_key IS NOT NULL AND l.expires_at > %s "
        "                THEN 1 ELSE 0 END) AS active_count, "
        "       SUM(CASE WHEN l.revoked_at IS NOT NULL THEN 1 ELSE 0 END) "
        "           AS revoked_count, "
        "       MAX(l.expires_at) AS max_expires_at, "
        "       MAX(l.last_seen_at) AS last_seen_at "
        "FROM customer c LEFT JOIN license l ON l.customer_id = c.id "
        f"{where} "
        "GROUP BY c.id, c.name, c.phone, c.note, c.created_at, c.updated_at "
        "ORDER BY c.id DESC LIMIT %s OFFSET %s"
    )
    params = [now] + where_params + [int(size), int((page - 1) * size)]
    cur.execute(sql, tuple(params))
    rows = list(cur.fetchall() or [])
    items: List[Dict[str, Any]] = []
    for row in rows:
        items.append({
            "id": to_int(row.get("id")),
            "name": str(row.get("name") or ""),
            "phone": str(row.get("phone") or ""),
            "note": str(row.get("note") or ""),
            "machine_total": to_int(row.get("machine_total")),
            "active_count": to_int(row.get("active_count")),
            "revoked_count": to_int(row.get("revoked_count")),
            "max_expires_at": row.get("max_expires_at"),
            "last_seen_at": row.get("last_seen_at"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        })
    return items, total
