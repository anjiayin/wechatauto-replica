# -*- coding: utf-8 -*-
"""任务 #1：商机分析只处理「监控对象配置完成时刻之后」进入的消息。

现场：Listener 的 watermark 是 sort_seq 水位，与配置时刻无关；一旦
watermark 过期/重置（首次读库连续失败后回退 0），会把该会话多年历史
消息一次性回放给 ``_on_message``，2022 年的旧消息被当成新商机排队。

本文件锁死四条契约：
1. 配置前（create_time < monitor_since）的群/私聊消息被丢弃，不进聚合；
2. 配置后消息正常进入聚合；毫秒 create_time 也能正确归一；
3. 存量 target（水位 0）不限制，老行为不变；停用→重启用刷新水位；
4. DB 迁移幂等：新库带列、老库补列、重复初始化不报错。
"""
from __future__ import annotations

import sqlite3

from app.api.router_targets import create_target, update_target
from app.db import Database
from app.schemas import MonitorTargetCreate, MonitorTargetUpdate
from app.wechat_service import WeChatService


class _SpyAgg:
    """记录进入商机合并窗口的消息（不触发真实 flush/AI）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[dict, str, str]] = []

    def add(self, msg: dict, session_name: str, kind: str) -> bool:
        self.calls.append((msg, session_name, kind))
        return True


class _ApiSvc:
    """router 依赖的最小服务替身（不触碰真实微信）。"""

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    def get_display_name(self, username: str) -> str:
        return ""

    def is_self_username(self, username: str) -> bool:
        return False

    def register_target(self, username: str) -> None:
        self.registered.append(username)

    def unregister_target(self, username: str) -> None:
        self.unregistered.append(username)


def _msg(username: str, create_time: float, content: str = "我要一台发动机"
         ) -> dict:
    return {
        "local_id": 1,
        "type": "文本",
        "sender_id": 3,
        "sender_username": "wxid_seller",
        "create_time": create_time,
        "content": content,
        "sort_seq": 1,
        "username": username,
    }


def _svc(db: Database) -> tuple[WeChatService, _SpyAgg]:
    agg = _SpyAgg()
    svc = WeChatService(db, agg)  # type: ignore[arg-type]
    return svc, agg


class TestHistoryFilter:
    """配置前历史消息不进商机分析，配置后消息正常。"""

    def test_group_history_before_config_dropped(self):
        db = Database(":memory:")
        now = 1_760_000_000
        db.insert_target("111@chatroom", "测试群", "group", monitor_since=now)
        svc, agg = _svc(db)
        # 2022 年历史消息（远早于水位）：静默丢弃，不进聚合、不落 skipped 日志
        svc._on_message(_msg("111@chatroom", 1_640_000_000))
        assert agg.calls == []
        logs = db._conn.execute(
            "SELECT COUNT(*) FROM analysis_logs").fetchone()[0]
        assert logs == 0

    def test_friend_history_before_config_dropped(self):
        db = Database(":memory:")
        now = 1_760_000_000
        db.insert_target("wxid_friend", "张三", "friend", monitor_since=now)
        svc, agg = _svc(db)
        svc._on_message(_msg("wxid_friend", now - 1))
        assert agg.calls == []

    def test_message_after_config_analyzed(self):
        db = Database(":memory:")
        now = 1_760_000_000
        db.insert_target("111@chatroom", "测试群", "group", monitor_since=now)
        svc, agg = _svc(db)
        msg = _msg("111@chatroom", now + 5, content="wxid_seller:\n我要一台发动机")
        svc._on_message(msg)
        assert len(agg.calls) == 1
        assert agg.calls[0][0]["create_time"] == now + 5

    def test_millisecond_create_time_normalized(self):
        """微信某版本 create_time 为毫秒时不应把配置后消息误判成历史。"""
        db = Database(":memory:")
        now = 1_760_000_000
        db.insert_target("wxid_friend", "张三", "friend", monitor_since=now)
        svc, agg = _svc(db)
        svc._on_message(_msg("wxid_friend", (now + 10) * 1000))
        assert len(agg.calls) == 1

    def test_legacy_target_without_water_mark_unrestricted(self):
        """存量 target（水位 0）不过滤，升级不误伤正在监控的会话。"""
        db = Database(":memory:")
        db.insert_target("111@chatroom", "老群", "group")  # monitor_since 默认 0
        svc, agg = _svc(db)
        svc._on_message(_msg("111@chatroom", 1_640_000_000))
        assert len(agg.calls) == 1

    def test_unconfigured_session_unrestricted(self):
        """没有配置 target 的会话（直调/测试）保持原行为。"""
        db = Database(":memory:")
        svc, agg = _svc(db)
        svc._on_message(_msg("111@chatroom", 1_640_000_000))
        assert len(agg.calls) == 1

    def test_missing_create_time_unrestricted(self):
        """无法比较的时间一律放行，避免误伤。"""
        db = Database(":memory:")
        db.insert_target("wxid_friend", "张三", "friend",
                         monitor_since=1_760_000_000)
        svc, agg = _svc(db)
        msg = _msg("wxid_friend", 0)
        msg["create_time"] = ""
        svc._on_message(msg)
        assert len(agg.calls) == 1


class TestApiWaterMark:
    """配置 API 正确写入/刷新水位。"""

    def test_create_target_writes_now_epoch(self):
        db = Database(":memory:")
        svc = _ApiSvc()
        before = int(__import__("time").time())
        out = create_target(
            MonitorTargetCreate(username="111@chatroom", display_name="测试群"),
            db, svc)  # type: ignore[arg-type]
        after = int(__import__("time").time())
        stored = db.get_target(out.id)["monitor_since"]
        assert before <= stored <= after

    def test_reenable_refreshes_water_mark(self):
        db = Database(":memory:")
        svc = _ApiSvc()
        target = db.insert_target("wxid_friend", "张三", "friend",
                                  monitor_since=1_700_000_000)
        update_target(target["id"], MonitorTargetUpdate(enabled=False),
                      db, svc)  # type: ignore[arg-type]
        update_target(target["id"], MonitorTargetUpdate(enabled=True),
                      db, svc)  # type: ignore[arg-type]
        fresh = db.get_target(target["id"])["monitor_since"]
        assert fresh > 1_700_000_000

    def test_repeated_enable_keeps_water_mark(self):
        """已启用状态下重复 PATCH 不刷新，避免过滤掉刚到的消息。"""
        db = Database(":memory:")
        svc = _ApiSvc()
        target = db.insert_target("wxid_friend", "张三", "friend",
                                  monitor_since=1_700_000_000)
        update_target(target["id"], MonitorTargetUpdate(enabled=True),
                      db, svc)  # type: ignore[arg-type]
        assert db.get_target(target["id"])["monitor_since"] == 1_700_000_000


class TestMigration:
    """DDL / 加列迁移幂等。"""

    def test_new_db_has_column(self):
        db = Database(":memory:")
        cols = {r["name"] for r in db._conn.execute(
            "PRAGMA table_info(monitor_targets)")}
        assert "monitor_since" in cols

    def test_legacy_db_column_added_and_idempotent(self, tmp_path):
        path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE monitor_targets ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, "
            "kind TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')))")
        conn.execute(
            "INSERT INTO monitor_targets (username, display_name, kind) "
            "VALUES ('wxid_old', '老目标', 'friend')")
        conn.commit()
        conn.close()

        for _ in range(2):  # 重复初始化必须幂等
            db = Database(path)
            cols = {r["name"] for r in db._conn.execute(
                "PRAGMA table_info(monitor_targets)")}
            assert "monitor_since" in cols
            row = db.get_target_by_username("wxid_old")
            assert row is not None and row["monitor_since"] == 0
            db.close()
