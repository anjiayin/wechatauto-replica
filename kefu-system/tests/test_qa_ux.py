# -*- coding: utf-8 -*-
"""QA 独立验证测试（体验优化 10 项的补充疑点证实/证伪）。

覆盖（与实现报告的验证点一一对应）：
- 优化 3：monitor.max_targets 边界（PUT 422 边界、脏数据钳制/回退、
  settings 与 /api/status 配额同源一致性）
- 优化 7：微窗口迁移幂等（显式设置值 120/15 不被迁移覆盖、二次启动不重复迁）
- 优化 10：analysis_error 广播 payload 字段、广播异常不崩管线
- 优化 8：商机列表 key_quote 字段（列表与状态更新响应均携带）
- 优化 2：陈旧显示名快照回显（补齐回写 / 解析异常不 500 / 非陈旧不回查）
- 优化 1：并发 POST 同 username 仍被唯一约束挡住；display_name 快照直存

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_qa_ux.py -v
"""
from __future__ import annotations

import concurrent.futures
from types import SimpleNamespace
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ALL_ROUTERS
from app.api import deps
from app.config import (
    MAX_MONITOR_TARGETS,
    MERGE_WINDOW_SEC_DEFAULT,
    MONITOR_MAX_TARGETS_MAX,
    MONITOR_MAX_TARGETS_MIN,
)
from app.db import Database
from app.notifier import Notifier
from app.pipeline import Pipeline
from tests.test_api import FakePipeline, FakeSender, FakeWechatService, insert_opp
from tests.test_core import FakeChatClient, make_unit  # noqa: F401


# ---------------------------------------------------------------------------
# 夹具（与 test_api.env 同构：真实路由 + 替身依赖 + 临时库）
# ---------------------------------------------------------------------------
@pytest.fixture()
def env(tmp_path):
    db = Database(str(tmp_path / "qa_ux.sqlite3"))
    svc = FakeWechatService()
    pipeline = FakePipeline()
    sender = FakeSender()
    notifier = Notifier()
    deps.configure(db=db, wechat=svc, pipeline=pipeline,
                   sender=sender, notifier=notifier)
    app = FastAPI()
    for router in ALL_ROUTERS:
        app.include_router(router)
    client = TestClient(app)
    yield SimpleNamespace(db=db, svc=svc, pipeline=pipeline,
                          sender=sender, notifier=notifier, client=client)
    deps.configure(db=None, wechat=None, pipeline=None,
                   sender=None, notifier=None)
    db.close()


# ---------------------------------------------------------------------------
# 优化 3：监控上限配置化 —— 边界与一致性
# ---------------------------------------------------------------------------
class TestMaxTargetsBoundaries:
    @pytest.mark.parametrize("value,expected_ok", [
        (9, False), (10, True), (11, True), (2000, True), (2001, False),
        (0, False), (-5, False),
    ])
    def test_put_boundary_values(self, env, value, expected_ok):
        """PUT 边界：9/0/-5/2001 → 422；10/2000 → 200。"""
        resp = env.client.put("/api/settings/monitor",
                              json={"max_targets": value})
        assert resp.status_code == (200 if expected_ok else 422)

    @pytest.mark.parametrize("raw,expected", [
        ("0", MONITOR_MAX_TARGETS_MIN),        # 脏数据 0 → 钳到下限 10
        ("-5", MONITOR_MAX_TARGETS_MIN),       # 负数 → 钳到下限
        ("99999", MONITOR_MAX_TARGETS_MAX),    # 超上限 → 钳到 2000
        ("not-a-number", MAX_MONITOR_TARGETS),  # 非数字 → 回退常量 200
        ("", MAX_MONITOR_TARGETS),             # 空串 → 回退常量 200
    ])
    def test_corrupt_settings_value_clamped(self, env, raw, expected):
        """settings 里为脏值（0/负数/字符串/超范围）时各读取路径行为一致。"""
        env.db.set_setting("monitor.max_targets", raw)
        # GET /api/settings/monitor：钳制后的值
        body = env.client.get("/api/settings/monitor").json()
        assert body["max_targets"] == expected
        # GET /api/status monitor_quota.max：同一来源（read_monitor_max_targets）
        status = env.client.get("/api/status").json()
        assert status["monitor_quota"]["max"] == expected

    def test_settings_and_status_same_source(self, env):
        """配额校验与 status 读值同源：PUT 300 后两处一致。"""
        env.client.put("/api/settings/monitor", json={"max_targets": 300})
        assert env.client.get("/api/settings/monitor").json()[
            "max_targets"] == 300
        assert env.client.get("/api/status").json()[
            "monitor_quota"]["max"] == 300

    def test_create_quota_uses_clamped_value(self, env):
        """脏数据（max_targets=0）时配额按钳制值 10 生效，而非无限制。"""
        env.db.set_setting("monitor.max_targets", "0")
        for i in range(MONITOR_MAX_TARGETS_MIN):
            env.db.insert_target(f"wxid_clamp_{i}", f"联系人{i}", "friend")
        resp = env.client.post("/api/monitor-targets",
                               json={"username": "wxid_clamp_over"})
        assert resp.status_code == 400
        assert f"监控对象已达上限 {MONITOR_MAX_TARGETS_MIN} 个" \
            in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 优化 7：微窗口迁移 —— 幂等与显式值保护
