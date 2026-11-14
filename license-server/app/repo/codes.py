# -*- coding: utf-8 -*-
"""激活码数据访问。

不变量（INV-3）：激活码只新增不删除；``used_machine`` 一旦非空永不变回空
（本版不提供"重置激活码"，售后正确做法是发新码）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app import signing
from app.db import to_int
from app.errors import ApiError
from app.repo import customers, like_pattern

#: 生成激活码时的防撞重试次数
_MAX_CODE_RETRY = 5

_STATUS_UNUSED = "unused"
_STATUS_USED = "used"
_STATUS_VOIDED = "voided"


def _code_exists(cur: Any, code: str) -> bool:
    """判断激活码是否已存在。"""
    cur.execute("SELECT id FROM license_code WHERE code = %s LIMIT 1", (code,))
    return cur.fetchone() is not None


def new_unique_code(cur: Any) -> str:
    """生成一个库内不存在的激活码（最多试 5 次）。"""
    for _ in range(_MAX_CODE_RETRY):
        code = signing.new_activation_code()
        if not _code_exists(cur, code):
            return code
    raise ApiError("internal_error", "生成激活码失败（随机冲突），请重试", 500)


def batch_create(cur: Any, *, count: int, days: int, edition: str,
                 customer_name: str, customer_phone: str, note: str,
                 batch: str, created_by: str,
                 now: datetime) -> Dict[str, Any]:
    """批量生成激活码。

    Returns:
        ``{"batch": 批次号, "items": [ {...}, ... ]}``。
    """
    total = max(1, min(int(count), 500))
    valid_days = max(1, min(int(days), 3650))
    batch_no = str(batch or "").strip()[:32] or signing.new_batch_no(now)
    name = str(customer_name or "").strip()[:64]
    phone = customers.normalize_phone(customer_phone)
    edition_text = str(edition or "").strip()[:32] or "standard"
    note_text = str(note or "").strip()[:255]
    actor = str(created_by or "")[:32]

    # 有手机号 → 建/更新客户档案，拿 customer_id（无手机号则不建档）
    profile = customers.upsert_by_phone(cur, phone, name, now)

    items: List[Dict[str, Any]] = []
    for _ in range(total):
        code = new_unique_code(cur)
        cur.execute(
            "INSERT INTO license_code "
            "(code, days, edition, customer_id, customer_name, customer_phone, "
            " batch, note, used_machine, used_at, created_by, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '', NULL, %s, %s)",
            (code, valid_days, edition_text, profile["id"], name, phone,
             batch_no, note_text, actor, now),
        )
        items.append({
            "code": code,
            "days": valid_days,
            "edition": edition_text,
            "customer_id": profile["id"],
            "customer_name": name,
            "customer_phone": phone,
            "batch": batch_no,
            "note": note_text,
            "used_machine": "",
            "used_at": None,
            "created_by": actor,
            "created_at": now,
        })
    return {"batch": batch_no, "items": items}


def find_by_code(cur: Any, code: str) -> Optional[Dict[str, Any]]:
    """按激活码查询（不加锁）。"""
    cur.execute("SELECT * FROM license_code WHERE code = %s LIMIT 1",
                (signing.normalize_code(code),))
    return cur.fetchone()


def find_by_code_for_update(cur: Any, code: str) -> Optional[Dict[str, Any]]:
    """按激活码查询并加行锁（防并发重复兑换同一码）。"""
    cur.execute("SELECT * FROM license_code WHERE code = %s LIMIT 1 FOR UPDATE",
                (signing.normalize_code(code),))
    return cur.fetchone()


def mark_used(cur: Any, code: str, machine_code: str,
              now: datetime) -> None:
    """标记激活码已被某台机器使用（已用则不改回空；已作废的码不可用）。"""
    cur.execute(
        "UPDATE license_code SET used_machine = %s, used_at = %s "
        "WHERE code = %s AND used_machine = '' AND voided_at IS NULL",
        (str(machine_code)[:40], now, signing.normalize_code(code)),
    )


def void(cur: Any, code: str, now: datetime) -> int:
    """作废未使用的激活码。

    Returns:
        受影响行数（0 = 码不存在 / 已使用 / 已作废，语义由服务层区分）。
    """
    cur.execute(
        "UPDATE license_code SET voided_at = %s "
        "WHERE code = %s AND used_machine = '' AND voided_at IS NULL",
        (now, signing.normalize_code(code)),
    )
    return int(getattr(cur, "rowcount", 0) or 0)


def _where(keyword: str, status: str, phone: str,
           batch: str) -> Tuple[str, List[Any]]:
    """构造列表查询的 WHERE 子句与参数（全部参数化）。"""
    text = str(keyword or "").strip()
    like = like_pattern(text)
    conds = ["(%s = '' OR code LIKE %s OR customer_name LIKE %s "
             " OR customer_phone LIKE %s OR batch LIKE %s)"]
    params: List[Any] = [text, like, like, like, like]
    if status == _STATUS_UNUSED:
        conds.append("used_machine = '' AND voided_at IS NULL")
    elif status == _STATUS_USED:
        conds.append("used_machine <> ''")
    elif status == _STATUS_VOIDED:
        conds.append("voided_at IS NOT NULL")
    phone_text = customers.normalize_phone(phone)
    if phone_text:
        conds.append("customer_phone LIKE %s")
        params.append(like_pattern(phone_text))
    batch_text = str(batch or "").strip()
    if batch_text:
        conds.append("batch LIKE %s")
        params.append(like_pattern(batch_text))
    return " AND ".join(conds), params


def list_page(cur: Any, *, keyword: str, status: str, phone: str,
              batch: str, page: int, size: int
              ) -> Tuple[List[Dict[str, Any]], int]:
    """激活码列表（分页）+ 总数。"""
    where, params = _where(keyword, status, phone, batch)
    cur.execute(f"SELECT COUNT(*) AS total FROM license_code WHERE {where}",
                tuple(params))
    total = to_int((cur.fetchone() or {}).get("total"))

    sql = (
        "SELECT id, code, days, edition, customer_id, customer_name, "
        "       customer_phone, batch, note, used_machine, used_at, "
        "       voided_at, created_by, created_at "
        f"FROM license_code WHERE {where} "
        "ORDER BY id DESC LIMIT %s OFFSET %s"
    )
    cur.execute(sql, tuple(params + [int(size), int((page - 1) * size)]))
    rows = list(cur.fetchall() or [])
    return [_row_out(row) for row in rows], total


def export_rows(cur: Any, *, keyword: str, status: str, phone: str,
                batch: str, limit: int = 20000) -> List[Dict[str, Any]]:
    """导出激活码完整行（与列表同一套筛选条件，供 CSV 下载）。"""
    where, params = _where(keyword, status, phone, batch)
    cur.execute(f"SELECT id, code, days, edition, customer_name, "
                "       customer_phone, batch, note, used_machine, used_at, "
                "       voided_at, created_by, created_at "
                f"FROM license_code WHERE {where} "
                "ORDER BY id DESC LIMIT %s",
                tuple(params + [int(limit)]))
    return list(cur.fetchall() or [])


def count_stats(cur: Any, now: datetime, days: int = 30) -> Dict[str, int]:
    """激活码统计（总/已用/未用/已作废/近 N 天新增）。"""
    since = now - timedelta(days=int(days))
    cur.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN used_machine <> '' THEN 1 ELSE 0 END) AS used, "
        "       SUM(CASE WHEN used_machine = '' AND voided_at IS NULL "
        "                THEN 1 ELSE 0 END) AS unused, "
        "       SUM(CASE WHEN voided_at IS NOT NULL THEN 1 ELSE 0 END) "
        "            AS voided, "
        "       SUM(CASE WHEN created_at >= %s THEN 1 ELSE 0 END) AS recent "
        "FROM license_code",
        (since,),
    )
    row = cur.fetchone() or {}
    return {
        "total": to_int(row.get("total")),
        "used": to_int(row.get("used")),
        "unused": to_int(row.get("unused")),
        "voided": to_int(row.get("voided")),
        "new_30d": to_int(row.get("recent")),
    }


def _row_out(row: Dict[str, Any]) -> Dict[str, Any]:
    """把数据库行整理成接口输出（字段名与文档一致）。"""
    return {
        "id": to_int(row.get("id")),
        "code": str(row.get("code") or ""),
        "days": to_int(row.get("days")),
        "edition": str(row.get("edition") or ""),
        "customer_id": to_int(row.get("customer_id")),
        "customer_name": str(row.get("customer_name") or ""),
        "customer_phone": str(row.get("customer_phone") or ""),
        "batch": str(row.get("batch") or ""),
        "note": str(row.get("note") or ""),
        "used_machine": str(row.get("used_machine") or ""),
        "used_at": row.get("used_at"),
        "voided_at": row.get("voided_at"),
        "created_by": str(row.get("created_by") or ""),
        "created_at": row.get("created_at"),
    }
