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
import re
import time
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
    def search_sessions(self, keyword: str = "", limit: int = 20) -> list[dict]:
        if keyword:
            return [s for s in self.sessions
                    if keyword in s["display_name"] or keyword in s["username"]]
        return self.sessions[:limit]

    def get_messages(self, username: str, limit: int = 50,
                     before_seq: int = 0) -> list[dict]:
        return self.messages[:limit]

    def get_display_name(self, username: str) -> str:
        return username

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

    @property
    def queue_size(self) -> int:
        return 0


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


LIST_ITEM_KEYS = {"id", "session_username", "session_name", "sender_wxid",
                  "sender_name", "window_end", "type_id", "type_name",
                  "summary", "confidence", "status", "created_at"}
DETAIL_EXTRA_KEYS = {"window_start", "merged_count", "raw_text", "key_quote",
                     "reply_content", "replied_at", "context"}


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
                    "worker", "ai_configured", "watermark_saved_at"):
            assert key in body, f"缺少字段 {key}"
        assert set(body["wechat"]) == {"alive", "visible"}
        assert set(body["queue"]) == {"size", "max", "dropped_total"}
        assert set(body["worker"]) == {"busy", "max"}
        assert body["queue"]["max"] == 500
        assert body["listener_running"] is True
        assert body["monitored_count"] == 2

    def test_sessions_search(self, env):
        resp = env.client.get("/api/sessions/search", params={"keyword": "外协"})
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert set(items[0]) == {"username", "display_name", "kind"}
        assert items[0]["kind"] == "group"

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
        # POST → 201 + 即时注册监听
        resp = env.client.post("/api/monitor-targets",
                               json={"username": "111@chatroom"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["username"] == "111@chatroom"
        assert body["kind"] == "group"
        assert body["enabled"] == 1
        assert env.svc.registered == ["111@chatroom"]
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
                 client=None) -> None:
        FakeAIClient.last_kwargs = {
            "base_url": base_url, "api_key": api_key, "model": model,
            "timeout_sec": timeout_sec, "temperature": temperature,
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
        assert resp.json()["merge_window_sec"] == 300

        resp = env.client.put("/api/settings/analysis", json={
            "strictness": "strict", "min_confidence": 75,
            "prescreen_enabled": True, "prescreen_keywords": "采购,求购",
            "prescreen_regex": "", "merge_window_sec": 120})
        assert resp.status_code == 200
        body = resp.json()
        assert body["strictness"] == "strict"
        assert body["min_confidence"] == 75
        assert body["prescreen_enabled"] is True
        assert body["merge_window_sec"] == 120

        # 部分更新：merge_window_sec 缺省时回退默认 300（schemas default=300）
        resp = env.client.put("/api/settings/analysis", json={
            "strictness": "balanced", "min_confidence": 60,
            "prescreen_enabled": False})
        assert resp.status_code == 200
        assert resp.json()["merge_window_sec"] == 300

        # 非法档位 / 越界门槛 / 越界窗口 / 缺必填 → 422
        bad_bodies = [
            {"strictness": "loose", "min_confidence": 60,
             "prescreen_enabled": False},
            {"strictness": "strict", "min_confidence": 101,
             "prescreen_enabled": False},
            {"strictness": "strict", "min_confidence": 60,
             "prescreen_enabled": False, "merge_window_sec": 10},
            {"strictness": "strict", "min_confidence": 60},  # 缺 prescreen_enabled
        ]
        for bad in bad_bodies:
            resp = env.client.put("/api/settings/analysis", json=bad)
            assert resp.status_code == 422, f"应 422：{bad}"

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

    def test_detail_with_context_and_unread_clear(self, env):
        opp = insert_opp(env.db, type_id=None)
        env.notifier.mark_new_opportunity(opp["id"])
        assert env.notifier.unread_count == 1

        resp = env.client.get(f"/api/opportunities/{opp['id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == LIST_ITEM_KEYS | DETAIL_EXTRA_KEYS
        assert body["raw_text"].startswith("[14:02]")
        assert body["merged_count"] == 2
        assert body["key_quote"] == "有谁能做铝合金CNC"
        assert body["reply_content"] is None
        assert len(body["context"]) == 11  # 前后各 5 条 + 命中消息位
        assert body["context"][0]["content"] == "上下文消息1"
        # 详情打开即清除未读（设计文档 3.2）
        assert env.notifier.unread_count == 0

        # 再次打开：未读本已清零，不重复广播
        env.client.get(f"/api/opportunities/{opp['id']}")
        assert env.notifier.unread_count == 0

    def test_detail_404(self, env):
        resp = env.client.get("/api/opportunities/99999")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "商机不存在"

    def test_patch_status_flow(self, env):
        opp = insert_opp(env.db)
        for status in ("replied", "ignored", "pending"):
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
            done, not_done = concurrent.futures.wait(
                futures, timeout=60)
        # 死锁检测：60 秒内必须全部完成
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