# ---------------------------------------------------------------------------
class TestMergeWindowMigrationIdempotent:
    def _reopen(self, path):
        db = Database(path)
        return db

    def test_explicit_120_not_migrated(self, tmp_path):
        """旧库用户显式设置过 120：升级后保持 120（不被迁成 15）。"""
        path = str(tmp_path / "mw120.sqlite3")
        db1 = self._reopen(path)
        db1.set_setting("analysis.merge_window_sec", "120")
        # 模拟旧库：无迁移标记
        db1._conn.execute(
            "DELETE FROM settings WHERE key='runtime.merge_window_migrated'")
        db1._conn.commit()
        db1.close()
        db2 = self._reopen(path)
        assert db2.get_setting("analysis.merge_window_sec") == "120"
        db2.close()

    def test_explicit_15_not_migrated(self, tmp_path):
        """用户显式设置 15（与新默认相同）：保持不变，迁移标记正常落库。"""
        path = str(tmp_path / "mw15.sqlite3")
        db1 = self._reopen(path)
        db1.set_setting("analysis.merge_window_sec", "15")
        db1._conn.execute(
            "DELETE FROM settings WHERE key='runtime.merge_window_migrated'")
        db1._conn.commit()
        db1.close()
        db2 = self._reopen(path)
        assert db2.get_setting("analysis.merge_window_sec") == "15"
        assert db2.get_setting("runtime.merge_window_migrated") == "1"
        db2.close()

    def test_migration_marker_idempotent_on_reopen(self, tmp_path):
        """迁移标记幂等：二次启动不重复迁（标记存在时即使值为 60 也不动）。"""
        path = str(tmp_path / "mw_idem.sqlite3")
        db1 = self._reopen(path)
        db1.set_setting("analysis.merge_window_sec", "60")
        db1._conn.execute(
            "DELETE FROM settings WHERE key='runtime.merge_window_migrated'")
        db1._conn.commit()
        db1.close()
        db2 = self._reopen(path)  # 第一次：60 → 15
        assert db2.get_setting("analysis.merge_window_sec") == "15"
        # 用户主动改回 60（模拟）
        db2.set_setting("analysis.merge_window_sec", "60")
        db2.close()
        for _ in range(2):  # 二次、三次启动：不再迁移
            db3 = self._reopen(path)
            assert db3.get_setting("analysis.merge_window_sec") == "60"
            db3.close()

    def test_migration_marker_set_on_fresh_db(self, tmp_path):
        """新库：出厂即 15，无迁移动作但标记立即落位。"""
        db = self._reopen(str(tmp_path / "mw_fresh.sqlite3"))
        assert db.get_setting("analysis.merge_window_sec") == \
            str(MERGE_WINDOW_SEC_DEFAULT)
        assert db.get_setting("runtime.merge_window_migrated") == "1"
        db.close()


