# -*- coding: utf-8 -*-
"""多开隔离：监控对象/商机按账号归属、调度器只跑当前账号的定时群。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.aggregator import Aggregator
from app.api import deps
from app.api.router_opportunities import router as opportunities_router
from app.api.router_targets import router as targets_router
from app.config import KEY_SUMMARY_SCHEDULE_CONFIGS
from app.db import Database
from app.notifier import Notifier
from app.summary import SummaryScheduler


# ---------------------------------------------------------------------------
# 数据层
# ---------------------------------------------------------------------------
def _insert_opp(db: Database, *, session: str, account: str, sender: str = "wxid_a") -> dict:
    return db.insert_opportunity(
        session_username=session, session_name=session,
        sender_wxid=sender, sender_name="张三",
        window_start="2026-11-27T10:00:00",
        window_end="2026-11-27T10:01:00",
        merged_count=1, raw_text="[10:00] 测试原文",
        type_id=None, type_name="机械加工订单",
        summary="要点", key_quote="原文", confidence=80,
        account=account)


def test_legacy_table_rebuilt_to_account_unique(tmp_path):
    """旧库（UNIQUE(username)、无 account 列）打开后重建约束且可复用。"""
    path = str(tmp_path / "legacy_scope.db")
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

    db = Database(path)
    try:
        cols = {r["name"] for r in db._conn.execute(
            "PRAGMA table_info(monitor_targets)")}
        assert {"account", "monitor_since"} <= cols
        sql = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='monitor_targets'").fetchone()["sql"]
        assert "unique(account,username)" in "".join(sql.lower().split())
        # 存量行归属为空串；同名会话可在两个账号下各自添加
        assert db.get_target_by_username("wxid_old")["account"] == ""
        db.insert_target("wxid_old", "老目标", "friend", account="acc_a")
        db.insert_target("wxid_old", "老目标", "friend", account="acc_b")
        assert db.count_targets(account="acc_a") == 1
        assert len(db.list_targets()) == 3
    finally:
        db.close()


def test_same_username_duplicate_within_account_raises(tmp_path):
    db = Database(str(tmp_path / "dup_scope.db"))
    try:
        db.insert_target("g@chatroom", "群", "group", account="acc_a")
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_target("g@chatroom", "群", "group", account="acc_a")
        db.insert_target("g@chatroom", "群", "group", account="acc_b")
    finally:
        db.close()


def test_queries_filter_opportunities_by_account(tmp_path):
    db = Database(str(tmp_path / "opp_scope.db"))
    try:
        a = _insert_opp(db, session="a@chatroom", account="acc_a")
        b = _insert_opp(db, session="b@chatroom", account="acc_b")
        total, rows = db.list_opportunities(account="acc_a")
        assert total == 1 and rows[0]["id"] == a["id"]
        assert db.list_opportunities()[0] == 2
        assert db.count_unread_opportunities([a["id"], b["id"]],
                                             account="acc_a") == 1
        assert db.list_enabled_target_usernames(account="acc_a") == []
    finally:
        db.close()


def test_backfill_scope_assigns_legacy_rows_once(tmp_path):
    db = Database(str(tmp_path / "backfill_scope.db"))
    try:
        db.set_setting(KEY_SUMMARY_SCHEDULE_CONFIGS, json.dumps(
            [{"session": "g@chatroom", "time": "18:00", "enabled": True}],
            ensure_ascii=False))
        db.insert_target("g@chatroom", "群", "group")
        _insert_opp(db, session="g@chatroom", account="")

        result = db.backfill_account_scope("acc_a")
        assert result == {"targets": 1, "opportunities": 1, "schedules": 1}
        assert db.list_targets(account="acc_a")[0]["username"] == "g@chatroom"
        assert db.list_opportunities(account="acc_a")[0] == 1
        configs = json.loads(db.get_setting(KEY_SUMMARY_SCHEDULE_CONFIGS))
        assert configs[0]["account"] == "acc_a"

        # 幂等：再次打开同一账号不会重复归属，也不会错归其它账号
        assert db.backfill_account_scope("acc_a") == {
            "targets": 0, "opportunities": 0, "schedules": 0}
        assert db.list_targets(account="acc_b") == []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# API：默认只看当前账号，all_accounts=1 看全部
# ---------------------------------------------------------------------------
class _FakeWechat:
    def __init__(self, account: str) -> None:
        self._account = account
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    def effective_account(self) -> str:
        return self._account

    def account_display(self, account: str) -> str:
        return {"acc_a": "微信A", "acc_b": "微信B"}.get(account, "")

    def get_display_name(self, username: str) -> str:
        return username

    def session_exists(self, username: str) -> bool:
        return True

    def register_target(self, username: str) -> None:
        self.registered.append(username)

    def unregister_target(self, username: str) -> None:
        self.unregistered.append(username)


@pytest.fixture()
def scope_api(tmp_path):
    db = Database(str(tmp_path / "api_scope.db"))
    svc = _FakeWechat("acc_a")
    deps.configure(db=db, wechat=svc, pipeline=None, sender=None,
                   notifier=Notifier())
    app = FastAPI()
    app.include_router(targets_router)
    app.include_router(opportunities_router)
    client = TestClient(app)
    yield SimpleNamespace(db=db, svc=svc, client=client)
    deps.reset_for_tests()
    db.close()


def test_targets_api_filters_and_labels_account(scope_api):
    env = scope_api
    env.db.insert_target("a1@chatroom", "A1群", "group", account="acc_a")
    env.db.insert_target("b1@chatroom", "B1群", "group", account="acc_b")

    default_items = env.client.get("/api/monitor-targets").json()
    assert [t["username"] for t in default_items] == ["a1@chatroom"]
    assert default_items[0]["account"] == "acc_a"
    assert default_items[0]["account_name"] == "微信A"

    all_items = env.client.get("/api/monitor-targets?all_accounts=1").json()
    assert {t["username"]: t["account_name"] for t in all_items} == {
        "a1@chatroom": "微信A", "b1@chatroom": "微信B"}

    # 同一群在别的账号下监控过，不影响本账号再次添加（归属当前账号）
    created = env.client.post("/api/monitor-targets",
                              json={"username": "b1@chatroom"})
    assert created.status_code == 201
    assert created.json()["account"] == "acc_a"
    assert created.json()["account_name"] == "微信A"


def test_other_account_target_not_registered_on_current_listener(scope_api):
    """跨账号勾选/删除对象只改库，不注册到当前账号的监听。"""
    env = scope_api
    other = env.db.insert_target("b1@chatroom", "B1群", "group", account="acc_b")
    assert env.client.patch(f"/api/monitor-targets/{other['id']}",
                            json={"enabled": False}).status_code == 200
    assert env.client.patch(f"/api/monitor-targets/{other['id']}",
                            json={"enabled": True}).status_code == 200
    assert env.svc.registered == []
    assert env.svc.unregistered == []

    assert env.client.delete(
        f"/api/monitor-targets/{other['id']}").status_code == 204
    assert env.svc.unregistered == []


def test_opportunities_api_filters_and_all_accounts(scope_api):
    env = scope_api
    _insert_opp(env.db, session="a@chatroom", account="acc_a")
    _insert_opp(env.db, session="b@chatroom", account="acc_b")

    default_body = env.client.get("/api/opportunities").json()
    assert default_body["total"] == 1
    assert default_body["items"][0]["session_username"] == "a@chatroom"

    all_body = env.client.get("/api/opportunities?all_accounts=1").json()
    assert all_body["total"] == 2


# ---------------------------------------------------------------------------
# 聚合器：切号时同一会话/发送人不混缓冲，单元携带来源账号
# ---------------------------------------------------------------------------
def test_aggregator_buffers_are_separated_by_account():
    units = []
    agg = Aggregator(units.append, window_sec_provider=lambda: 15)
    base = {"local_id": 1, "type": "文本", "sender_id": 2,
            "sender_username": "wxid_a", "sender_display_name": "张三",
            "create_time": 1794571099, "sort_seq": 1,
            "username": "g@chatroom"}
    agg.add({**base, "content": "账号A的消息", "_account": "acc_a"},
            "群", "group")
    agg.add({**base, "content": "账号B的消息", "_account": "acc_b"},
            "群", "group")
    assert agg.pending_count() == 2

    agg._flush_expired(force=True)
    assert sorted(unit.account for unit in units) == ["acc_a", "acc_b"]
    assert {unit.merged_text() for unit in units} == {
        "[19:58] 账号A的消息", "[19:58] 账号B的消息"}


# ---------------------------------------------------------------------------
# 调度器：只跑当前监控账号的定时群
# ---------------------------------------------------------------------------
class _SchedulerDb:
    def __init__(self, configs: list[dict]) -> None:
        self.settings = {
            "summary.schedule_enabled": "1",
            KEY_SUMMARY_SCHEDULE_CONFIGS: json.dumps(configs, ensure_ascii=False),
        }

    def get_setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    def has_group_summary_on_day(self, session: str, day: str) -> bool:
        return False


class _SchedulerWechat:
    def __init__(self, account: str) -> None:
        self._account = account

    def effective_account(self) -> str:
        return self._account


class _SchedulerService:
    def __init__(self, account: str, configs: list[dict]) -> None:
        self._db = _SchedulerDb(configs)
        self._wechat = _SchedulerWechat(account)
        self.generated: list[str] = []

    def generate(self, session: str, start: str, end: str,
                 senders=None, style: str = "detailed", custom_prompt=None) -> dict:
        self.generated.append(session)
        return {"id": len(self.generated), "session_username": session,
                "session_name": session, "created_at": "", "msg_count": 1}


_AT_SIX = datetime(2026, 11, 27, 6, 0)
_CONFIGS = [
    {"session": "g_a@chatroom", "enabled": True, "time": "06:00", "account": "acc_a"},
    {"session": "g_b@chatroom", "enabled": True, "time": "06:00", "account": "acc_b"},
    {"session": "g_legacy@chatroom", "enabled": True, "time": "06:00"},
]


def test_scheduler_only_runs_current_account_configs():
    svc = _SchedulerService("acc_a", _CONFIGS)
    produced = SummaryScheduler(svc, None, now_fn=lambda: _AT_SIX).tick()
    assert svc.generated == ["g_a@chatroom"]
    assert [row["session_username"] for row in produced] == ["g_a@chatroom"]


def test_scheduler_unknown_account_runs_only_unowned_configs():
    svc = _SchedulerService("", _CONFIGS)
    SummaryScheduler(svc, None, now_fn=lambda: _AT_SIX).tick()
    assert svc.generated == ["g_legacy@chatroom"]


def test_scheduler_without_account_capability_keeps_legacy_behavior():
    """测试替身/旧服务没有 _wechat：仅跑未标注账号的旧配置。"""

    class _LegacyService:
        def __init__(self) -> None:
            self._db = _SchedulerDb(_CONFIGS)
            self.generated: list[str] = []

        def generate(self, session, start, end, senders=None,
                     style="detailed", custom_prompt=None):
            self.generated.append(session)
            return {"id": len(self.generated), "session_username": session,
                    "session_name": session, "created_at": "", "msg_count": 1}

    svc = _LegacyService()
    SummaryScheduler(svc, None, now_fn=lambda: _AT_SIX).tick()
    assert svc.generated == ["g_legacy@chatroom"]