# -*- coding: utf-8 -*-
"""群消息总结 T01–T04 关键用例（DB/DAO/预算/服务/REST）。

运行（仓库根）::

    python -m pytest --basetemp=D:/tmp/pt-summary-t01t04 kefu-system/tests/test_summary.py

全程使用临时 SQLite 与替身，不触碰真实业务库、不调真实 AI/微信。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import analyzer
from app.api import ALL_ROUTERS, deps
from app.api.deps import get_summary_service
from app.billing import estimate_cost, get_billing_settings
from app.crypto import encrypt_api_key
from app.db import Database
from app.notifier import Notifier
from app.summary import (
    BUDGET_HINT_NO_PRICE,
    SummaryOverBudgetError,
    SummaryService,
    build_system_prompt,
    build_user_message,
    parse_summary_json,
    parse_time_range,
    truncation_text,
)
from wechatauto.db import WeChatDB

START = "2026-11-20 09:00:00"
END = "2026-11-20 18:00:00"
JSON_REPLY = json.dumps({
    "reminders": ["客户投诉需立即处理"],
    "topics": ["价格咨询"],
    "comments": ["报价响应及时"],
    "followups": ["张三跟进报价"],
    "others": [],
    "closing": "整体沟通顺畅",
}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 替身与夹具
# ---------------------------------------------------------------------------
class FakeChat:
    """AIClient 替身：固定 JSON 输出 + 可配置的 usage/异常。"""

    def __init__(self, cfg: dict, reply: str = JSON_REPLY) -> None:
        self.cfg = cfg
        self.reply = reply
        self.last_usage: dict | None = {"prompt_tokens": 123, "completion_tokens": 45}
        self.calls: list[tuple[str, str]] = []

    def chat(self, system_prompt: str, user_message: str) -> str:
        self.calls.append((system_prompt, user_message))
        if self.reply == "__AI_ERROR__":
            raise analyzer.AIError("AI 接口调用失败：模拟故障")
        return self.reply


class FakeWechat:
    """WeChatService 替身：只实现总结链路用到的方法。"""

    def __init__(self) -> None:
        self.error = ""
        self.messages = [
            {
                "local_id": i,
                "type": "文本",
                "type_name": "文本",
                "sender_id": 10 + i,
                "sender_wxid": "wxid_a" if i % 2 else "wxid_b",
                "sender_name": "张三" if i % 2 else "李四",
                "create_time": f"2026-11-20T{9 + i // 4:02d}:{i * 7 % 60:02d}:00",
                "content": f"第{i}条关于报价与售后的讨论",
                "sort_seq": i,
            }
            for i in range(1, 9)
        ]
        self.calls: list[tuple] = []

    def get_messages_in_range(self, username, start_epoch, end_epoch,
                              limit=5000):
        self.calls.append((username, start_epoch, end_epoch, limit))
        return (list(self.messages), self.error)

    def display_name_with_corp(self, username: str) -> str:
        return "测试群"

    def resolve_group_member_display(self, session: str, wxid: str) -> str:
        return {"wxid_a": "张三", "wxid_b": "李四"}.get(wxid, wxid)


@pytest.fixture()
def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "summary_test.sqlite3"))
    yield database
    database.close()


@pytest.fixture()
def fake_wechat() -> FakeWechat:
    return FakeWechat()


def configure_ai(db: Database) -> None:
    """写入可用的 AI 配置（Key 为加密后的测试值）。"""
    db.set_setting("ai.base_url", "https://example.invalid/v1")
    db.set_setting("ai.model", "test-model")
    db.set_setting("ai.api_key_enc", encrypt_api_key("sk-test"))


def make_service(db: Database, wechat: FakeWechat, *,
                 chat: FakeChat | None = None) -> tuple[SummaryService, FakeChat]:
    client = chat or FakeChat({})
    service = SummaryService(db, wechat, lambda cfg: client)
    return service, client


# ---------------------------------------------------------------------------
# T01：建表 / DAO / 时间过滤 / 账单公式
# ---------------------------------------------------------------------------
def test_ddl_is_idempotent(tmp_path):
    path = str(tmp_path / "ddl.sqlite3")
    first = Database(path)
    first.close()
    second = Database(path)
    try:
        names = {r[0] for r in second._conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        assert {"group_summaries", "idx_gs_created", "idx_gs_session"} <= names
        second.insert_group_summary(
            session_username="g@chatroom", session_name="测试群",
            time_start=START, time_end=END, msg_count=2, member_count=2,
            content='{"closing":"ok"}')
        assert second.get_group_summary(1)["session_name"] == "测试群"
    finally:
        second.close()


def test_group_summary_dao_and_manual_purge(db: Database):
    row = db.insert_group_summary(
        session_username="g@chatroom", session_name="测试群",
        time_start=START, time_end=END, msg_count=3, member_count=2,
        sender_wxids='["wxid_a"]', style="concise",
        content='{"closing":"ok"}', truncated=True, truncated_original=9,
        prompt_tokens=10, completion_tokens=2)
    assert row["id"] > 0 and row["truncated"] == 1
    total, items = db.list_group_summaries(page=1, page_size=10,
                                           session="g@chatroom", date=row["created_at"][:10])
    assert total == 1 and items[0]["id"] == row["id"]
    assert db.list_group_summaries(session="other")[0] == 0
    assert db.delete_group_summary(9999) == 0

    # 手动 purge 白名单：截断点由 SQLite 本地时间计算，旧记录可预览/真删
    with db._lock:
        db._conn.execute(
            "UPDATE group_summaries SET created_at=? WHERE id=?",
            ("2020-01-01 00:00:00", row["id"]))
        db._conn.commit()
    assert db.count_older_than("group_summaries", 180) == 1
    assert db.delete_older_than("group_summaries", 180) == 1
    assert db.get_group_summary(row["id"]) is None


def test_get_messages_between_filters_and_merges(monkeypatch):
    """各分片 SQL 侧按 create_time 过滤，跨分片按 sort_seq 升序归并。"""
    db = object.__new__(WeChatDB)
    shards = []
    rows_by_shard = [
        [(101, 1, 2, 100, "早于范围", 0),
         (102, 1, 2, 150, "范围内A", 0),
         (103, 1, 2, 300, "晚于范围")],
        [(201, 1, 2, 120, "范围内B", 0),
         (202, 1, 2, 200, "范围内C", 0)],
    ]
    for idx, rows in enumerate(rows_by_shard):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE message_%d (local_id INT, local_type INT, "
            "real_sender_id INT, create_time INT, message_content TEXT, "
            "source INT, packed_info_data BLOB, compress_content BLOB, "
            "sort_seq INT)" % idx)
        conn.executemany(
            "INSERT INTO message_%d VALUES (?,?,?,?,?,?,?,?,?)" % idx,
            [(r[0], r[1], r[2], r[3], r[4], r[5] if len(r) > 5 else 0,
              None, None, r[0]) for r in rows])
        shards.append((conn, "message_%d" % idx))
    monkeypatch.setattr(db, "_run_msg_query",
                        lambda user, build: build(shards))
    result = db.get_messages_between("g@chatroom", 110, 250, limit=10)
    assert [m["content"] for m in result] == ["范围内A", "范围内B", "范围内C"]
    assert all(110 <= m["create_time"] <= 250 for m in result)
    for conn, _ in shards:
        conn.close()


def test_billing_formula_matches_legacy(db: Database):
    billing = get_billing_settings(db)
    assert billing["unit"] == "per_m"
    assert estimate_cost(2_000_000, 1_000_000, billing) == 0.0  # 单价未配
    db.set_setting("billing.unit", "per_k")
    db.set_setting("billing.input_price", "0.01")
    db.set_setting("billing.output_price", "0.03")
    billing = get_billing_settings(db)
    assert estimate_cost(2000, 1000, billing) == pytest.approx(0.05)
    db.set_setting("billing.unit", "per_m")
    db.set_setting("billing.input_price", "10")
    db.set_setting("billing.output_price", "30")
    assert estimate_cost(2_000_000, 1_000_000,
                         get_billing_settings(db)) == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# T02：预算 / 截断 / 解析 / 全链路
# ---------------------------------------------------------------------------
def test_budget_modes_hint_and_ratio(db: Database, fake_wechat):
    svc, _ = make_service(db, fake_wechat)
    stats = {"est_prompt_tokens": 100, "est_output_tokens": 2000}
    b = svc.check_budget(stats)
    assert b["budget_mode"] == "money" and b["budget_value"] == 1.0
    assert b["budget_hint"] == BUDGET_HINT_NO_PRICE  # 单价为 0 不得静默

    db.set_setting("billing.input_price", "10")
    db.set_setting("billing.output_price", "30")
    b = svc.check_budget(stats)
    assert b["est_tokens"] == 2100 and b["est_cost"] == pytest.approx(0.061)
    assert b["over_budget"] is False and b["budget_text"] == "¥1.00"

    db.set_setting(KEY_BUDGET_MODE := "summary.budget_mode", "tokens")
    db.set_setting("summary.budget_tokens", "100")
    b = svc.check_budget(stats)
    assert b["over_budget"] is True and b["usage_ratio"] == pytest.approx(21.0)
    assert b["budget_text"] == "100 tokens" and b["budget_hint"] == ""


def test_apply_truncate_decrements_and_limit(db: Database, fake_wechat):
    svc, _ = make_service(db, fake_wechat)
    db.set_setting("summary.budget_mode", "tokens")
    # 20 条×10 字符 + 固定输出 2000：20 条=2200 超 2185，18 条=2180 落入预算
    db.set_setting("summary.budget_tokens", "2185")
    db.set_setting("summary.over_limit_action", "truncate_recent")
    db.set_setting("summary.truncate_recent_n", "20")
    msgs = [{"content": "abcdefghij",
             "sender_wxid": "wxid_a", "sender_name": "张三",
             "create_time": f"2026-11-20T10:{i:02d}:00",
             "local_id": i, "type": "文本", "sort_seq": i} for i in range(20)]
    stats = {"est_prompt_tokens": sum(len(m["content"]) for m in msgs) + 2000,
             "est_output_tokens": 2000, "msg_count": 20}
    kept, new_stats, original = svc.apply_truncate(msgs, stats)
    assert original == 20 and len(kept) == 18  # 20 → 18（10% 递减）
    assert new_stats["msg_count"] == len(kept)
    assert svc.check_budget(new_stats)["over_budget"] is False

    db.set_setting("summary.budget_tokens", "1")
    with pytest.raises(SummaryOverBudgetError, match="无法在预算内生成"):
        svc.apply_truncate(msgs, stats)


def test_prompt_and_json_fallback():
    system = build_system_prompt("bullets", "重点看价格")
    assert "每节" in system and "重点看价格" in system
    assert '"reminders"' in system  # 花括号已被正确转义
    user = build_user_message("测试群", START, END, ["张三"], 2, "语料",
                              truncated=2, truncated_original=9)
    assert truncation_text(2, 9) in user and "消息（共 2 条" in user
    assert parse_summary_json("```json\n" + JSON_REPLY + "\n```")["closing"] == "整体沟通顺畅"
    assert parse_summary_json("前言" + JSON_REPLY + "后记")["reminders"]
    assert parse_summary_json('{"topics":[],"closing":["甲","乙"]}')["closing"] == "甲 乙"
    with pytest.raises(analyzer.AIError):
        parse_summary_json("不是 JSON")
    with pytest.raises(ValueError):
        parse_time_range("2026-11-20T09:00:00", END)


def test_generate_full_chain_and_export(db: Database, fake_wechat):
    configure_ai(db)
    svc, client = make_service(db, fake_wechat)
    row = svc.generate("g@chatroom", START, END, ["wxid_a"], "concise", "看价格")
    assert row["id"] > 0 and row["session_name"] == "测试群"
    assert row["msg_count"] == 4 and row["style"] == "concise"
    assert row["prompt_tokens"] == 123 and row["completion_tokens"] == 45
    assert "重点" not in client.calls[0][0] or "看价格" in client.calls[0][0]
    assert db.get_group_summary(row["id"]) is not None
    assert db.usage_by_date(datetime.now().strftime("%Y-%m-%d"))["calls"] >= 1

    txt = svc.render_txt(row)
    html_text = svc.render_html(row)
    for text in (txt, html_text):
        assert "重要提醒" in text and "待跟进事项" in text and "结语" in text
    assert "@media print" in html_text
    assert "&lt;" in svc.render_html({**row, "session_name": "<script>"})

    # 截断落库 → 四处统一标注文案
    db.set_setting("summary.budget_mode", "tokens")
    db.set_setting("summary.budget_tokens", "10")
    db.set_setting("summary.over_limit_action", "truncate_recent")
    with pytest.raises(SummaryOverBudgetError):
        svc.generate("g@chatroom", START, END)


# ---------------------------------------------------------------------------
# T03：REST API
# ---------------------------------------------------------------------------
@pytest.fixture()
def api(db: Database, fake_wechat):
    configure_ai(db)
    service, client = make_service(db, fake_wechat)
    deps.configure(db=db, wechat=fake_wechat, pipeline=SimpleNamespace(),
                   sender=SimpleNamespace(), notifier=Notifier())
    app = FastAPI()
    for router in ALL_ROUTERS:
        app.include_router(router)
    app.dependency_overrides[get_summary_service] = lambda: service
    yield SimpleNamespace(client=TestClient(app), db=db, svc=service,
                          chat=client, wechat=fake_wechat), app
    app.dependency_overrides.clear()
    deps.reset_for_tests()


def test_api_members_and_bad_requests(api):
    env, _ = api
    r = env.client.get("/api/summary/members", params={
        "session": "g@chatroom", "start": START, "end": END,
        "senders": "wxid_a"})
    assert r.status_code == 200
    data = r.json()
    assert data["msg_count"] == 4 and len(data["items"]) == 2
    assert data["budget_hint"] == BUDGET_HINT_NO_PRICE
    assert env.client.get("/api/summary/members", params={
        "session": "g@chatroom", "start": "bad", "end": END}).status_code == 400

    env.wechat.error = "微信数据库不可用，请确认微信已登录"
    r = env.client.get("/api/summary/members", params={
        "session": "g@chatroom", "start": START, "end": END})
    assert r.status_code == 503 and "微信" in r.json()["detail"]


def test_api_generate_error_mapping(api):
    env, _ = api
    payload = {"session": "g@chatroom", "start": START, "end": END}
    assert env.client.post("/api/summary/generate", json=payload).status_code == 200
    assert env.client.post("/api/summary/generate", json={
        **payload, "style": "wrong"}).status_code == 400
    env.wechat.messages = []
    assert env.client.post("/api/summary/generate", json=payload).status_code == 400

    env.wechat.messages = [{"local_id": 1, "type": "文本", "content": "hi",
                            "sender_wxid": "wxid_a", "sender_name": "张三",
                            "create_time": "2026-11-20T10:00:00"}]
    env.db.set_setting("summary.budget_mode", "tokens")
    env.db.set_setting("summary.budget_tokens", "1")
    r = env.client.post("/api/summary/generate", json=payload)
    assert r.status_code == 413 and "超预算" in r.json()["detail"]

    env.db.set_setting("summary.budget_tokens", "60000")
    env.chat.reply = "__AI_ERROR__"
    r = env.client.post("/api/summary/generate", json=payload)
    assert r.status_code == 502
    env.wechat.error = "微信数据库不可用，请确认微信已登录"
    assert env.client.post("/api/summary/generate", json=payload).status_code == 503


def test_api_history_export_and_settings(api):
    env, _ = api
    created = env.client.post("/api/summary/generate", json={
        "session": "g@chatroom", "start": START, "end": END}).json()
    sid = created["id"]
    listed = env.client.get("/api/summary/history", params={"date": "2020-01-01"})
    assert listed.status_code == 200 and listed.json()["total"] == 0
    assert env.client.get("/api/summary/history", params={"date": "bad"}).status_code == 400
    assert env.client.get("/api/summary/history/999999").status_code == 404
    assert env.client.get(f"/api/summary/history/{sid}").json()["id"] == sid

    r = env.client.get(f"/api/summary/history/{sid}/export", params={"format": "txt"})
    assert r.status_code == 200 and "群消息总结" in r.text
    assert "filename*=UTF-8''" in r.headers["content-disposition"]
    assert env.client.get(f"/api/summary/history/{sid}/export",
                          params={"format": "pdf"}).status_code == 400
    assert env.client.get("/api/summary/history/999999/export").status_code == 404

    settings = env.client.get("/api/summary/settings").json()
    assert settings["summary.budget_mode"] == "money"
    updated = env.client.put("/api/summary/settings", json={
        "summary.budget_mode": "tokens", "summary.truncate_recent_n": "50"})
    assert updated.status_code == 200
    assert updated.json()["summary.budget_mode"] == "tokens"
    assert env.client.put("/api/summary/settings", json={
        "ai.base_url": "http://evil"}).status_code == 422
    # 设置页唯一通路同时读写预算 5 键 + 定时 2 键（P1 才做语义化 schedules 封装）
    assert settings["summary.schedule_enabled"] == "0"
    assert settings["summary.schedule_configs"] == "[]"
    saved = env.client.put("/api/summary/settings", json={
        "summary.schedule_enabled": "1",
        "summary.schedule_configs": json.dumps([{
            "session": "g@chatroom", "display_name": "测试群",
            "enabled": True, "time": "18:30"}], ensure_ascii=False)})
    assert saved.status_code == 200
    assert saved.json()["summary.schedule_enabled"] == "1"
    assert "18:30" in saved.json()["summary.schedule_configs"]
    assert env.client.put("/api/summary/settings", json={
        "summary.schedule_configs": "[]%"}).status_code == 422
    assert env.client.put("/api/summary/settings", json={
        "summary.budget_mode": "wrong"}).status_code == 422

    assert env.client.delete(f"/api/summary/history/{sid}").json() == {"ok": True}
    assert env.client.delete(f"/api/summary/history/{sid}").status_code == 404


def test_get_messages_between_respects_limit(monkeypatch):
    """limit 只作病态输入兜底：范围内按 sort_seq 升序截取。"""
    db = object.__new__(WeChatDB)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE message_0 (local_id INT, local_type INT, real_sender_id INT, "
        "create_time INT, message_content TEXT, source INT, "
        "packed_info_data BLOB, compress_content BLOB, sort_seq INT)")
    conn.executemany(
        "INSERT INTO message_0 VALUES (?,?,?,?,?,?,?,?,?)",
        [(i, 1, 0, 100 + i, f"msg{i}", 0, None, None, i) for i in range(1, 6)])
    conn.commit()
    monkeypatch.setattr(db, "_run_msg_query",
                        lambda user, build: build([(conn, "message_0")]))
    got = db.get_messages_between("g@chatroom", 100, 200, limit=3)
    assert [m["content"] for m in got] == ["msg1", "msg2", "msg3"]
    conn.close()


def test_api_retention_and_manual_purge(api):
    """保留天数扩展与 group_summaries 手动清理复用现有端点。"""
    env, _ = api
    assert env.client.get("/api/system/retention").json()["summaries_days"] == 180
    updated = env.client.put("/api/system/retention", json={"summaries_days": 30})
    assert updated.status_code == 200 and updated.json()["summaries_days"] == 30
    created = env.client.post("/api/summary/generate", json={
        "session": "g@chatroom", "start": START, "end": END}).json()
    # 模拟超过保留期的历史行（真实用户场景下由时间流逝产生）
    with env.db._lock:
        env.db._conn.execute(
            "UPDATE group_summaries SET created_at='2020-01-01 00:00:00' WHERE id=?",
            (created["id"],))
        env.db._conn.commit()
    preview = env.client.post("/api/system/purge", json={
        "table": "group_summaries", "keep_days": 180, "dry_run": True})
    assert preview.status_code == 200 and preview.json()["deleted"] == 1
    real = env.client.post("/api/system/purge", json={
        "table": "group_summaries", "keep_days": 180, "dry_run": False})
    assert real.status_code == 200 and real.json()["deleted"] == 1
    assert env.client.get(f"/api/summary/history/{created['id']}").status_code == 404
    assert env.client.get("/api/system/purge/last").json()["table"] == "group_summaries"


def test_no_send_or_auto_cleanup_routes():
    """用户定稿取消发送回群，且不新增任何自动清理端点。"""
    from app.api import ALL_ROUTERS
    paths = [getattr(r, "path", "") for router in ALL_ROUTERS for r in router.routes]
    assert not any("send" in p.lower() for p in paths)
    assert not any("cleanup" in p.lower() or "auto_purge" in p.lower() for p in paths)