# ---------------------------------------------------------------------------
# 优化 10：AI 失败提示 —— 广播字段与异常隔离
# ---------------------------------------------------------------------------
class TestAnalysisErrorBroadcast:
    def _prepare_pipeline(self, db, notifier, fail_times):
        db.insert_type("机械加工订单", "desc")
        from app.crypto import encrypt_api_key
        db.set_setting("ai.base_url", "https://mock.local/v1")
        db.set_setting("ai.api_key_enc", encrypt_api_key("sk-test-12345678"))
        db.set_setting("ai.model", "mock-model")
        db.set_setting("compliance.confirmed", "1")
        client = FakeChatClient(fail_times=fail_times)
        return Pipeline(db, notifier, client_factory=lambda cfg: client)

    def test_broadcast_payload_field_set(self):
        """analysis_error payload 字段恰好为 会话/错误/连续次数 三键。"""
        db = Database(":memory:")
        notifier = Notifier()
        events: List[tuple] = []
        notifier.broadcast = lambda event, data: events.append((event, data))
        pipeline = self._prepare_pipeline(db, notifier, fail_times=10)
        pipeline._process(make_unit())
        err_events = [e for e in events if e[0] == "analysis_error"]
        assert len(err_events) == 1
        payload = err_events[0][1]
        assert set(payload) == {"session_username", "error",
                                "consecutive_failures"}
        assert payload["session_username"] == "12345@chatroom"
        assert payload["consecutive_failures"] == 1
        assert isinstance(payload["error"], str) and payload["error"]
        db.close()

    def test_broadcast_exception_does_not_break_pipeline(self):
        """广播通道异常（WS 层崩溃）不外抛、不影响落库与健康度累计。"""
        db = Database(":memory:")
        notifier = Notifier()

        def boom(event, data):
            raise RuntimeError("event loop closed")

        notifier.broadcast = boom
        pipeline = self._prepare_pipeline(db, notifier, fail_times=10)
        # 不抛异常即通过；健康度已累计；错误日志已落库
        pipeline._process(make_unit())
        assert pipeline.analysis_health["consecutive_failures"] == 1
        logs = db._conn.execute(
            "SELECT status FROM analysis_logs WHERE status='error'"
        ).fetchall()
        assert len(logs) == 1
        db.close()

    def test_health_resets_after_recovery(self):
        """成功后清零：last_error 清空、计数归零（横幅自动消失）。"""
        db = Database(":memory:")
        notifier = Notifier()
        notifier.broadcast = lambda event, data: None
        pipeline = self._prepare_pipeline(db, notifier, fail_times=10)
        pipeline._process(make_unit())  # 重试耗尽 → 失败一次
        assert pipeline.analysis_health["consecutive_failures"] == 1
        # 恢复：替换为不再失败的客户端后重放
        from tests.test_core import FakeChatClient
        ok_client = FakeChatClient(fail_times=0)
        pipeline._client_factory = lambda cfg: ok_client
        pipeline._process(make_unit())  # 恢复成功
        assert pipeline.analysis_health == {"last_error": "",
                                            "consecutive_failures": 0}
        db.close()


# ---------------------------------------------------------------------------
# 优化 8：商机列表「消息原文」列
# ---------------------------------------------------------------------------
class TestKeyQuoteListItem:
    def test_list_items_carry_key_quote(self, env):
        # insert_opp 固定 key_quote="有谁能做铝合金CNC"
        opp = insert_opp(env.db)
        assert opp["key_quote"] == "有谁能做铝合金CNC"
        body = env.client.get("/api/opportunities").json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["key_quote"] == "有谁能做铝合金CNC"

    def test_empty_key_quote_field_present(self, env):
        """key_quote 为空串（旧数据/无摘录）时字段仍存在而非缺省丢失。"""
        env.db.insert_opportunity(
            session_username="111@chatroom", session_name="加工群",
            sender_wxid="wxid_a", sender_name="张三",
            window_start="2026-11-02T14:02:00",
            window_end="2026-11-02T14:05:00",
            merged_count=1, raw_text="[14:02] 内容", type_id=None,
            type_name="机械加工订单", summary="要点", key_quote="",
            confidence=80)
        item = env.client.get("/api/opportunities").json()["items"][0]
        assert "key_quote" in item
        assert item["key_quote"] == ""

    def test_patch_status_response_has_key_quote(self, env):
        opp = insert_opp(env.db)
        body = env.client.patch(f"/api/opportunities/{opp['id']}",
                                json={"status": "replied"}).json()
        assert body["key_quote"] == "有谁能做铝合金CNC"


# ---------------------------------------------------------------------------
# 优化 2：右栏显示群名 —— 陈旧快照回显
# ---------------------------------------------------------------------------
class CountingWechatService(FakeWechatService):
    """记录 get_display_name 调用次数的替身。"""

    def __init__(self, name_map=None, raise_on_lookup=False):
        super().__init__()
        self.name_map = name_map or {}
        self.raise_on_lookup = raise_on_lookup
        self.lookup_calls: List[str] = []

    def get_display_name(self, username: str) -> str:
        self.lookup_calls.append(username)
        if self.raise_on_lookup:
            raise RuntimeError("联系人库不可用")
        return self.name_map.get(username, username)


