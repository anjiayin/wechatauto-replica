# -*- coding: utf-8 -*-
"""D 组：统计正确性（最容易出错的一组）。

对齐依据：``docs/design.md`` §6.1 指标定义 / §6.2 统计 SQL / §6.4 趋势补 0。
做法：**手工造一批已知数字**，再拿 dashboard 的每个计数与手算结果逐一比对
（不用工程师的临时脚本，也不用接口自己算出来的数当预期）。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import _helpers as H

PHONE_A = "13900000001"
PHONE_B = "13900000002"


def _machine(i: int) -> str:
    """造一个合法的 32 位十六进制机器码。"""
    return "%04X-0000-0000-0000-0000-0000-0000-%04X" % (i, i)


def _build_dataset() -> dict:
    """20 个码（8 已用）+ 10 张证（4 生效 / 2 过期 / 3 吊销 / 1 被取代）。"""
    now = H.signing.now()

    # --- 20 个激活码：8 个已用，全部今天创建 ---
    for i in range(1, 21):
        used = i <= 8
        H.insert_code(code="KF-D-%04d" % i, days=365, customer_name="甲",
                      customer_phone=PHONE_A,
                      used_machine=_machine(i) if used else "",
                      used_at=now if used else None)

    # --- 4 张生效证 ---
    H.insert_license(license_id="LIC-D-A1", machine_code=_machine(1),
                     customer_name="甲", customer_phone=PHONE_A,
                     expires_at=now + timedelta(days=365),
                     last_seen_at=now - timedelta(hours=1))
    H.insert_license(license_id="LIC-D-A2", machine_code=_machine(2),
                     customer_name="甲", customer_phone=PHONE_A,
                     expires_at=now + timedelta(days=200),
                     last_seen_at=now - timedelta(days=2))
    H.insert_license(license_id="LIC-D-A3", machine_code=_machine(3),
                     customer_name="甲", customer_phone=PHONE_A,
                     expires_at=now + timedelta(days=100),
                     last_seen_at=now - timedelta(days=30))
    H.insert_license(license_id="LIC-D-A4", machine_code=_machine(4),
                     customer_name="甲", customer_phone=PHONE_A,
                     expires_at=now + timedelta(days=10),
                     last_seen_at=now - timedelta(days=30))

    # --- 2 张过期（仍占着 current_key）---
    for idx in (5, 6):
        H.insert_license(license_id="LIC-D-E%d" % idx, machine_code=_machine(idx),
                         customer_name="甲", customer_phone=PHONE_A,
                         expires_at=now - timedelta(days=1),
                         last_seen_at=now - timedelta(days=30))

    # --- 3 张吊销（current_key 置空）---
    for idx in (7, 8, 9):
        H.insert_license(license_id="LIC-D-R%d" % idx, machine_code=_machine(idx),
                         customer_name="乙", customer_phone=PHONE_B,
                         revoked_at=now - timedelta(days=1),
                         expires_at=now + timedelta(days=100),
                         last_seen_at=now - timedelta(days=10))

    # --- 1 张被取代（防御性状态）---
    H.insert_license(license_id="LIC-D-S10", machine_code=_machine(10),
                     customer_name="乙", customer_phone=PHONE_B,
                     superseded_at=now - timedelta(days=1),
                     expires_at=now + timedelta(days=100),
                     last_seen_at=now - timedelta(days=10))

    return {
        "codes": {"total": 20, "used": 8, "unused": 12, "new_30d": 20},
        "licenses": {"total": 10, "active": 4, "expired": 2, "revoked": 3,
                     "superseded": 1},
        "machines": {"total": 10, "active_7d": 2},
        "expiring_30d": 1,
    }


def test_d01_dashboard_counts_match_hand_calc(admin):
    """D1：dashboard 每个计数与手算完全一致（含 Decimal→int 归一）。"""
    expected = _build_dataset()
    resp = admin.get("/admin/api/dashboard")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    for group in ("codes", "licenses", "machines"):
        for key, want in expected[group].items():
            got = data[group][key]
            assert isinstance(got, int), "%s.%s 不是 int：%r" % (group, key, got)
            assert got == want, "%s.%s 预期 %s，实际 %s" % (group, key, want, got)

    assert data["expiring_30d"] == expected["expiring_30d"], data["expiring_30d"]
    assert data.get("ok") is True
    assert data.get("days") == 30


def test_d01_dashboard_group_by_phone(admin):
    """D1-b：按手机号聚合的数字与手算一致。"""
    _build_dataset()
    data = admin.get("/admin/api/dashboard").json()
    groups = {row["key"]: row for row in data["by_phone"]}
    assert set(groups) == {PHONE_A, PHONE_B}, groups.keys()

    a = groups[PHONE_A]
    assert a["machine_total"] == 6, a
    assert a["active_count"] == 4, a
    assert a["expired_count"] == 2, a
    assert a["revoked_count"] == 0, a

    b = groups[PHONE_B]
    assert b["machine_total"] == 4, b
    assert b["active_count"] == 0, b
    assert b["revoked_count"] == 3, b


def test_d01_recent_activations(admin):
    """D1-c：最近签发列表不超过 10 条且字段完整。"""
    _build_dataset()
    data = admin.get("/admin/api/dashboard").json()
    rows = data["recent_activations"]
    assert len(rows) == 10, len(rows)
    for row in rows:
        assert row["license_id"] and row["machine_code"]
        assert H.is_iso_cn(row["expires_at"]), row
        assert isinstance(row["current"], bool)


# ---------------------------------------------------------------------------
# 趋势
# ---------------------------------------------------------------------------
def test_d02_trend_30_days_continuous(admin):
    """D2：近 30 天趋势长度 30、日期连续无缺口、无数据的天为 0。"""
    _build_dataset()
    data = admin.get("/admin/api/dashboard").json()
    trend = data["trend"]
    assert len(trend) == 30, "趋势长度 %d（期望 30）" % len(trend)

    days = [row["date"] for row in trend]
    from datetime import date as _date
    parsed = [_date.fromisoformat(d) for d in days]
    for prev, cur in zip(parsed, parsed[1:]):
        assert (cur - prev).days == 1, "趋势日期有缺口：%s → %s" % (prev, cur)

    today = H.signing.now().date()
    assert parsed[-1] == today, "趋势最后一天不是今天：%s vs %s" % (parsed[-1], today)

    # 数据全在今天造 → 只有最后一天非 0
    assert trend[-1]["codes"] == 20, trend[-1]
    assert trend[-1]["licenses"] == 10, trend[-1]
    for row in trend[:-1]:
        assert row["codes"] == 0 and row["licenses"] == 0, row


def test_d02_trend_custom_days(admin):
    """D2-b：days 参数生效（7 → 长度 7，90 → 长度 90）。"""
    _build_dataset()
    for days in (7, 45, 90):
        data = admin.get("/admin/api/dashboard", params={"days": days}).json()
        assert len(data["trend"]) == days, (days, len(data["trend"]))
        assert data["days"] == days
    # 越界拒绝
    assert admin.get("/admin/api/dashboard", params={"days": 3}).status_code == 400
    assert admin.get("/admin/api/dashboard", params={"days": 91}).status_code == 400


# ---------------------------------------------------------------------------
# 聚合 / 客户档案
# ---------------------------------------------------------------------------
def test_d03_same_phone_two_machines(admin):
    """D3：同一手机号 2 台机器 → 计数 2，且 customer 表不重复建档。"""
    codes = H.create_codes(admin, count=2, days=365,
                           customer="王老板", phone=PHONE_A)
    H.activate_ok(H.M1, codes[0])
    H.activate_ok(H.M2, codes[1])

    data = admin.get("/admin/api/dashboard").json()
    groups = {row["key"]: row for row in data["by_phone"]}
    assert PHONE_A in groups, groups
    entry = groups[PHONE_A]
    assert entry["machine_total"] == 2, entry
    assert entry["active_count"] == 2, entry

    rows = H.q("SELECT id FROM customer WHERE phone = %s", (PHONE_A,))
    assert len(rows) == 1, "同一手机号建了 %d 份档案" % len(rows)

    # 客户列表口径
    resp = admin.get("/admin/api/customers")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1, items
    assert items[0]["machine_total"] == 2, items[0]
    assert items[0]["active_count"] == 2, items[0]


# ---------------------------------------------------------------------------
# D4 口径偏差：已过期但仍占 current_key 的机器被算进 active_7d
# ---------------------------------------------------------------------------
def test_d04_expired_license_must_not_count_as_active_7d(admin):
    """D4：已过期（但 current_key 仍非空）的机器不应计入 machines.active_7d。"""
    now = H.signing.now()
    H.insert_license(license_id="LIC-X-EXPIRED", machine_code=_machine(1),
                     current_key=_machine(1),
                     expires_at=now - timedelta(days=1), last_seen_at=now)
    data = admin.get("/admin/api/dashboard").json()
    assert data["machines"]["active_7d"] == 0, data["machines"]
    assert data["machines"]["total"] == 1, data["machines"]
