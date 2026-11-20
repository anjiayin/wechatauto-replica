# -*- coding: utf-8 -*-
"""QA 独立验证：/api/status 新增 cache 字段的最小契约检查。

只验证契约本身（字段存在/类型/向后兼容/异常降级），不连真实服务、
不读真实微信库；依赖用替身注入，业务库用 tmp_path 临时 SQLite。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_cache_status_api.py -q
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.router_system import router as system_router
from app.db import Database
from app.notifier import Notifier
from app.schemas import CacheShardStatus, StatusOut
from app.wechat_service import WeChatService

CACHE_FIELDS = {"rel", "stale_seconds", "last_ok_at", "last_error",
                "fail_streak", "rebuilding"}


class FakeService:
    """只实现 /api/status 依赖到的接口。"""

    listener_running = True
    monitored_count = 2
    available = True
    watermark_saved_at = "2026-11-10T14:39:00"
    degraded_reason = ""

    def __init__(self, items=None, raises=False):
        self._items = items if items is not None else []
        self._raises = raises
        self.calls = 0

    def probe_wechat_window(self) -> bool:
        return False

    def cache_status(self):
        self.calls += 1
        if self._raises:
            raise RuntimeError("读取缓存健康度失败")
        return self._items


class FakePipeline:
    queue_size = 0
    queue_max = 100
    dropped_total = 0
    busy = False

    @property
    def analysis_health(self) -> dict:
        return {}


def make_client(tmp_path, svc):
    db = Database(str(tmp_path / "status.sqlite3"))
    deps.configure(db=db, wechat=svc, pipeline=FakePipeline(),
                   sender=None, notifier=Notifier())
    app = FastAPI()
    app.include_router(system_router)
    return TestClient(app), db


@pytest.fixture()
def cleanup():
    yield
    deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                   notifier=None)


def test_status_cache_field_contract(tmp_path, cleanup):
    """cache 列表字段齐全且类型正确（stale_seconds 允许 null）。"""
    item = {"rel": "message/message_8.db", "stale_seconds": 143.5,
            "last_ok_at": "2026-11-10T14:39:00",
            "last_error": "quick_check 校验失败（快照撕裂，第 3 轮）",
            "fail_streak": 3, "rebuilding": True}
    client, db = make_client(tmp_path, FakeService([item]))
    try:
        resp = client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "cache" in body, "/api/status 缺少新增 cache 字段"
        assert body["cache"] == [item]
        assert set(body["cache"][0]) == CACHE_FIELDS
        assert isinstance(body["cache"][0]["stale_seconds"], float)
        assert body["cache"][0]["rebuilding"] is True
    finally:
        db.close()


def test_status_cache_null_when_never_succeeded(tmp_path, cleanup):
    """从未成功更新时 stale_seconds 必须是 null（前端据此判断"从未成功"）。"""
    item = {"rel": "message/message_3.db", "stale_seconds": None,
            "last_ok_at": "", "last_error": "", "fail_streak": 0,
            "rebuilding": False}
    client, db = make_client(tmp_path, FakeService([item]))
    try:
        body = client.get("/api/status").json()
        assert body["cache"][0]["stale_seconds"] is None
    finally:
        db.close()


def test_status_cache_empty_is_backward_compatible(tmp_path, cleanup):
    """服务返回空列表：cache 为 []，其余既有字段不变（不破坏前端契约）。"""
    client, db = make_client(tmp_path, FakeService([]))
    try:
        body = client.get("/api/status").json()
        assert body["cache"] == []
        for key in ("listener_running", "monitored_count", "today_cost",
                    "degraded_reason", "unread_count"):
            assert key in body
    finally:
        db.close()


def test_status_survives_cache_status_exception(tmp_path, cleanup):
    """cache_status 抛异常时 /api/status 必须仍 200 且 cache 降级为空。"""
    client, db = make_client(tmp_path, FakeService(raises=True))
    try:
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["cache"] == []
    finally:
        db.close()


def test_status_skips_malformed_item_but_keeps_others(tmp_path, cleanup):
    """单个分片字段非法时跳过该条，不影响其他分片与整体 200。"""
    good = {"rel": "message/message_1.db", "stale_seconds": 1.0,
            "last_ok_at": "", "last_error": "", "fail_streak": 0,
            "rebuilding": False}
    bad = {"rel": "message/message_2.db", "stale_seconds": "不是数字"}
    client, db = make_client(tmp_path, FakeService([bad, good, "非字典"]))
    try:
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["cache"] == [good]
    finally:
        db.close()


def test_schema_defaults_and_types():
    """CacheShardStatus 默认值与 StatusOut.cache 默认空列表。"""
    shard = CacheShardStatus()
    assert shard.rel == "" and shard.stale_seconds is None
    assert shard.fail_streak == 0 and shard.rebuilding is False
    assert StatusOut().cache == []


def test_service_cache_status_normalizes_and_degrades():
    """WeChatService.cache_status 的字段归一化与异常降级（不触碰真实微信）。"""
    class FakeWxdb:
        def __init__(self, items=None, raises=False):
            self._items, self._raises = items, raises

        def cache_status(self):
            if self._raises:
                raise RuntimeError("boom")
            return self._items

    svc = WeChatService.__new__(WeChatService)  # 只测该方法的纯逻辑

    svc._wxdb = None
    assert svc.cache_status() == []

    svc._wxdb = FakeWxdb([{"rel": None, "stale_seconds": None,
                           "fail_streak": None, "rebuilding": None}, "x"])
    assert svc.cache_status() == [{
        "rel": "", "stale_seconds": None, "last_ok_at": "",
        "last_error": "", "fail_streak": 0, "rebuilding": False,
    }]

    svc._wxdb = FakeWxdb(raises=True)
    assert svc.cache_status() == []

    class NoMethod:
        pass

    svc._wxdb = NoMethod()   # 旧版 WeChatDB 无 cache_status → 降级
    assert svc.cache_status() == []


# ---------------------------------------------------------------------------
# 今日费用分项契约（商机分析/群消息总结/图片识别）
# ---------------------------------------------------------------------------
def _seed_usage(db: Database) -> None:
    """写三类用量各一条，供分项断言。"""
    db.insert_ai_usage(prompt_tokens=1000, completion_tokens=100,
                       model="m", session_username="a@chatroom",
                       usage_kind="opportunity")
    db.insert_ai_usage(prompt_tokens=2000, completion_tokens=200,
                       model="m", session_username="g@chatroom",
                       usage_kind="summary")
    db.insert_ai_usage(prompt_tokens=3000, completion_tokens=300,
                       model="m", session_username="g@chatroom",
                       usage_kind="vision")


def test_status_today_cost_breakdown_contract(tmp_path, cleanup):
    """/api/status 返回 today_cost_breakdown 且三类分项之和等于总数。"""
    from datetime import datetime as _dt
    client, db = make_client(tmp_path, FakeService())
    try:
        _seed_usage(db)
        # 配置单价（每千 tokens 输入 2 元 / 输出 8 元）让 cost > 0
        db.set_setting("billing.unit", "per_k")
        db.set_setting("billing.input_price", "2")
        db.set_setting("billing.output_price", "8")
        body = client.get("/api/status").json()
        bd = body["today_cost_breakdown"]
        assert set(bd) == {"opportunity", "summary", "vision"}
        # 1000/千×2+100/千×8=2.8；2000×2/1000+200×8/1000=5.6；3000/300 → 8.4
        assert abs(bd["opportunity"] - 2.8) < 1e-6
        assert abs(bd["summary"] - 5.6) < 1e-6
        assert abs(bd["vision"] - 8.4) < 1e-6
        assert abs(sum(bd.values()) - body["today_cost"]) < 1e-6
        assert body["today_cost"] > 0
        assert _dt.now().strftime("%Y-%m-%d")  # 口径为「今日」
    finally:
        db.close()


def test_billing_breakdown_contract(tmp_path, cleanup):
    """/api/system/billing 返回 breakdown，字段结构与 today 一致。"""
    client, db = make_client(tmp_path, FakeService())
    try:
        _seed_usage(db)
        db.set_setting("billing.unit", "per_k")
        db.set_setting("billing.input_price", "2")
        db.set_setting("billing.output_price", "8")
        body = client.get("/api/system/billing").json()
        bd = body["breakdown"]
        assert set(bd) == {"opportunity", "summary", "vision"}
        for kind, item in bd.items():
            assert set(item) == {"calls", "prompt_tokens",
                                 "completion_tokens", "cost"}
        assert bd["opportunity"]["calls"] == 1
        assert bd["summary"]["prompt_tokens"] == 2000
        assert bd["vision"]["completion_tokens"] == 300
        # 分项费用之和 == 今日总费用
        total = sum(v["cost"] for v in bd.values())
        assert abs(total - body["today"]["cost"]) < 1e-6
    finally:
        db.close()


def test_billing_breakdown_empty_when_no_usage(tmp_path, cleanup):
    """无用量时 breakdown 仍返回三类结构（全 0），前端无需判空。"""
    client, db = make_client(tmp_path, FakeService())
    try:
        body = client.get("/api/system/billing").json()
        bd = body["breakdown"]
        assert set(bd) == {"opportunity", "summary", "vision"}
        assert all(v["calls"] == 0 and v["cost"] == 0 for v in bd.values())
    finally:
        db.close()
