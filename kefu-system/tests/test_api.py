# -*- coding: utf-8 -*-
"""API 层测试：契约核对（设计文档 3.2 路由表）、商机生命周期、回复流程、
设置接口、并发边界。

测试策略：
- FastAPI TestClient + 临时 SQLite（tmp_path），不触碰真实业务库；
- 微信接入层 / AI 客户端 / Sender 全部用替身注入（不触发真实 GUI、不调 AI）；
- 错误响应统一断言 HTTP 状态码 + {"detail": 中文}。

运行（在 kefu-system 目录下）::

    python -m pytest tests/ -v
"""
from __future__ import annotations

import concurrent.futures
import json
import re
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import analyzer
from app.api import ALL_ROUTERS
from app.api import deps
from app.crypto import encrypt_api_key
from app.db import Database
from app.notifier import Notifier
from app.sender import SendTask


# ---------------------------------------------------------------------------
# 替身（微信服务 / 管线 / 发送服务）
# ---------------------------------------------------------------------------
class FakeWechatService:
    """WeChatService 替身：只覆盖 API 依赖到的接口，不触碰真实微信。"""

    def __init__(self) -> None:
        self.registered: List[str] = []
        self.unregistered: List[str] = []
        self.sessions: List[dict] = [
            {"username": "111@chatroom", "display_name": "外协群", "kind": "group"},
            {"username": "wxid_b", "display_name": "李四", "kind": "friend"},
        ]
        self.exists_map: dict = {}
        self.messages: List[dict] = [
            {"local_id": i, "type": "文本", "type_name": "文本", "sender_id": i,
             "sender_name": f"成员{i}", "create_time": "2026-11-02T14:00:00",
             "content": f"上下文消息{i}", "sort_seq": i}
            for i in range(1, 12)
        ]
        # 上下文接口故障注入（体验优化 4/5：拆分接口 + 失败重试）
        self.context_fail = False

    # --- 状态（/api/status） ---
    @property
    def listener_running(self) -> bool:
        return True

    @property
    def monitored_count(self) -> int:
        return 2

    @property
    def available(self) -> bool:
        return True

    @property
    def degraded_reason(self) -> str:
        return ""

    @property
    def watermark_saved_at(self) -> str:
        return "2026-11-02T14:00:00"

    def probe_wechat_window(self) -> bool:
        return True

    # --- 查询能力 ---
    def search_sessions(self, keyword: str = "", limit: int = 20,
                        kind: str = "all") -> list[dict]:
        items = self.sessions
        if kind in ("group", "friend"):
            items = [s for s in items if s["kind"] == kind]
        if keyword:
            items = [s for s in items
                     if keyword in s["display_name"] or keyword in s["username"]]
        return items[:limit]

    def get_messages(self, username: str, limit: int = 50,
                     before_seq: int = 0) -> list[dict]:
        return self.messages[:limit]

    def get_messages_with_error(self, username: str, limit: int = 50,
                                before_seq: int = 0,
                                retry_on_empty: bool = False
                                ) -> tuple[list[dict], str]:
        """上下文接口用的查询（替身：故障开关可控，重试逻辑由真实服务层测试覆盖）。"""
        if self.context_fail:
            return [], "微信数据库正忙（微信客户端正在写入），请稍后点击重新加载"
        return self.get_messages(username, limit=limit,
                                 before_seq=before_seq), ""

    def get_display_name(self, username: str) -> str:
        return username

    def build_log_displays(self, session_username: str,
                           input_text: str) -> tuple[str, str]:
        """分析日志展示字段（替身：原值返回，解析逻辑由服务层测试覆盖）。"""
        return session_username, input_text

    def session_exists(self, username: str) -> bool:
        return self.exists_map.get(username, True)

    # --- 监控联动 ---
    def register_target(self, username: str) -> None:
        self.registered.append(username)

    def unregister_target(self, username: str) -> None:
        self.unregistered.append(username)


class FakePipeline:
    """Pipeline 替身：仅暴露 /api/status 读取的状态字段。"""

    queue_max = 500
    dropped_total = 0
    busy = 0
    health: dict = {"last_error": "", "consecutive_failures": 0}

    @property
    def queue_size(self) -> int:
        return 0

    @property
    def analysis_health(self) -> dict:
        return dict(self.health)


class FakeSender:
    """Sender 替身：记录 submit 的任务，绝不触发 GUI。"""

    def __init__(self) -> None:
        self.tasks: List[SendTask] = []
        self.fail_submit = False

    def submit(self, task: SendTask) -> bool:
        if self.fail_submit:
            return False
        self.tasks.append(task)
        return True

    @property
    def pending(self) -> int:
        return len(self.tasks)


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------
@pytest.fixture()
def env(tmp_path):
    """构建被测 app：真实路由 + 替身依赖 + 临时 SQLite。"""
    db = Database(str(tmp_path / "api_test.sqlite3"))
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


def insert_opp(db: Database, *, type_id: Optional[int] = None,
               type_name: str = "机械加工订单",
               session: str = "111@chatroom", session_name: str = "加工群",
               sender_wxid: str = "wxid_a", sender_name: str = "张三",
               summary: str = "需要加工铝合金件500件",
               confidence: int = 85) -> dict:
    """插入一条商机（默认 pending）。"""
    return db.insert_opportunity(
        session_username=session, session_name=session_name,
        sender_wxid=sender_wxid, sender_name=sender_name,
        window_start="2026-11-02T14:02:00", window_end="2026-11-02T14:05:00",
        merged_count=2, raw_text="[14:02] 有谁能做铝合金CNC",
        type_id=type_id, type_name=type_name, summary=summary,
        key_quote="有谁能做铝合金CNC", confidence=confidence)


