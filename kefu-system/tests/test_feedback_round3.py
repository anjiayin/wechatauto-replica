# -*- coding: utf-8 -*-
"""第三轮反馈专项测试（用户 11 项体验问题中的后端行为）。

覆盖：
1. 历史数据只保留手动清理（定时任务已移除）；
2. 「不使用 AI 接口」开关 + 轻量筛选直接判定商机（关键词/正则命中即商机）；
3. 商机/日志时间戳改为本地时间（含老库 UTC → 本地一次性平移）；
4. 行内未读标识（列表返回 unread + 单条已读接口 + 详情自动已读）；
5. 商机原文/上下文发送人显示名（不再出现 wxid 前缀，小程序不再透传 XML）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import analyzer
from app.aggregator import AnalysisUnit
from app.api import ALL_ROUTERS, deps
from app.content_format import format_content
from app.db import Database, read_ai_settings
from app.notifier import Notifier
from app.pipeline import Pipeline
from app.wechat_service import WeChatService


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def make_unit(text: str = "我要一箱机油", sender: str = "张三") -> AnalysisUnit:
    """构造一个分析单元（模拟合并窗口 flush 的输出）。"""
    now = datetime.now().isoformat(timespec="seconds")
    return AnalysisUnit(
        session_username="21264829018@chatroom",
        session_name="配件群",
        session_kind="group",
        sender_wxid="wxid_a",
        sender_name=sender,
        window_start=now,
        window_end=now,
        merged_count=1,
        lines=[("14:02", text)],
        first_seen_epoch=time.time(),
        enqueued_epoch=time.time(),
        enqueued_mono=time.monotonic(),
    )


def _no_ai_factory(cfg):  # pragma: no cover - 只用于断言不被调用
    raise AssertionError("关闭 AI 后不得创建/调用 AI 客户端")


def make_pipeline(db: Database, factory=None) -> Pipeline:
    return Pipeline(db, Notifier(), client_factory=factory or _no_ai_factory)


def prepare_db(**settings: str) -> Database:
    """内存业务库 + 指定配置（默认关闭 AI、给定关键词）。"""
    db = Database(":memory:")
    db.set_setting("ai.enabled", "0")
    db.set_setting("analysis.prescreen_keywords", "机油,刹车片")
    db.set_setting("analysis.prescreen_regex", "")
    for key, value in settings.items():
        db.set_setting(key.replace("__", "."), value)
    return db


# ---------------------------------------------------------------------------
# 1. 定时清理已下线，只保留手动清理
# ---------------------------------------------------------------------------
class TestNoScheduledPurge:
    def test_scheduler_symbols_removed(self):
        source = Path(__file__).resolve().parent.parent.joinpath(
            "app", "main.py").read_text(encoding="utf-8")
        assert "_purge_scheduler" not in source
        assert "_run_scheduled_purge" not in source
        assert "purge.last_run_date" not in source

    def test_manual_purge_still_available(self):
        """手动清理入口保留（用户唯一清理途径）。"""
        source = Path(__file__).resolve().parent.parent.joinpath(
            "app", "api", "router_system.py").read_text(encoding="utf-8")
        assert "/api/system/purge" in source


# ---------------------------------------------------------------------------
# 2. 不使用 AI 接口：轻量筛选命中即商机
# ---------------------------------------------------------------------------
class TestNoAiMode:
    def test_keyword_hit_creates_opportunity(self):
        db = prepare_db()
        pipeline = make_pipeline(db)
        pipeline._process(make_unit("我要一箱机油，有货的联系"))

        opps = db.list_opportunities()[1]
        assert len(opps) == 1
        opp = opps[0]
        assert opp["type_name"] == "轻量筛选命中"
        assert opp["confidence"] == 100
        assert "机油" in opp["summary"]
        # 日志：ok + model=轻量筛选（不产生 token 消耗）
        _total, logs = db.list_analysis_logs()
        assert logs[0]["status"] == "ok"
        assert logs[0]["model"] == "轻量筛选"
        assert logs[0]["latency_ms"] is not None

    def test_regex_hit_creates_opportunity(self):
        db = prepare_db()
        db.set_setting("analysis.prescreen_keywords", "")
        db.set_setting("analysis.prescreen_regex", r"求购.{0,6}件")
        pipeline = make_pipeline(db)
        pipeline._process(make_unit("求购侧围件 20 件"))

        opps = db.list_opportunities()[1]
        assert len(opps) == 1
        assert opps[0]["type_name"] == "轻量筛选命中"

    def test_miss_logs_skipped(self):
        db = prepare_db()
        pipeline = make_pipeline(db)
        pipeline._process(make_unit("今天天气不错，出来聊聊"))

        assert db.list_opportunities()[1] == []
        _total, logs = db.list_analysis_logs()
        assert logs[0]["status"] == "skipped"
        assert "未命中" in logs[0]["error"]

    def test_no_rules_logs_skipped_with_hint(self):
        db = prepare_db()
        db.set_setting("analysis.prescreen_keywords", "")
        db.set_setting("analysis.prescreen_regex", "")
        pipeline = make_pipeline(db)
        pipeline._process(make_unit("我要一箱机油"))

        assert db.list_opportunities()[1] == []
        _total, logs = db.list_analysis_logs()
        assert logs[0]["status"] == "skipped"
        assert "未配置" in logs[0]["error"]

    def test_does_not_require_compliance_or_api_key(self):
        """关闭 AI 后不向第三方发送内容：无需合规确认、无需配置接口。"""
        db = prepare_db()   # compliance.confirmed 默认 0，base_url/key 为空
        assert db.get_setting("compliance.confirmed", "0") == "0"
        pipeline = make_pipeline(db)
        pipeline._process(make_unit("刹车片要 50 套"))

        assert len(db.list_opportunities()[1]) == 1
        _total, logs = db.list_analysis_logs()
        assert logs[0]["status"] == "ok"


# ---------------------------------------------------------------------------
# 3. 时间戳为本地时间
# ---------------------------------------------------------------------------
class TestLocalTimestamps:
    def test_new_rows_use_local_time(self):
        db = Database(":memory:")
        db.insert_opportunity(
            session_username="1@chatroom", session_name="群", sender_wxid="wxid_a",
            sender_name="张三", window_start="2026-11-10T14:00:00",
            window_end="2026-11-10T14:02:00", merged_count=1,
            raw_text="[14:02] 机油", type_id=None, type_name="其他",
            summary="机油", key_quote="机油", confidence=80)
        db.insert_analysis_log(session_username="1@chatroom", status="ok")
        for table in ("opportunities", "analysis_logs"):
            row = db._conn.execute(
                f"SELECT created_at FROM {table} ORDER BY id DESC LIMIT 1"
            ).fetchone()
            parsed = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
            assert abs((datetime.now() - parsed).total_seconds()) < 120

    def test_legacy_utc_rows_shifted_once(self, tmp_path):
        path = tmp_path / "legacy.sqlite3"
        db = Database(str(path))
        opp = db.insert_opportunity(
            session_username="1@chatroom", session_name="群", sender_wxid="wxid_a",
            sender_name="张三", window_start="2026-11-10T14:00:00",
            window_end="2026-11-10T14:02:00", merged_count=1,
            raw_text="[14:02] 机油", type_id=None, type_name="其他",
            summary="机油", key_quote="机油", confidence=80)
        # 模拟旧版本写入的 UTC 时间戳 + 未迁移标记
        db._conn.execute("UPDATE opportunities SET created_at=? WHERE id=?",
                         ("2026-11-10 06:00:00", opp["id"]))
        db._conn.commit()
        db.set_setting("runtime.created_at_localtime", "0")
        db.close()

        db2 = Database(str(path))
        shifted = db2.get_opportunity(opp["id"])["created_at"]
        db2.close()
        offset_hours = round(
            datetime.now().astimezone().utcoffset().total_seconds() / 3600)
        expected = (datetime.strptime("2026-11-10 06:00:00",
                                      "%Y-%m-%d %H:%M:%S")
                    + timedelta(hours=offset_hours)).strftime("%Y-%m-%d %H:%M:%S")
        assert shifted == expected


# ---------------------------------------------------------------------------
# 4/5. 未读标识、原文显示名（API 层）
# ---------------------------------------------------------------------------
class FakeSvc:
    """微信服务替身：只实现商机接口与 /api/status 用到的成员。"""

    listener_running = True
    monitored_count = 1
    available = True
    watermark_saved_at = ""
    degraded_reason = ""

    def build_log_displays(self, session_username: str,
                           input_text: str) -> tuple[str, str]:
        return session_username, input_text.replace("wxid_a", "张三")

    def probe_wechat_window(self) -> bool:
        return True

    def cache_status(self) -> list:
        return []


class FakePipeline:
    """管线替身：/api/status 只读取这几个属性。"""

    queue_size = 0
    queue_max = 500
    dropped_total = 0
    busy = 0
    analysis_health = {"last_error": "", "consecutive_failures": 0}


@pytest.fixture()
def api(tmp_path):
    db = Database(str(tmp_path / "r3.sqlite3"))
    svc = FakeSvc()
    notifier = Notifier()
    deps.configure(db=db, wechat=svc, pipeline=FakePipeline(),
                   sender=None, notifier=notifier)
    app = FastAPI()
    for router in ALL_ROUTERS:
        app.include_router(router)
    client = TestClient(app)
    yield SimpleNamespace(db=db, notifier=notifier, client=client)
    deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                   notifier=None)
    db.close()


def insert_opp(db: Database) -> dict:
    return db.insert_opportunity(
        session_username="21264829018@chatroom", session_name="配件群",
        sender_wxid="wxid_a", sender_name="张三",
        window_start="2026-11-10T14:00:00", window_end="2026-11-10T14:02:00",
        merged_count=1, raw_text="[14:02] wxid_a: 我要一箱机油",
        type_id=None, type_name="其他", summary="机油 1 箱",
        key_quote="我要一箱机油", confidence=80)


class TestUnreadFlag:
    def test_list_marks_unread_and_read_clears(self, api):
        opp = insert_opp(api.db)
        api.notifier.mark_new_opportunity(opp["id"])

        body = api.client.get("/api/opportunities").json()
        assert body["items"][0]["unread"] is True
        assert api.client.get("/api/status").json()["unread_count"] == 1

        resp = api.client.post(f"/api/opportunities/{opp['id']}/read")
        assert resp.status_code == 200
        assert resp.json()["cleared"] is True
        assert api.client.get("/api/opportunities").json()["items"][0]["unread"] is False
        assert api.client.get("/api/status").json()["unread_count"] == 0
        # 幂等：再次调用不报错
        assert api.client.post(f"/api/opportunities/{opp['id']}/read"
                               ).json()["cleared"] is False

    def test_detail_clears_unread(self, api):
        opp = insert_opp(api.db)
        api.notifier.mark_new_opportunity(opp["id"])
        api.client.get(f"/api/opportunities/{opp['id']}")
        assert api.notifier.is_unread(opp["id"]) is False

    def test_read_missing_returns_404(self, api):
        assert api.client.post("/api/opportunities/99999/read"
                               ).status_code == 404


class TestRawTextDisplay:
    def test_detail_returns_display_text(self, api):
        opp = insert_opp(api.db)
        body = api.client.get(f"/api/opportunities/{opp['id']}").json()
        assert "wxid_a" not in body["raw_text_display"]
        assert "张三" in body["raw_text_display"]

    def test_display_fallback_keeps_raw_text(self, tmp_path):
        """显示名解析抛异常时详情仍可用（绝不 500）。"""
        db = Database(str(tmp_path / "fallback.sqlite3"))

        class BrokenSvc:
            def build_log_displays(self, *a, **k):
                raise RuntimeError("群成员表读取失败")

        deps.configure(db=db, wechat=BrokenSvc(), pipeline=None, sender=None,
                       notifier=Notifier())
        app = FastAPI()
        for router in ALL_ROUTERS:
            app.include_router(router)
        client = TestClient(app)
        try:
            opp = insert_opp(db)
            body = client.get(f"/api/opportunities/{opp['id']}").json()
            assert body["raw_text_display"] == opp["raw_text"]
        finally:
            deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                           notifier=None)
            db.close()


# ---------------------------------------------------------------------------
# AI 开关设置契约
# ---------------------------------------------------------------------------
class TestAiEnabledSetting:
    def test_default_enabled(self):
        assert read_ai_settings(Database(":memory:"))["enabled"] == "1"

    def test_api_roundtrip(self, api):
        body = api.client.get("/api/settings/ai").json()
        assert body["enabled"] is True
        saved = api.client.put("/api/settings/ai", json={
            "base_url": "", "api_key": "", "model": "", "timeout_sec": 60,
            "temperature": 0.2, "disable_thinking": True, "enabled": False,
            "confirm_compliance": False,
        })
        assert saved.status_code == 200
        assert saved.json()["enabled"] is False
        assert api.db.get_setting("ai.enabled") == "0"
        # /api/status 同步暴露（前端横幅用）
        assert api.client.get("/api/status").json()["ai_enabled"] is False


# ---------------------------------------------------------------------------
# 6/7/11. 发送人显示名 + 小程序内容
# ---------------------------------------------------------------------------
class FakeWxDb:
    """群成员 / 昵称 / 历史消息替身。"""

    def get_group_members(self, chatroom: str) -> list[dict]:
        return [
            {"username": "wxid_m9klwkbp4f1c22", "nick_name": "渔",
             "remark": "", "is_owner": False},
        ]

    def get_nickname(self, username: str) -> str:
        return {"21264829018@chatroom": "配件群"}.get(username, "")

    def get_messages(self, username: str, limit: int = 50) -> list[dict]:
        return [
            {"local_id": 1, "type": "文本", "sender_id": 3,
             "sender_username": "wxid_wrong_map", "sort_seq": 1,
             "create_time": time.time(),
             "content": "wxid_m9klwkbp4f1c22:\n我要一箱机油"},
            {"local_id": 2, "type": "文件/链接/卡片", "sender_id": 3,
             "sender_username": "wxid_wrong_map", "sort_seq": 2,
             "create_time": time.time(),
             "content": "wxid_m9klwkbp4f1c22:\n" + MINI_PROGRAM_XML},
        ]


def make_service() -> WeChatService:
    svc = WeChatService(Database(":memory:"), None)  # type: ignore[arg-type]
    svc._wxdb = FakeWxDb()
    return svc


MINI_PROGRAM_XML = ('<?xml version="1.0"?>\n<msg><appmsg appid="" sdkver="0">'
                    "<title>汽车配件/拆车件批发 &gt;&gt;&gt;</title>"
                    "<type>33</type>"
                    "<url>https://mp.weixin.qq.com/mp/waerrpage?x=1</url>"
                    "<sourcedisplayname>车链网数字低碳产业园</sourcedisplayname>"
                    "</appmsg></msg>")


class TestSenderDisplay:
    def test_group_prefix_resolved_and_stripped(self):
        svc = make_service()
        name, clean = svc._resolve_row_sender(
            "21264829018@chatroom", "wxid_m9klwkbp4f1c22:\n我要一箱机油",
            "wxid_m9klwkbp4f1c22", 3)
        assert name == "渔"
        assert clean == "我要一箱机油"

    def test_non_member_prefix_not_stripped(self):
        """正文首行像「9:30」但不是群成员：不得误吃正文。"""
        svc = make_service()
        name, clean = svc._resolve_row_sender(
            "21264829018@chatroom", "9:30 开会", "wxid_m9klwkbp4f1c22", 3)
        assert clean == "9:30 开会"
        assert name == "渔"

    def test_private_chat_self(self):
        svc = make_service()
        name, clean = svc._resolve_row_sender("wxid_b", "你好", "", 2)
        assert (name, clean) == ("我", "你好")

    def test_mini_program_not_xml(self):
        """反馈 11：小程序卡片带发送者前缀 + 「文件/链接/卡片」类型也要可读。"""
        out = format_content(
            "文件/链接/卡片",
            "wxid_m9klwkbp4f1c22:\n" + MINI_PROGRAM_XML)
        assert out.startswith("[小程序] 汽车配件/拆车件批发")
        assert "车链网数字低碳产业园" in out
        assert "<" not in out
        assert "waerrpage" not in out

    def test_legacy_comma_type_still_works(self):
        out = format_content("文件、链接、卡片", MINI_PROGRAM_XML)
        assert out.startswith("[小程序]")

    def test_text_type_not_touched(self):
        assert format_content("文本", "wxid_a:\n内容") == "wxid_a:\n内容"

    def test_context_messages_have_names_and_clean_content(self):
        """上下文接口：发送人显示群昵称，正文无 wxid 前缀，小程序不再是 XML。"""
        svc = make_service()
        msgs, error = svc.get_messages_with_error(
            "21264829018@chatroom", limit=10)
        assert error == ""
        assert msgs[0]["sender_name"] == "渔"
        assert msgs[0]["content"] == "我要一箱机油"
        assert msgs[1]["content"].startswith("[小程序]")
        assert "wxid_" not in msgs[1]["content"]
        assert "<" not in msgs[1]["content"]
