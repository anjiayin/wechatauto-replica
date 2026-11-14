# -*- coding: utf-8 -*-
"""统计查询（MySQL 5.6 兼容写法）。

统一约定：
* ``now`` 由 Python 传入（东八区 naive），SQL 里**不出现** ``NOW()``/``CURDATE()``；
* ``SUM(...)`` 在 PyMySQL 里返回 ``Decimal`` → 一律 ``to_int()`` 归一；
* 5.6 没有日期序列 → 近 N 天趋势由 Python 生成连续日期补 0。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List

from app.db import to_int
from app.repo import codes

#: 摘要表最多返回多少组（按手机号/客户名聚合）
_GROUP_LIMIT = 20
#: 最近激活列表条数
_RECENT_LIMIT = 10


def license_stats(cur: Any, now: datetime) -> Dict[str, int]:
    """一次全表扫描拿全部许可证计数（设计文档 §6.2）。"""
    week_ago = now - timedelta(days=7)
    in_30d = now + timedelta(days=30)
    cur.execute(
        "SELECT "
        "  COUNT(*) AS total, "
        "  SUM(CASE WHEN current_key IS NOT NULL AND expires_at >  %s "
        "           THEN 1 ELSE 0 END) AS active, "
        "  SUM(CASE WHEN current_key IS NOT NULL AND expires_at <= %s "
        "           THEN 1 ELSE 0 END) AS expired, "
        "  SUM(CASE WHEN revoked_at IS NOT NULL THEN 1 ELSE 0 END) AS revoked, "
        "  SUM(CASE WHEN revoked_at IS NULL AND superseded_at IS NOT NULL "
        "           THEN 1 ELSE 0 END) AS superseded, "
        "  SUM(CASE WHEN current_key IS NOT NULL AND expires_at > %s "
        "                AND last_seen_at >= %s THEN 1 ELSE 0 END) AS active_7d, "
        "  SUM(CASE WHEN current_key IS NOT NULL AND expires_at > %s "
        "                AND expires_at <= %s THEN 1 ELSE 0 END) AS expiring_30d, "
        "  COUNT(DISTINCT machine_code) AS machines_total "
        "FROM license",
        (now, now, now, week_ago, now, in_30d),
    )
    row = cur.fetchone() or {}
    return {
        "total": to_int(row.get("total")),
        "active": to_int(row.get("active")),
        "expired": to_int(row.get("expired")),
        "revoked": to_int(row.get("revoked")),
        "superseded": to_int(row.get("superseded")),
        "active_7d": to_int(row.get("active_7d")),
        "expiring_30d": to_int(row.get("expiring_30d")),
        "machines_total": to_int(row.get("machines_total")),
    }


def group_by(cur: Any, column: str, now: datetime,
             limit: int = _GROUP_LIMIT) -> List[Dict[str, Any]]:
    """按手机号或客户名聚合（``column`` 只允许代码内的字面量）。"""
    if column not in ("customer_phone", "customer_name"):
        raise ValueError(f"不支持的聚合列：{column}")
    sql = (
        f"SELECT {column} AS group_key, "
        "       MAX(customer_name) AS customer_name, "
        "       COUNT(*) AS machine_total, "
        "       SUM(CASE WHEN current_key IS NOT NULL AND expires_at > %s "
        "                THEN 1 ELSE 0 END) AS active_count, "
        "       SUM(CASE WHEN current_key IS NOT NULL AND expires_at <= %s "
        "                THEN 1 ELSE 0 END) AS expired_count, "
        "       SUM(CASE WHEN revoked_at IS NOT NULL THEN 1 ELSE 0 END) "
        "           AS revoked_count, "
        "       MAX(expires_at) AS max_expires_at, "
        "       MAX(last_seen_at) AS last_seen_at "
        f"FROM license WHERE {column} <> '' "
        f"GROUP BY {column} "
        "ORDER BY active_count DESC, max_expires_at DESC LIMIT %s"
    )
    cur.execute(sql, (now, now, int(limit)))
    rows: List[Dict[str, Any]] = []
    for row in cur.fetchall() or []:
        rows.append({
            "key": str(row.get("group_key") or ""),
            "customer_name": str(row.get("customer_name") or ""),
            "machine_total": to_int(row.get("machine_total")),
            "active_count": to_int(row.get("active_count")),
            "expired_count": to_int(row.get("expired_count")),
            "revoked_count": to_int(row.get("revoked_count")),
            "max_expires_at": row.get("max_expires_at"),
            "last_seen_at": row.get("last_seen_at"),
        })
    return rows


def _daily_counts(cur: Any, table: str, since: datetime) -> Dict[date, int]:
    """按天统计新增数量（``table`` 只允许代码内字面量）。"""
    if table not in ("license_code", "license"):
        raise ValueError(f"不支持的表：{table}")
    cur.execute(
        f"SELECT DATE(created_at) AS d, COUNT(*) AS c FROM {table} "
        "WHERE created_at >= %s GROUP BY DATE(created_at)",
        (since,),
    )
    result: Dict[date, int] = {}
    for row in cur.fetchall() or []:
        day = row.get("d")
        if isinstance(day, datetime):
            day = day.date()
        if isinstance(day, date):
            result[day] = to_int(row.get("c"))
    return result


def trend(cur: Any, now: datetime, days: int = 30) -> List[Dict[str, Any]]:
    """近 N 天每日新增（激活码 + 许可证），缺失日期补 0。"""
    span = max(1, min(int(days), 90))
    day_list = [(now - timedelta(days=i)).date()
                for i in range(span - 1, -1, -1)]
    since = datetime.combine(day_list[0], datetime.min.time())
    code_counts = _daily_counts(cur, "license_code", since)
    license_counts = _daily_counts(cur, "license", since)
    return [{
        "date": day.isoformat(),
        "codes": code_counts.get(day, 0),
        "licenses": license_counts.get(day, 0),
    } for day in day_list]


def recent_activations(cur: Any, limit: int = _RECENT_LIMIT
                       ) -> List[Dict[str, Any]]:
    """最近 N 条新签发（按主键倒序）。"""
    cur.execute(
        "SELECT id, license_id, machine_code, customer_name, customer_phone, "
        "       edition, expires_at, created_at, current_key "
        "FROM license ORDER BY id DESC LIMIT %s", (int(limit),))
    rows: List[Dict[str, Any]] = []
    for row in cur.fetchall() or []:
        rows.append({
            "id": to_int(row.get("id")),
            "license_id": str(row.get("license_id") or ""),
            "machine_code": str(row.get("machine_code") or ""),
            "customer_name": str(row.get("customer_name") or ""),
            "customer_phone": str(row.get("customer_phone") or ""),
            "edition": str(row.get("edition") or ""),
            "expires_at": row.get("expires_at"),
            "created_at": row.get("created_at"),
            "current": row.get("current_key") is not None,
        })
    return rows


def dashboard(cur: Any, now: datetime, days: int = 30) -> Dict[str, Any]:
    """仪表盘全部指标（一次性组装，供 B1 直接返回）。"""
    span = max(7, min(int(days), 90))
    code_stats = codes.count_stats(cur, now, days=span)
    lic_stats = license_stats(cur, now)
    return {
        "codes": code_stats,
        "licenses": {
            "total": lic_stats["total"],
            "active": lic_stats["active"],
            "expired": lic_stats["expired"],
            "revoked": lic_stats["revoked"],
            "superseded": lic_stats["superseded"],
        },
        "machines": {
            "total": lic_stats["machines_total"],
            "active_7d": lic_stats["active_7d"],
        },
        "expiring_30d": lic_stats["expiring_30d"],
        "by_phone": group_by(cur, "customer_phone", now),
        "by_customer": group_by(cur, "customer_name", now),
        "trend": trend(cur, now, span),
        "recent_activations": recent_activations(cur),
        "days": span,
        "generated_at": now,
    }