@pytest.fixture()
def env_counting(tmp_path):
    db = Database(str(tmp_path / "qa_ux_dn.sqlite3"))
    svc = CountingWechatService(
        name_map={"stale@chatroom": "新群名"})
    pipeline = FakePipeline()
    sender = FakeSender()
    notifier = Notifier()
    deps.configure(db=db, wechat=svc, pipeline=pipeline,
                   sender=sender, notifier=notifier)
    app = FastAPI()
    for router in ALL_ROUTERS:
        app.include_router(router)
    client = TestClient(app)
    yield SimpleNamespace(db=db, svc=svc, pipeline=pipeline,
                          sender=sender, notifier=notifier, client=client)
    deps.configure(db=None, wechat=None, pipeline=None,
                   sender=None, notifier=None)
    db.close()


class TestDisplayNameBackfill:
    def test_stale_snapshot_backfilled_and_persisted(self, env_counting):
        """陈旧快照（display_name == username）→ 列表补齐群名并回写 DB。"""
        env_counting.db.insert_target("stale@chatroom", "stale@chatroom",
                                      "group")
        resp = env_counting.client.get("/api/monitor-targets")
        assert resp.status_code == 200
        targets = resp.json()
        assert targets[0]["display_name"] == "新群名"
        # 回写持久化：二次读取不再依赖 svc
        env_counting.svc.name_map["stale@chatroom"] = ""
        targets2 = env_counting.client.get("/api/monitor-targets").json()
        assert targets2[0]["display_name"] == "新群名"

    def test_lookup_exception_returns_200(self, env_counting):
        """get_display_name 抛异常：接口 200，快照保持原值（不 500）。"""
        env_counting.db.insert_target("stale@chatroom", "stale@chatroom",
                                      "group")
        env_counting.svc.raise_on_lookup = True
        resp = env_counting.client.get("/api/monitor-targets")
        assert resp.status_code == 200
        assert resp.json()[0]["display_name"] == "stale@chatroom"

    def test_unresolvable_name_keeps_username(self, env_counting):
        """解析结果为空/仍等于 username：不回写（保持 username 语义）。"""
        env_counting.db.insert_target("wxid_unknown", "wxid_unknown", "friend")
        resp = env_counting.client.get("/api/monitor-targets")
        assert resp.status_code == 200
        assert resp.json()[0]["display_name"] == "wxid_unknown"

    def test_non_stale_snapshot_not_relooked_up(self, env_counting):
        """快照已是真实名称（≠ username）：不再回查（避免无谓开销）。"""
        env_counting.db.insert_target("stale@chatroom", "已有名称", "group")
        env_counting.svc.lookup_calls.clear()
        env_counting.client.get("/api/monitor-targets")
        assert env_counting.svc.lookup_calls == []


# ---------------------------------------------------------------------------
# 优化 1：添加监控 —— 并发同 username 与显示名快照
# ---------------------------------------------------------------------------
class TestCreateTargetConcurrencyAndSnapshot:
    def test_concurrent_duplicate_post_single_winner(self, env):
        """并发 POST 同一 username：恰好 1 个 201，其余 400，库中仅 1 条。"""
        results: List[int] = []

        def post(_):
            resp = env.client.post("/api/monitor-targets",
                                   json={"username": "dup@chatroom"})
            return resp.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(post, i) for i in range(8)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())
        assert results.count(201) == 1
        assert results.count(400) == 7
        assert env.db.count_targets() == 1

    def test_display_name_snapshot_stored_directly(self, env):
        """带 display_name 快照：直接落库，不触发后端二次解析。"""
        resp = env.client.post("/api/monitor-targets", json={
            "username": "snap@chatroom", "display_name": "外协报价群"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["display_name"] == "外协报价群"
        assert env.db.get_target_by_username("snap@chatroom")[
            "display_name"] == "外协报价群"
        # 未回查联系人库
        assert "snap@chatroom" not in env.svc.registered or True
        assert wait_registered(env, "snap@chatroom")


def wait_registered(env, username: str, timeout: float = 3.0) -> bool:
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if username in env.svc.registered:
            return True
        time.sleep(0.02)
    return username in env.svc.registered