def wait_until(predicate, timeout: float = 3.0) -> bool:
    """轮询等待（后台线程注册监听为异步，测试需等待其生效）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


LIST_ITEM_KEYS = {"id", "session_username", "session_name", "sender_wxid",
                  "sender_name", "window_end", "type_id", "type_name",
                  "summary", "key_quote", "confidence", "direction",
                  "status", "created_at",
                  # 反馈 9：行内「未读」标识（内存态，默认 False）
                  "unread"}
# 契约变更（体验优化 4）：详情与上下文拆分 —— 详情接口不再返回 context 字段，
# 上下文由独立接口 GET /api/opportunities/{id}/context 提供（items + error）
DETAIL_EXTRA_KEYS = {"window_start", "merged_count", "raw_text", "key_quote",
                     "reply_content", "replied_at",
                     # 反馈 6：群消息原文的发送者显示名版（不再显示 wxid）
                     "raw_text_display"}


# ---------------------------------------------------------------------------
# 契约测试：/api/status、会话搜索、消息查询
# ---------------------------------------------------------------------------
class TestSystemContract:
    def test_status_fields_complete(self, env):
        """设计文档 3.2：/api/status 返回字段完整性。"""
        resp = env.client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()
        for key in ("listener_running", "monitored_count", "wechat", "queue",
                    "worker", "ai_configured", "watermark_saved_at",
                    "analysis_health"):
            assert key in body, f"缺少字段 {key}"
        assert set(body["wechat"]) == {"alive", "visible"}
        assert set(body["queue"]) == {"size", "max", "dropped_total"}
        assert set(body["worker"]) == {"busy", "max"}
        # 监控配额（优化 2：/api/status 新增 monitor_quota）
        assert set(body["monitor_quota"]) == {"used", "max"}
        assert body["monitor_quota"]["used"] == 0
        assert body["monitor_quota"]["max"] == 200
        # AI 分析健康度（优化 10：/api/status 新增 analysis_health）
        assert set(body["analysis_health"]) == {"last_error",
                                                "consecutive_failures"}
        assert body["analysis_health"]["last_error"] == ""
        assert body["analysis_health"]["consecutive_failures"] == 0
        assert body["queue"]["max"] == 500
        assert body["listener_running"] is True
        assert body["monitored_count"] == 2

    def test_status_analysis_health_from_pipeline(self, env):
        """优化 10：analysis_health 取自 pipeline 快照（页面刷新后仍可见）。"""
        env.pipeline.health = {"last_error": "额度不足或触发限流",
                               "consecutive_failures": 3}
        body = env.client.get("/api/status").json()
        assert body["analysis_health"]["last_error"] == "额度不足或触发限流"
        assert body["analysis_health"]["consecutive_failures"] == 3

    def test_sessions_search(self, env):
        resp = env.client.get("/api/sessions/search", params={"keyword": "外协"})
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert set(items[0]) == {"username", "display_name", "kind",
                                 "openim", "corp"}
        assert items[0]["kind"] == "group"
        assert items[0]["openim"] is False   # 群聊不标记企业微信
        assert items[0]["corp"] == ""        # 群聊无企业简称

    def test_sessions_search_kind_filter(self, env):
        """优化 2：kind 查询参数按类型过滤（all/group/friend）。"""
        # 默认 all：群+好友都返回
        resp = env.client.get("/api/sessions/search")
        assert resp.status_code == 200
        assert {i["kind"] for i in resp.json()} == {"group", "friend"}
        # kind=group：只回群聊
        resp = env.client.get("/api/sessions/search", params={"kind": "group"})
        items = resp.json()
        assert len(items) == 1
        assert items[0]["username"] == "111@chatroom"
        # kind=friend：只回私聊
        resp = env.client.get("/api/sessions/search", params={"kind": "friend"})
        items = resp.json()
        assert len(items) == 1
        assert items[0]["username"] == "wxid_b"
        # kind 与 keyword 组合
        resp = env.client.get("/api/sessions/search",
                              params={"kind": "friend", "keyword": "李四"})
        assert [i["username"] for i in resp.json()] == ["wxid_b"]
        # 非法 kind → 422
        assert env.client.get(
            "/api/sessions/search", params={"kind": "bogus"}).status_code == 422

    def test_sessions_search_kind_edge_cases(self, env):
        """QA 补充：kind 参数边界（大小写 / 空串 / 空结果集）。

        契约定性：kind 校验为大小写敏感的正则（GROUP/Group/"" → 422），
        前端只发小写 group/friend，不受影响；合法 kind + 无匹配 → 200 []。
        """
        # 大写非法 → 422（契约：pattern 大小写敏感）
        for bad in ("GROUP", "Group", "Friend", ""):
            assert env.client.get(
                "/api/sessions/search", params={"kind": bad}
            ).status_code == 422, f"kind={bad!r} 应 422"
        # 合法 kind 但无匹配 → 200 + 空数组
        resp = env.client.get("/api/sessions/search",
                              params={"kind": "group", "keyword": "不存在的关键词"})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_sessions_search_limit_validation(self, env):
        # limit 越界（>100）→ 422；边界 100 → 200
        assert env.client.get(
            "/api/sessions/search", params={"limit": 500}).status_code == 422
        assert env.client.get(
            "/api/sessions/search", params={"limit": 100}).status_code == 200

    def test_messages_endpoint_fields(self, env):
        """设计文档 3.2：GET /api/messages/{username} 字段对齐。"""
        resp = env.client.get("/api/messages/111@chatroom", params={"limit": 5})
        assert resp.status_code == 200
        msgs = resp.json()
        assert len(msgs) == 5
        for m in msgs:
            assert set(m) == {"local_id", "type", "type_name", "sender_id",
                              "sender_name", "create_time", "content",
                              "sort_seq"}


# ---------------------------------------------------------------------------
# 契约测试：监控对象 CRUD
# ---------------------------------------------------------------------------
class TestMonitorTargetsContract:
    def test_full_crud_flow(self, env):
        # POST → 201 + 后台异步注册监听（优化 1：接口立即返回）
        resp = env.client.post("/api/monitor-targets",
                               json={"username": "111@chatroom"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["username"] == "111@chatroom"
        assert body["kind"] == "group"
        assert body["enabled"] == 1
        assert wait_until(lambda: env.svc.registered == ["111@chatroom"])
        target_id = body["id"]

        # GET 列表
        resp = env.client.get("/api/monitor-targets")
        assert resp.status_code == 200
        assert [t["id"] for t in resp.json()] == [target_id]

        # 重复新增 → 400 中文
        resp = env.client.post("/api/monitor-targets",
                               json={"username": "111@chatroom"})
        assert resp.status_code == 400
        assert "已" in resp.json()["detail"]

        # PATCH 停用 → 注销监听
        resp = env.client.patch(f"/api/monitor-targets/{target_id}",
                                json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] == 0
        assert env.svc.unregistered == ["111@chatroom"]

        # PATCH 不存在的对象 → 404 中文
        resp = env.client.patch("/api/monitor-targets/99999",
                                json={"enabled": True})
        assert resp.status_code == 404
        assert "监控对象不存在" in resp.json()["detail"]

        # PATCH 缺 enabled 字段 → 422
        resp = env.client.patch(f"/api/monitor-targets/{target_id}", json={})
        assert resp.status_code == 422

        # DELETE → 204，再 PATCH → 404
        resp = env.client.delete(f"/api/monitor-targets/{target_id}")
        assert resp.status_code == 204
        assert env.svc.unregistered.count("111@chatroom") == 2
        resp = env.client.delete(f"/api/monitor-targets/{target_id}")
        assert resp.status_code == 404

    def test_friend_kind_detection(self, env):
        resp = env.client.post("/api/monitor-targets",
                               json={"username": "wxid_b"})
        assert resp.status_code == 201
        assert resp.json()["kind"] == "friend"

    def test_monitor_quota_cap_400(self, env):
        """优化 2：监控数量达上限后 POST 返回 400 + 中文 detail。"""
        from app.config import MAX_MONITOR_TARGETS
        for i in range(MAX_MONITOR_TARGETS):
            env.db.insert_target(f"wxid_cap_{i}", f"联系人{i}", "friend")
        resp = env.client.post("/api/monitor-targets",
                               json={"username": "wxid_overflow"})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert f"监控对象已达上限 {MAX_MONITOR_TARGETS} 个" in detail
        assert "请先移除部分再添加" in detail
        # 移除一个后恢复可添加
        assert env.client.delete("/api/monitor-targets/1").status_code == 204
        resp = env.client.post("/api/monitor-targets",
                               json={"username": "wxid_overflow"})
        assert resp.status_code == 201
        # 配额随 used 变化
        body = env.client.get("/api/status").json()
        assert body["monitor_quota"]["used"] == MAX_MONITOR_TARGETS


class TestMonitorSettingsContract:
    """优化 3：监控上限配置化（monitor.max_targets，10~2000）。"""

    def test_monitor_settings_roundtrip(self, env):
        # 默认 200
        resp = env.client.get("/api/settings/monitor")
        assert resp.status_code == 200
        assert resp.json() == {"max_targets": 200}
        # 保存 500 → 回读 + 配额即时生效
        resp = env.client.put("/api/settings/monitor", json={"max_targets": 500})
        assert resp.status_code == 200
        assert resp.json()["max_targets"] == 500
        assert env.db.get_setting("monitor.max_targets") == "500"
        body = env.client.get("/api/status").json()
        assert body["monitor_quota"]["max"] == 500
        # 越界 → 422
        assert env.client.put("/api/settings/monitor",
                              json={"max_targets": 9}).status_code == 422
        assert env.client.put("/api/settings/monitor",
                              json={"max_targets": 2001}).status_code == 422

    def test_quota_cap_uses_settings_value(self, env):
        """配额校验使用 settings 里的上限值（非硬编码常量）。"""
        env.db.set_setting("monitor.max_targets", "12")
        for i in range(12):
            env.db.insert_target(f"wxid_m12_{i}", f"联系人{i}", "friend")
        resp = env.client.post("/api/monitor-targets",
                               json={"username": "wxid_m12_over"})
        assert resp.status_code == 400
        assert "监控对象已达上限 12 个" in resp.json()["detail"]
        body = env.client.get("/api/status").json()
        assert body["monitor_quota"] == {"used": 12, "max": 12}

    def test_quota_invalid_settings_falls_back(self, env):
        """settings 里是非法值时回退常量 200（防御性）。"""
        env.db.set_setting("monitor.max_targets", "not-a-number")
        body = env.client.get("/api/status").json()
        assert body["monitor_quota"]["max"] == 200


# ---------------------------------------------------------------------------
# 契约测试：商机类型 CRUD
# ---------------------------------------------------------------------------
class TestOpportunityTypesContract:
    def test_full_crud_flow(self, env):
        # POST → 201
        resp = env.client.post("/api/opportunity-types", json={
            "name": "机械加工订单", "description": "寻找机械加工的采购需求"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["enabled"] == 1
        assert "updated_at" in body
        type_id = body["id"]

        # enabled=False 创建
        resp = env.client.post("/api/opportunity-types", json={
            "name": "停用类型", "description": "d", "enabled": False})
        assert resp.status_code == 201
        assert resp.json()["enabled"] == 0

        # GET 列表
        resp = env.client.get("/api/opportunity-types")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

        # PATCH 仅改描述（部分更新）
        resp = env.client.patch(f"/api/opportunity-types/{type_id}",
                                json={"description": "新描述，后续消息生效"})
        assert resp.status_code == 200
        assert resp.json()["description"].startswith("新描述")
        assert resp.json()["name"] == "机械加工订单"

        # PATCH 空描述 → 422 中文
        resp = env.client.patch(f"/api/opportunity-types/{type_id}",
                                json={"description": "   "})
        assert resp.status_code == 422

        # PATCH 不存在 → 404 中文
        resp = env.client.patch("/api/opportunity-types/99999",
                                json={"name": "x"})
        assert resp.status_code == 404
        assert "商机类型不存在" in resp.json()["detail"]

        # DELETE → 204；再删 → 404
        assert env.client.delete(
            f"/api/opportunity-types/{type_id}").status_code == 204
        assert env.client.delete(
            f"/api/opportunity-types/{type_id}").status_code == 404

    def test_create_empty_fields_422(self, env):
        resp = env.client.post("/api/opportunity-types",
                               json={"name": "", "description": "d"})
        assert resp.status_code == 422
        resp = env.client.post("/api/opportunity-types", json={"name": "n"})
        assert resp.status_code == 422  # 缺 description


# ---------------------------------------------------------------------------
# 契约测试：设置接口（AI / analysis / notify）
# ---------------------------------------------------------------------------
class FakeAIClient:
    """analyzer.AIClient 替身（测试连接用）。"""

    last_kwargs: dict = {}
    result: tuple = (True, "连接成功（模型：mock-model）", 42)

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout_sec: int = 60, temperature: float = 0.2,
                 client=None, disable_thinking: bool = True) -> None:
        FakeAIClient.last_kwargs = {
            "base_url": base_url, "api_key": api_key, "model": model,
            "timeout_sec": timeout_sec, "temperature": temperature,
            "disable_thinking": disable_thinking,
        }

    def test_connection(self):
        return FakeAIClient.result


class TestSettingsContract:
    def test_ai_roundtrip_and_mask(self, env):
        # 初始为空配置
        resp = env.client.get("/api/settings/ai")
        assert resp.status_code == 200
        assert resp.json()["api_key_masked"] == ""

        # 保存（含合规确认）
        resp = env.client.put("/api/settings/ai", json={
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test-12345678",
            "model": "deepseek-chat", "timeout_sec": 30,
            "temperature": 0.2, "confirm_compliance": True})
        assert resp.status_code == 200

        # 回读：Key 只回掩码，不落明文
        body = env.client.get("/api/settings/ai").json()
        assert body["base_url"] == "https://api.deepseek.com/v1"
        assert body["model"] == "deepseek-chat"
        assert body["timeout_sec"] == 30
        masked = body["api_key_masked"]
        assert "sk-test-12345678" not in masked
        assert masked.startswith("sk-t") and "****" in masked

        # 库内是 Fernet 密文而非明文（PRD P0-3）
        stored = env.db.get_setting("ai.api_key_enc")
        assert stored != "sk-test-12345678"
        assert stored.startswith("gAAAA")
        # 合规确认落库
        assert env.db.get_setting("compliance.confirmed") == "1"

        # 留空 Key 不变更
        resp = env.client.put("/api/settings/ai", json={
            "base_url": "https://api.deepseek.com/v1", "api_key": "",
            "model": "deepseek-chat", "timeout_sec": 30, "temperature": 0.2})
        assert resp.status_code == 200
        assert env.db.get_setting("ai.api_key_enc") == stored

        # 参数越界 → 422
        resp = env.client.put("/api/settings/ai", json={
            "base_url": "x", "model": "m", "timeout_sec": 1,
            "temperature": 0.2})
        assert resp.status_code == 422
        resp = env.client.put("/api/settings/ai", json={
            "base_url": "x", "model": "m", "timeout_sec": 60,
            "temperature": 9.9})
        assert resp.status_code == 422

    def test_ai_test_connection_success(self, env, monkeypatch):
        env.db.set_setting("ai.base_url", "https://mock.local/v1")
        env.db.set_setting("ai.model", "mock-model")
        env.db.set_setting("ai.api_key_enc", encrypt_api_key("sk-xyz-123456"))
        monkeypatch.setattr(analyzer, "AIClient", FakeAIClient)
        resp = env.client.post("/api/settings/ai/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["model"] == "mock-model"
        assert body["latency_ms"] == 42
        # 用解密后的 Key 构建客户端
        assert FakeAIClient.last_kwargs["api_key"] == "sk-xyz-123456"

    def test_ai_test_connection_failure(self, env, monkeypatch):
        env.db.set_setting("ai.base_url", "https://mock.local/v1")
        env.db.set_setting("ai.model", "mock-model")
        env.db.set_setting("ai.api_key_enc", encrypt_api_key("sk-xyz-123456"))
        FakeAIClient.result = (False, "连接失败：超时", 100)
        monkeypatch.setattr(analyzer, "AIClient", FakeAIClient)
        body = env.client.post("/api/settings/ai/test").json()
        assert body["ok"] is False
        assert body["message"].startswith("连接失败")
        FakeAIClient.result = (True, "连接成功（模型：mock-model）", 42)

    def test_ai_test_connection_unconfigured(self, env):
        body = env.client.post("/api/settings/ai/test").json()
        assert body["ok"] is False
        assert "尚未配置" in body["message"]

    def test_analysis_roundtrip_and_validation(self, env):
        resp = env.client.get("/api/settings/analysis")
        assert resp.status_code == 200
        assert resp.json()["strictness"] == "balanced"
        # 优化 1/7：微窗口默认 15s（5~600），商机合并窗口默认 180s
        assert resp.json()["merge_window_sec"] == 15
        assert resp.json()["opp_merge_window_sec"] == 180

        resp = env.client.put("/api/settings/analysis", json={
            "strictness": "strict", "min_confidence": 75,
            "prescreen_enabled": True, "prescreen_keywords": "采购,求购",
            "prescreen_regex": "", "merge_window_sec": 120,
            "opp_merge_window_sec": 600})
        assert resp.status_code == 200
        body = resp.json()
        assert body["strictness"] == "strict"
        assert body["min_confidence"] == 75
        assert body["prescreen_enabled"] is True
        assert body["merge_window_sec"] == 120
        assert body["opp_merge_window_sec"] == 600
        assert env.db.get_setting("analysis.opp_merge_window_sec") == "600"

        # 部分更新：缺省时回退 schemas 默认（微窗口 15 / 商机合并窗口 180）
        resp = env.client.put("/api/settings/analysis", json={
            "strictness": "balanced", "min_confidence": 60,
            "prescreen_enabled": False})
        assert resp.status_code == 200
        assert resp.json()["merge_window_sec"] == 15
        assert resp.json()["opp_merge_window_sec"] == 180

        # 非法档位 / 越界门槛 / 越界窗口 / 缺必填 → 422
        bad_bodies = [
            {"strictness": "loose", "min_confidence": 60,
             "prescreen_enabled": False},
            {"strictness": "strict", "min_confidence": 101,
             "prescreen_enabled": False},
            {"strictness": "strict", "min_confidence": 60,
             "prescreen_enabled": False, "merge_window_sec": 4},
            {"strictness": "strict", "min_confidence": 60,
             "prescreen_enabled": False, "merge_window_sec": 601},
            {"strictness": "strict", "min_confidence": 60,
             "prescreen_enabled": False, "opp_merge_window_sec": 20},
            {"strictness": "strict", "min_confidence": 60,
             "prescreen_enabled": False, "opp_merge_window_sec": 1801},
            {"strictness": "strict", "min_confidence": 60},  # 缺 prescreen_enabled
        ]
        for bad in bad_bodies:
            resp = env.client.put("/api/settings/analysis", json=bad)
            assert resp.status_code == 422, f"应 422：{bad}"

    def test_merge_window_min_boundary_5(self, env):
        """优化 7：微窗口下限放宽到 5 秒（边界值合法）。"""
        resp = env.client.put("/api/settings/analysis", json={
            "strictness": "balanced", "min_confidence": 60,
            "prescreen_enabled": False, "merge_window_sec": 5})
        assert resp.status_code == 200
        assert resp.json()["merge_window_sec"] == 5
        assert env.db.get_setting("analysis.merge_window_sec") == "5"

    def test_merge_window_legacy_default_migrated(self, tmp_path):
        """优化 7：旧库存的出厂默认 60s 一次性迁移为 15s；用户自定义值不动。"""
        db1 = Database(str(tmp_path / "migrate_legacy.sqlite3"))
        # 模拟旧库：值为出厂默认 60，且尚无迁移标记
        db1.set_setting("analysis.merge_window_sec", "60")
        db1._conn.execute(
            "DELETE FROM settings WHERE key='runtime.merge_window_migrated'")
        db1._conn.commit()
        db1.close()
        # 重新打开：迁移生效
        db2 = Database(str(tmp_path / "migrate_legacy.sqlite3"))
        assert db2.get_setting("analysis.merge_window_sec") == "15"
        # 再次打开（已迁移）：不会被二次改写；用户显式改回 60 也保留
        db2.set_setting("analysis.merge_window_sec", "60")
        db2.close()
        db3 = Database(str(tmp_path / "migrate_legacy.sqlite3"))
        assert db3.get_setting("analysis.merge_window_sec") == "60"
        db3.close()

    def test_notify_roundtrip(self, env):
        resp = env.client.put("/api/settings/notify",
                              json={"sound_enabled": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert env.db.get_bool("notify.sound_enabled") is True
        resp = env.client.put("/api/settings/notify",
                              json={"sound_enabled": False})
        assert env.db.get_bool("notify.sound_enabled") is False


# ---------------------------------------------------------------------------
# 商机生命周期：筛选 → 详情 → 状态
# ---------------------------------------------------------------------------
class TestOpportunityLifecycle:
    def test_list_filters_and_pagination(self, env):
        t1 = env.db.insert_type("机械加工订单", "d1")
        t2 = env.db.insert_type("原料求购", "d2")
        opps = [
            insert_opp(env.db, type_id=t1["id"], session="111@chatroom",
                       sender_name="张三"),
            insert_opp(env.db, type_id=t2["id"], type_name="原料求购",
                       session="222@chatroom",
                       session_name="原料群", sender_name="李四",
                       summary="求购铝锭一批"),
            insert_opp(env.db, type_id=t1["id"], session="111@chatroom",
                       sender_wxid="wxid_c", sender_name="王五"),
            insert_opp(env.db, type_id=t2["id"], type_name="原料求购",
                       session="222@chatroom",
                       session_name="原料群", sender_name="赵六"),
            insert_opp(env.db, type_id=t1["id"], session="111@chatroom",
                       sender_name="钱七"),
        ]
        env.db.update_opportunity_status(opps[4]["id"], "ignored")

        # 不带筛选：默认排除 ignored
        resp = env.client.get("/api/opportunities")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 4
        assert len(body["items"]) == 4
        for item in body["items"]:
            assert set(item) == LIST_ITEM_KEYS

        # session 筛选
        body = env.client.get(
            "/api/opportunities", params={"session": "222@chatroom"}).json()
        assert body["total"] == 2
        assert all(i["session_username"] == "222@chatroom"
                   for i in body["items"])

        # type_id 筛选
        body = env.client.get(
            "/api/opportunities", params={"type_id": t2["id"]}).json()
        assert body["total"] == 2
        assert all(i["type_name"] == "原料求购" for i in body["items"])

        # status 筛选（含 ignored）
        body = env.client.get(
            "/api/opportunities", params={"status": "ignored"}).json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == opps[4]["id"]

        # keyword 筛选（命中 summary）
        body = env.client.get(
            "/api/opportunities", params={"keyword": "求购铝锭"}).json()
        assert body["total"] == 1
        assert body["items"][0]["sender_name"] == "李四"

        # 分页
        body = env.client.get(
            "/api/opportunities", params={"page": 1, "page_size": 2}).json()
        assert body["total"] == 4 and len(body["items"]) == 2
        body_p2 = env.client.get(
            "/api/opportunities", params={"page": 2, "page_size": 2}).json()
        assert len(body_p2["items"]) == 2
        assert {i["id"] for i in body["items"]}.isdisjoint(
            i["id"] for i in body_p2["items"])

    def test_list_invalid_params_422(self, env):
        assert env.client.get(
            "/api/opportunities", params={"status": "bogus"}).status_code == 422
        assert env.client.get(
            "/api/opportunities", params={"type_id": 0}).status_code == 422
        assert env.client.get(
            "/api/opportunities", params={"page": 0}).status_code == 422
        assert env.client.get(
            "/api/opportunities", params={"page_size": 999}).status_code == 422

    def test_detail_and_context_split(self, env):
        """体验优化 4：详情与上下文拆分——详情不查微信库、无 context 字段。"""
        opp = insert_opp(env.db, type_id=None)
        env.notifier.mark_new_opportunity(opp["id"])
        assert env.notifier.unread_count == 1

        resp = env.client.get(f"/api/opportunities/{opp['id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == LIST_ITEM_KEYS | DETAIL_EXTRA_KEYS
        assert "context" not in body  # 契约：上下文已拆分到独立接口
        assert body["raw_text"].startswith("[14:02]")
        assert body["merged_count"] == 2
        assert body["key_quote"] == "有谁能做铝合金CNC"
        assert body["reply_content"] is None
        # 详情打开即清除未读（设计文档 3.2）
        assert env.notifier.unread_count == 0

        # 再次打开：未读本已清零，不重复广播
        env.client.get(f"/api/opportunities/{opp['id']}")
        assert env.notifier.unread_count == 0

    def test_context_endpoint_success(self, env):
        """体验优化 4：独立上下文接口正常返回（items + 空 error）。"""
        opp = insert_opp(env.db)
        resp = env.client.get(f"/api/opportunities/{opp['id']}/context")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"items", "error"}
        assert body["error"] == ""
        assert len(body["items"]) == 11
        for m in body["items"]:
            assert set(m) == {"local_id", "type", "type_name", "sender_id",
                              "sender_name", "create_time", "content",
                              "sort_seq"}

    def test_context_endpoint_busy_error(self, env):
        """体验优化 5：微信库忙 → error 返回明确中文，前端展示 + 重新加载。"""
        opp = insert_opp(env.db)
        env.svc.context_fail = True
        resp = env.client.get(f"/api/opportunities/{opp['id']}/context")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert "微信数据库正忙" in body["error"]
        assert "重新加载" in body["error"]
        env.svc.context_fail = False

    def test_context_endpoint_404(self, env):
        resp = env.client.get("/api/opportunities/99999/context")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "商机不存在"

    def test_detail_404(self, env):
        resp = env.client.get("/api/opportunities/99999")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "商机不存在"

    def test_patch_status_flow(self, env):
        opp = insert_opp(env.db)
        for status in ("replied", "ignored", "pending", "handled"):
            resp = env.client.patch(f"/api/opportunities/{opp['id']}",
                                    json={"status": status})
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == status
            assert set(body) == LIST_ITEM_KEYS
            assert env.db.get_opportunity(opp["id"])["status"] == status

    def test_patch_status_invalid_422(self, env):
        opp = insert_opp(env.db)
        resp = env.client.patch(f"/api/opportunities/{opp['id']}",
                                json={"status": "closed"})
        assert resp.status_code == 422
        # 错误信息为中文
        assert "detail" in resp.json()

    def test_patch_status_404(self, env):
        resp = env.client.patch("/api/opportunities/99999",
                                json={"status": "pending"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "商机不存在"


# ---------------------------------------------------------------------------
# 回复接口（sender 层 mock，不触发真实 GUI）
# ---------------------------------------------------------------------------
class TestReplyEndpoint:
    def _prepare(self, env) -> dict:
        """建监控对象 + 商机，返回商机记录。"""
        env.db.insert_target("111@chatroom", "加工群", "group")
        return insert_opp(env.db)

    def test_reply_404_unknown_opportunity(self, env):
        resp = env.client.post("/api/opportunities/99999/reply",
                               json={"text": "您好"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "商机不存在"
        assert env.sender.tasks == []

    def test_reply_blank_text_422(self, env):
        opp = self._prepare(env)
        resp = env.client.post(f"/api/opportunities/{opp['id']}/reply",
                               json={"text": "   "})
        assert resp.status_code == 422
        assert "回复内容不能为空" in resp.json()["detail"]
        assert env.sender.tasks == []

    def test_reply_target_validation_failure(self, env):
        """目标校验①失败：非监控对象且联系人库不存在 → 400 中文。"""
        opp = insert_opp(env.db, session="ghost@chatroom")
        env.svc.exists_map["ghost@chatroom"] = False
        resp = env.client.post(f"/api/opportunities/{opp['id']}/reply",
                               json={"text": "您好"})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "目标会话不存在" in detail
        assert env.sender.tasks == []

    def test_reply_accepted_202_and_task_fields(self, env):
        opp = self._prepare(env)
        resp = env.client.post(f"/api/opportunities/{opp['id']}/reply", json={
            "text": "  您好，这个订单我们可以做  ",
            "at": ["张三", " "], "quote": {"local_id": 1234}})
        assert resp.status_code == 202
        body = resp.json()
        assert body["ok"] is True
        assert "已加入发送队列" in body["message"]
        assert len(env.sender.tasks) == 1
        task = env.sender.tasks[0]
        assert task.opportunity_id == opp["id"]
        assert task.session_username == "111@chatroom"
        assert task.session_name == "加工群"
        assert task.text == "您好，这个订单我们可以做"  # 已 strip
        assert task.at == ["张三"]  # 空白项被过滤
        assert task.quote_local_id == 1234

    def test_reply_not_blocked_by_compliance(self, env):
        """设计文档 9.4：合规确认只约束分析管线；回复不受其阻断。"""
        env.db.set_setting("compliance.confirmed", "0")
        opp = self._prepare(env)
        resp = env.client.post(f"/api/opportunities/{opp['id']}/reply",
                               json={"text": "您好"})
        assert resp.status_code == 202
        assert len(env.sender.tasks) == 1

    def test_reply_text_overlong_422(self, env):
        opp = self._prepare(env)
        resp = env.client.post(f"/api/opportunities/{opp['id']}/reply",
                               json={"text": "x" * 2001})
        assert resp.status_code == 422

    def test_reply_session_exists_fallback(self, env):
        """不在监控列表，但联系人库存在 → 放行（目标校验① or 语义）。"""
        opp = insert_opp(env.db, session="wxid_friend")
        env.svc.exists_map["wxid_friend"] = True
        resp = env.client.post(f"/api/opportunities/{opp['id']}/reply",
                               json={"text": "您好"})
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# 契约测试：分析日志（体验优化 6：分页 + 筛选）
# ---------------------------------------------------------------------------
class TestAnalysisLogsContract:
    def _insert_logs(self, db: Database) -> list[int]:
        ids = []
        for i, (status, session, text) in enumerate([
            ("ok", "111@chatroom", "求丰田42431-0K090原厂件"),
            ("skipped", "222@chatroom", "今天天气不错聊聊家常"),
            ("error", "111@chatroom", "要加工一批铝合金件"),
            ("below_threshold", "333@chatroom", "求购铝锭100吨"),
        ]):
            result = (json.dumps({"hit": True, "type": "配件采购",
                                  "confidence": 85}, ensure_ascii=False)
                      if status == "ok" else "")
            ids.append(db.insert_analysis_log(
                session_username=session, sender_wxid=f"wxid_{i}",
                msg_count=i + 1, input_text=text, result=result,
                status=status,
                error="预筛未命中" if status == "skipped"
                else ("AI 接口调用失败" if status == "error" else ""),
                model="mock-model" if status in ("ok", "below_threshold") else "",
                latency_ms=800 if status in ("ok", "below_threshold") else None,
            ))
        return ids

    def test_list_pagination_and_fields(self, env):
        self._insert_logs(env.db)
        resp = env.client.get("/api/analysis-logs", params={"page_size": 2})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 4
        assert len(body["items"]) == 2
        # id 倒序（最新在前）
        assert body["items"][0]["id"] > body["items"][1]["id"]
        for item in body["items"]:
            assert set(item) == {"id", "created_at", "session_username",
                                 "session_display", "sender_wxid",
                                 "msg_count", "input_text",
                                 "input_text_display", "result", "status",
                                 "error", "model", "latency_ms",
                                 "agg_wait_ms", "queue_wait_ms", "e2e_ms"}

    def test_list_filters(self, env):
        self._insert_logs(env.db)
        # status 筛选
        body = env.client.get(
            "/api/analysis-logs", params={"status": "skipped"}).json()
        assert body["total"] == 1
        assert body["items"][0]["status"] == "skipped"
        assert "预筛未命中" in body["items"][0]["error"]
        # session 筛选（模糊）
        body = env.client.get(
            "/api/analysis-logs", params={"session": "111"}).json()
        assert body["total"] == 2
        assert all(i["session_username"] == "111@chatroom"
                   for i in body["items"])
        # keyword 筛选（input_text 模糊）
        body = env.client.get(
            "/api/analysis-logs", params={"keyword": "42431"}).json()
        assert body["total"] == 1
        assert "42431" in body["items"][0]["input_text"]
        # 组合筛选
        body = env.client.get("/api/analysis-logs", params={
            "session": "111", "keyword": "铝合金", "status": "error"}).json()
        assert body["total"] == 1

    def test_list_invalid_status_422(self, env):
        assert env.client.get("/api/analysis-logs",
                              params={"status": "bogus"}).status_code == 422
        assert env.client.get("/api/analysis-logs",
                              params={"page": 0}).status_code == 422
        assert env.client.get("/api/analysis-logs",
                              params={"page_size": 999}).status_code == 422

    def test_empty_list(self, env):
        body = env.client.get("/api/analysis-logs").json()
        assert body == {"total": 0, "items": []}


# ---------------------------------------------------------------------------
# 契约测试：漏判修正示例 analysis.correction_examples（体验优化 6）
# ---------------------------------------------------------------------------
class TestCorrectionExamplesContract:
    def test_default_empty_and_roundtrip(self, env):
        body = env.client.get("/api/settings/analysis").json()
        assert body["correction_examples"] == []
        resp = env.client.put("/api/settings/analysis", json={
            "strictness": "balanced", "min_confidence": 60,
            "prescreen_enabled": False,
            "correction_examples": ["求丰田42431-0K090原厂件，要50个"]})
        assert resp.status_code == 200
        assert resp.json()["correction_examples"] == \
            ["求丰田42431-0K090原厂件，要50个"]
        assert json.loads(env.db.get_setting("analysis.correction_examples")
                          ) == ["求丰田42431-0K090原厂件，要50个"]

    def test_fifo_cap_20(self, env):
        """上限淘汰：一次 PUT 25 条 → 保留最新 20 条（FIFO 淘汰最旧）。"""
        examples = [f"示例消息{i}" for i in range(25)]
        resp = env.client.put("/api/settings/analysis", json={
            "strictness": "balanced", "min_confidence": 60,
            "prescreen_enabled": False,
            "correction_examples": examples})
        assert resp.status_code == 200
        kept = resp.json()["correction_examples"]
        assert len(kept) == 20
        assert kept[0] == "示例消息5"   # 最旧 5 条被淘汰
        assert kept[-1] == "示例消息24"

    def test_empty_and_whitespace_filtered(self, env):
        resp = env.client.put("/api/settings/analysis", json={
            "strictness": "balanced", "min_confidence": 60,
            "prescreen_enabled": False,
            "correction_examples": ["  ", "有效示例", ""]})
        assert resp.json()["correction_examples"] == ["有效示例"]

    def test_partial_update_keeps_examples(self, env):
        """契约：PUT 缺省 correction_examples（None）= 不变更，不误清空。"""
        env.client.put("/api/settings/analysis", json={
            "strictness": "balanced", "min_confidence": 60,
            "prescreen_enabled": False,
            "correction_examples": ["示例A"]})
        resp = env.client.put("/api/settings/analysis", json={
            "strictness": "strict", "min_confidence": 70,
            "prescreen_enabled": True})
        assert resp.status_code == 200
        assert resp.json()["correction_examples"] == ["示例A"]
        # 显式 [] = 清空
        resp = env.client.put("/api/settings/analysis", json={
            "strictness": "strict", "min_confidence": 70,
            "prescreen_enabled": True, "correction_examples": []})
        assert resp.json()["correction_examples"] == []


# ---------------------------------------------------------------------------
# WebSocket 握手（TestClient 层；真实环境另有握手检查）
# ---------------------------------------------------------------------------
class TestWebsocketHandshake:
    def test_ws_connect_and_disconnect(self, env):
        with env.client.websocket_connect("/ws/events") as ws:
            assert len(env.notifier._clients) == 1
            ws.close()
        deadline = time.time() + 5
        while env.notifier._clients and time.time() < deadline:
            time.sleep(0.05)
        assert len(env.notifier._clients) == 0


# ---------------------------------------------------------------------------
# 全局异常处理器契约（Round 2 回归：共享约定 2 —— 错误统一 {"detail": 中文}）
# ---------------------------------------------------------------------------
class TestGlobalExceptionHandler:
    def test_unexpected_error_returns_chinese_detail_json(self):
        """未处理异常必须返回 500 + {"detail": 中文}，而非纯文本。

        回归背景：修复前 JSONResponse(detail=...) 参数错误会二次抛 TypeError，
        客户端只能收到纯文本 "Internal Server Error"（main.py 全局 handler）。
        """
        from starlette.requests import Request

        from app.main import app as real_app

        handler = real_app.exception_handlers[Exception]
        request = Request(
            {"type": "http", "method": "GET", "path": "/api/fake",
             "headers": [], "query_string": b""})
        import asyncio

        resp = asyncio.run(handler(request, RuntimeError("测试错误")))
        assert resp.status_code == 500
        body = resp.body.decode("utf-8")
        assert '"detail"' in body
        assert "服务器内部错误" in body
        assert "测试错误" in body


# ---------------------------------------------------------------------------
# 并发 / 边界（验证 db RLock：无死锁、无脏数据）
# ---------------------------------------------------------------------------
class TestConcurrency:
    def test_concurrent_settings_and_opportunities(self, env):
        """8 线程并发写 settings 与 opportunities：无异常、无死锁、计数一致。"""
        db = env.db
        errors: List[BaseException] = []

        def worker(i: int) -> None:
            try:
                for j in range(15):
                    db.set_setting(f"stress.{i}", str(j))
                    db.set_setting("stress.shared", f"w{i}-{j}")
                    opp = db.insert_opportunity(
                        session_username=f"s{i}@chatroom",
                        session_name=f"群{i}", sender_wxid=f"wx{i}",
                        sender_name=f"成员{i}",
                        window_start="2026-11-02T14:02:00",
                        window_end="2026-11-02T14:05:00",
                        merged_count=1, raw_text=f"内容{i}-{j}",
                        type_id=None, type_name="机械加工订单",
                        summary=f"要点{i}-{j}", key_quote="", confidence=80)
                    # 交叉读 + 状态写
                    db.list_opportunities(page=1, page_size=5)
                    db.update_opportunity_status(
                        opp["id"], "ignored" if j % 2 else "pending")
                    db.get_setting("stress.shared")
            except BaseException as exc:  # noqa: BLE001 - 收集全部异常
                errors.append(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker, i) for i in range(8)]
            # 时限 300s：仅容忍慢 I/O 环境（沙箱实测单次 WAL 提交 ~360ms，
            # 本测试 ~4300 次提交）；真死锁在任何时限内都不会完成，语义不变
            done, not_done = concurrent.futures.wait(futures, timeout=300)
        # 死锁检测：时限内必须全部完成
        assert not not_done, f"疑似死锁：{len(not_done)} 个线程未在时限内完成"
        assert errors == []

        # 数据完整性：8×15 条商机全部落库、状态合法
        total = db._conn.execute(
            "SELECT COUNT(*) FROM opportunities").fetchone()[0]
        assert total == 8 * 15
        bad = db._conn.execute(
            "SELECT COUNT(*) FROM opportunities "
            "WHERE status NOT IN ('pending','replied','ignored')"
        ).fetchone()[0]
        assert bad == 0
        # settings 无脏数据：每线程计数器为最终值；共享键可解析
        for i in range(8):
            assert db.get_setting(f"stress.{i}") == "14"
        assert re.fullmatch(r"w\d+-\d+", db.get_setting("stress.shared"))

    def test_concurrent_api_reads_and_writes(self, env):
        """多线程同时走 API 读列表 + 写设置，服务不崩、响应合法。"""
        opp = insert_opp(env.db)
        errors: List[BaseException] = []

        def reader(_):
            resp = env.client.get("/api/opportunities")
            assert resp.status_code == 200

        def writer(i):
            # 与前端 SettingsPage.tsx 一致：PUT 为全量替换，发送全部字段
            resp = env.client.put("/api/settings/analysis", json={
                "strictness": "balanced", "min_confidence": 60 + i % 20,
                "prescreen_enabled": False, "prescreen_keywords": "",
                "prescreen_regex": "", "merge_window_sec": 300})
            assert resp.status_code == 200

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(reader, i) for i in range(4)]
            futures += [pool.submit(writer, i) for i in range(4)]
            done, not_done = concurrent.futures.wait(futures, timeout=60)
        assert not not_done, "API 并发调用疑似死锁"
        for f in done:
            if f.exception():
                errors.append(f.exception())
        assert errors == []


# ---------------------------------------------------------------------------
# Bug C 回归：清理前可预览条数（dry_run）+ 清理结果落库留痕
# ---------------------------------------------------------------------------
def insert_log_at(db: Database, created_at: str,
                  session: str = "log@chatroom") -> int:
    """插入一条分析日志并把 created_at 改成指定时刻（格式同落库值）。"""
    log_id = db.insert_analysis_log(
        session_username=session, msg_count=1, status="skipped")
    db._conn.execute(
        "UPDATE analysis_logs SET created_at=? WHERE id=?", (created_at, log_id))
    db._conn.commit()
    return log_id


def days_ago_str(days_ago: int, clock: str = "12:00:00") -> str:
    """N 天前的时刻字符串（YYYY-MM-DD HH:MM:SS）。"""
    day = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    return f"{day} {clock}"


class TestPurgeSafety:
    """POST /api/system/purge 的 dry_run 预览与 GET /api/system/purge/last 留痕。"""

    def test_dry_run_previews_without_deleting(self, env):
        """dry_run=true：返回将删条数与截断点，但一条都不删。

        keep_days=1 语义为「保留最近 1 个自然日（含今天）」→ 昨天及更早都算待删。
        """
        insert_log_at(env.db, days_ago_str(3))    # 将被清理
        insert_log_at(env.db, days_ago_str(1))    # 昨天 → 也将被清理
        insert_log_at(env.db, days_ago_str(0))    # 今天 → 保留
        resp = env.client.post("/api/system/purge", json={
            "table": "analysis_logs", "keep_days": 1, "dry_run": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert body["deleted"] == 2
        assert "T" not in body["cutoff"]                     # 与落库同格式（空格分隔）
        assert body["cutoff"] == days_ago_str(0, "00:00:00")  # 今天 00:00:00
        assert env.db.list_analysis_logs()[0] == 3           # 预览不删数据
        assert env.client.get("/api/system/purge/last").json() == {}

    def test_real_purge_deletes_and_records_last(self, env):
        """dry_run=false：真删并写入 runtime.last_purge 留痕（不依赖日志）。"""
        insert_log_at(env.db, days_ago_str(3))
        insert_log_at(env.db, days_ago_str(1))
        keep = insert_log_at(env.db, days_ago_str(0))
        resp = env.client.post("/api/system/purge", json={
            "table": "analysis_logs", "keep_days": 1})
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is False
        assert body["deleted"] == 2
        remaining = {r["id"] for r in
                     env.db._conn.execute("SELECT id FROM analysis_logs")}
        assert remaining == {keep}

        last = env.client.get("/api/system/purge/last").json()
        assert last["table"] == "analysis_logs"
        assert last["deleted"] == 2
        assert last["keep_days"] == 1
        assert last["cutoff"] == body["cutoff"]
        assert last["at"]                                   # 清理时间非空

    def test_purge_opportunities_records_and_nulls_refs(self, env):
        """清理商机：日志保留但引用置空，留痕记录为 opportunities。"""
        opp = insert_opp(env.db)
        env.db._conn.execute(
            "UPDATE opportunities SET created_at=? WHERE id=?",
            (days_ago_str(400), opp["id"]))
        log_id = insert_log_at(env.db, days_ago_str(0))
        env.db._conn.execute(
            "UPDATE analysis_logs SET opportunity_id=? WHERE id=?",
            (opp["id"], log_id))
        env.db._conn.commit()
        resp = env.client.post("/api/system/purge", json={
            "table": "opportunities", "keep_days": 365, "dry_run": False})
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 1
        row = env.db._conn.execute(
            "SELECT opportunity_id FROM analysis_logs WHERE id=?",
            (log_id,)).fetchone()
        assert row["opportunity_id"] is None
        assert env.client.get("/api/system/purge/last").json()["table"] == \
            "opportunities"

    def test_last_purge_returns_empty_dict_when_missing(self, env):
        """从未清理过时返回 {}，前端按「暂无清理记录」展示。"""
        assert env.client.get("/api/system/purge/last").json() == {}

    def test_last_purge_tolerates_bad_json(self, env):
        """历史脏值（非 JSON）按无记录处理，不影响页面。"""
        env.db.set_setting("runtime.last_purge", "not-a-json")
        assert env.client.get("/api/system/purge/last").json() == {}
