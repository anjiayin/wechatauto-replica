# -*- coding: utf-8 -*-
"""审计日志数据访问（INV-5：所有管理端写操作必须写一条）。

``before_json`` / ``after_json`` 用 TEXT 存 JSON 字符串（MySQL 5.6 无 JSON 类型），
写入前统一 ``json.dumps(ensure_ascii=False, sort_keys=True)``，保证可读且稳定。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.db import to_int
from app.repo import like_pattern


def _dump(value: Optional[Dict[str, Any]]) -> Optional[str]:
    """dict → JSON 字符串（None 保持 None）。"""
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          default=str)
    except (TypeError, ValueError):
        return None


def load(value: Any) -> Optional[Dict[str, Any]]:
    """JSON 字符串 → dict（解析失败返回 None，前端原样展示兜底）。"""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        return None


def write(cur: Any, *, action: str, now: datetime, actor: str = "",
          actor_id: int = 0, target_type: str = "", target_id: str = "",
          machine_code: str = "", days_delta: int = 0,
          before: Optional[Dict[str, Any]] = None,
          after: Optional[Dict[str, Any]] = None, ip: str = "",
          note: str = "") -> None:
    """写一条审计日志。

    Args:
        action: 动作码（login/logout/change_password/create_codes/extend/
            set_expiry/revoke/restore/create_customer/activate_first/...）。
        days_delta: 本次增减天数（缩期为负；非期限类操作为 0）。
        before / after: 变更前后快照 dict（落库为 JSON 字符串）。
    """
    cur.execute(
        "INSERT INTO audit_log "
        "(actor_id, actor, action, target_type, target_id, machine_code, "
        " days_delta, before_json, after_json, ip, note, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (int(actor_id or 0), str(actor or "")[:32], str(action or "")[:32],
         str(target_type or "")[:16], str(target_id or "")[:64],
         str(machine_code or "")[:40], int(days_delta or 0),
         _dump(before), _dump(after), str(ip or "")[:45],
         str(note or "")[:255], now),
    )


def list_page(cur: Any, *, action: str, machine_code: str, keyword: str,
              page: int, size: int) -> Tuple[List[Dict[str, Any]], int]:
    """审计日志分页（倒序）。"""
    text = str(keyword or "").strip()
    like = like_pattern(text)
    conds = ["(%s = '' OR actor LIKE %s OR target_id LIKE %s "
             " OR note LIKE %s OR machine_code LIKE %s)"]
    params: List[Any] = [text, like, like, like, like]
    action_text = str(action or "").strip()
    if action_text:
        conds.append("action = %s")
        params.append(action_text)
    machine_text = str(machine_code or "").strip()
    if machine_text:
        conds.append("machine_code = %s")
        params.append(machine_text)
    where = " AND ".join(conds)

    cur.execute(f"SELECT COUNT(*) AS total FROM audit_log WHERE {where}",
                tuple(params))
    total = to_int((cur.fetchone() or {}).get("total"))

    cur.execute(
        "SELECT id, actor_id, actor, action, target_type, target_id, "
        "       machine_code, days_delta, before_json, after_json, ip, note, "
        "       created_at "
        f"FROM audit_log WHERE {where} ORDER BY id DESC LIMIT %s OFFSET %s",
        tuple(params + [int(size), int((page - 1) * size)]),
    )
    rows = list(cur.fetchall() or [])
    items: List[Dict[str, Any]] = []
    for row in rows:
        items.append({
            "id": to_int(row.get("id")),
            "actor_id": to_int(row.get("actor_id")),
            "actor": str(row.get("actor") or ""),
            "action": str(row.get("action") or ""),
            "target_type": str(row.get("target_type") or ""),
            "target_id": str(row.get("target_id") or ""),
            "machine_code": str(row.get("machine_code") or ""),
            "days_delta": to_int(row.get("days_delta")),
            "before": load(row.get("before_json")),
            "after": load(row.get("after_json")),
            "ip": str(row.get("ip") or ""),
            "note": str(row.get("note") or ""),
            "created_at": row.get("created_at"),
        })
    return items, total


def actions(cur: Any) -> List[str]:
    """已出现过的动作码（前端筛选下拉用）。"""
    cur.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")
    return [str(row.get("action") or "") for row in (cur.fetchall() or [])]
