# -*- coding: utf-8 -*-
"""QA 独立验证用例（严过关 / Edward）——群消息总结 T01–T04。

与工程师的 ``tests/test_summary.py`` 刻意使用**不同的数据、不同的入口与
不同的断言视角**：不复用其夹具/替身，全部自建，重点打边界、口径一致性与
"无副作用" 这类工程师自测最容易漏掉的路径。

运行（仓库根）::

    /d/my_conda_envs/wechatauto/python.exe -m pytest \
        --basetemp=D:/tmp/pt-summary-qa kefu-system/tests/test_summary_qa.py

全程使用临时 SQLite + 替身；不触碰真实业务库、不调真实 AI / 微信、不写业务源码。
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import analyzer
from app.api import ALL_ROUTERS, deps
from app.api.deps import get_db, get_summary_service
from app.crypto import encrypt_api_key
from app.db import Database
from app.notifier import Notifier
from app.schemas import PurgeRequest
from app.summary import (
    BUDGET_HINT_NO_PRICE,
    OVER_BUDGET_UNREACHABLE,
    SUMMARY_MSG_LIMIT,
    SummaryOverBudgetError,
    SummaryScheduler,
    SummaryService,
    parse_summary_json,
    parse_time_range,
    truncation_text,
)
from wechatauto.db import WeChatDB

# 跨天窗口：从 19 日 23:00 到 20 日 01:00（PRD 要求支持跨天任意范围）
QA_START = "2026-11-19 23:00:00"
QA_END = "2026-11-20 01:00:00"
QA_SESSION = "qa-group@chatroom"
QA_START_DAY = "2026-11-20 09:00:00"
QA_END_DAY = "2026-11-20 18:00:00"

QA_REPLY = json.dumps({
    "reminders": ["QA-提醒"],
    "topics": ["QA-话题"],
    "comments": ["QA-点评"],
    "followups": ["QA-待跟进"],
    "others": [],
    "closing": "QA-结语",
}, ensure_ascii=False)

QA_DISPLAY = {"wxid_a": "张三(群显示名)", "wxid_b": "李四(群显示名)"}

APP_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 自建夹具 / 替身（独立于 test_summary.py）
# ---------------------------------------------------------------------------
def qa_messages(count: int = 20, content: str = "abcdefghij") -> list[dict]:
    """构造跨天消息：前半段 19 日 23 点、后半段 20 日 00 点，每条 10 字。"""
    out: list[dict] = []
    for i in range(count):
        if i < count // 2:
            minute = i
            stamp = f"2026-11-19T23:{minute:02d}:00"
            moment = datetime(2026, 11, 19, 23, minute, 0)
        else:
            minute = i - count // 2
            stamp = f"2026-11-20T00:{minute:02d}:00"
            moment = datetime(2026, 11, 20, 0, minute, 0)
        out.append({
            "local_id": i + 1,
            "type": "文本",
            "sender_wxid": "wxid_a" if i % 2 == 0 else "wxid_b",
            "sender_name": "张三" if i % 2 == 0 else "李四",
            "create_time": stamp,
            "content": content,
            "sort_seq": i,
            "epoch": int(moment.timestamp()),
        })
    return out


class QaChat:
    """AIClient 替身：记录调用、可注入 usage 缺失 / 失败 / 自定义回复。"""

    def __init__(self, *, fail: bool = False, reply: str = QA_REPLY,
                 usage: dict | None = None) -> None:
        self.fail = fail
        self.reply = reply
        self.last_usage = ({"prompt_tokens": 17, "completion_tokens": 5}
                           if usage is None else usage)
        self.calls: list[tuple[str, str]] = []

    def chat(self, system_prompt: str, user_message: str) -> str:
        self.calls.append((system_prompt, user_message))
        if self.fail:
            raise analyzer.AIError("QA 模拟：AI 网关不可用")
        return self.reply


class QaWechat:
    """WeChatService 替身：记录 epoch 入参与成员显示名解析调用。"""

    def __init__(self, messages: list[dict] | None = None,
                 error: str = "") -> None:
        self.messages = list(qa_messages() if messages is None else messages)
        self.error = error
        self.range_calls: list[tuple] = []
        self.display_calls: list[tuple] = []
        self.member_display: dict[str, str] = {}

    def get_messages_in_range(self, username, start_epoch, end_epoch,
                              limit=5000):
        self.range_calls.append((username, int(start_epoch),
                                 int(end_epoch), int(limit)))
        return list(self.messages), self.error

    def display_name_with_corp(self, username: str) -> str:
        return f"QA群-{username}"

    def resolve_group_member_display(self, session: str, wxid: str) -> str:
        self.display_calls.append((session, wxid))
        return self.member_display.get(wxid, wxid)


@pytest.fixture()
def qa_db(tmp_path) -> Database:
    database = Database(str(tmp_path / "qa_summary.sqlite3"))
    yield database
    database.close()


def qa_configure_ai(db: Database) -> None:
    db.set_setting("ai.base_url", "https://qa.invalid/v1")
    db.set_setting("ai.model", "qa-model")
    db.set_setting("ai.api_key_enc", encrypt_api_key("sk-qa"))


def qa_service(db: Database, wechat: QaWechat,
               chat: QaChat | None = None) -> SummaryService:
    return SummaryService(db, wechat, lambda cfg: (chat or QaChat()))


@pytest.fixture()
def qa_api(qa_db):
    """独立装配的 API 环境（不复用工程师的 api fixture）。"""
    wechat = QaWechat(qa_messages())
    chat = QaChat()
    service = qa_service(qa_db, wechat, chat)
    qa_configure_ai(qa_db)
    deps.configure(db=qa_db, wechat=wechat, pipeline=SimpleNamespace(),
                   sender=SimpleNamespace(), notifier=Notifier())
    app = FastAPI()
    for router in ALL_ROUTERS:
        app.include_router(router)
    app.dependency_overrides[get_summary_service] = lambda: service
    env = SimpleNamespace(client=TestClient(app), db=qa_db, wechat=wechat,
                          chat=chat, svc=service, app=app)
    yield env
    app.dependency_overrides.clear()
    deps.reset_for_tests()


def qa_gen_payload(**overrides) -> dict:
    payload = {"session": QA_SESSION, "start": QA_START_DAY,
               "end": QA_END_DAY}
    payload.update(overrides)
    return payload


def qa_stats(msgs: list[dict]) -> dict:
    chars = sum(len(str(m.get("content") or "")) for m in msgs)
    return {"est_prompt_tokens": chars, "est_output_tokens": 2000,
            "est_tokens": chars + 2000, "msg_count": len(msgs)}


# ===========================================================================
# A. 预算边界（双口径临界值 / 固定提示 / 递减下限 / 两值互不干扰）
# ===========================================================================
class TestQaBudgetBoundary:
    def test_tokens_budget_exact_equality_and_plus_one(self, qa_db):
        """token 口径：正好等于预算不超，超 1 个 token 预算即超限。"""
        svc = qa_service(qa_db, QaWechat())
        qa_db.set_setting("summary.budget_mode", "tokens")
        stats = qa_stats(qa_messages(20))  # 200 字 + 固定输出 2000 = 2200

        qa_db.set_setting("summary.budget_tokens", "2200")
        exact = svc.check_budget(stats)
        assert exact["est_tokens"] == 2200
        assert exact["over_budget"] is False, "正好等于预算不应判超限"
        assert exact["usage_ratio"] == pytest.approx(1.0)

        qa_db.set_setting("summary.budget_tokens", "2199")
        plus_one = svc.check_budget(stats)
        assert plus_one["over_budget"] is True, "超 1 token 应判超限"
        assert plus_one["usage_ratio"] > 1.0

        qa_db.set_setting("summary.budget_tokens", "2201")
        under = svc.check_budget(stats)
        assert under["over_budget"] is False

    def test_money_budget_exact_equality_and_fixed_hint(self, qa_db):
        """金额口径：整元边界不超；单价未配置时固定提示原文返回。"""
        svc = qa_service(qa_db, QaWechat())
        qa_db.set_setting("billing.unit", "per_k")
        qa_db.set_setting("billing.input_price", "1")
        qa_db.set_setting("billing.output_price", "0")
        qa_db.set_setting("summary.budget_mode", "money")
        stats = {"est_prompt_tokens": 1000, "est_output_tokens": 2000}

        qa_db.set_setting("summary.budget_money", "1.0")
        exact = svc.check_budget(stats)
        assert exact["est_cost"] == pytest.approx(1.0)
        assert exact["over_budget"] is False, "金额正好等于预算不应判超限"
        assert exact["budget_hint"] == ""

        qa_db.set_setting("summary.budget_money", "0.99")
        assert svc.check_budget(stats)["over_budget"] is True

        qa_db.set_setting("billing.input_price", "0")
        qa_db.set_setting("billing.output_price", "0")
        hinted = svc.check_budget(stats)
        assert hinted["budget_hint"] == BUDGET_HINT_NO_PRICE
        assert BUDGET_HINT_NO_PRICE == "请先在设置页配置模型单价，否则金额预算不生效"
        assert hinted["est_cost"] == 0.0 and hinted["over_budget"] is False

        qa_db.set_setting("summary.budget_mode", "tokens")
        assert svc.check_budget(stats)["budget_hint"] == "", "token 口径不提示单价"

    def test_budget_hint_visible_via_api(self, qa_api):
        """budget_hint 必须经 members 响应原样回传，前端才能展示。"""
        env = qa_api
        env.db.set_setting("summary.budget_mode", "money")
        r = env.client.get("/api/summary/members", params={
            "session": QA_SESSION, "start": QA_START_DAY,
            "end": QA_END_DAY})
        assert r.status_code == 200
        assert r.json()["budget_hint"] == BUDGET_HINT_NO_PRICE
        env.db.set_setting("billing.input_price", "8")
        assert env.client.get("/api/summary/members", params={
            "session": QA_SESSION, "start": QA_START_DAY,
            "end": QA_END_DAY}).json()["budget_hint"] == ""
        env.db.set_setting("billing.input_price", "0")
        env.db.set_setting("billing.output_price", "9")
        assert svc_hint(env) == "", "仅输出单价配置即不提示"

    def test_money_and_tokens_values_stored_separately(self, qa_api):
        """切换口径只换读取的键，两个预算值分开存、互不改写。"""
        env = qa_api
        assert env.client.put("/api/summary/settings", json={
            "summary.budget_money": "0.25",
            "summary.budget_tokens": "12345",
        }).status_code == 200

        stats = {"est_prompt_tokens": 0, "est_output_tokens": 2000}
        env.db.set_setting("summary.budget_mode", "money")
        money = env.svc.check_budget(stats)
        assert money["budget_value"] == 0.25 and money["budget_text"] == "¥0.25"

        env.db.set_setting("summary.budget_mode", "tokens")
        tokens = env.svc.check_budget(stats)
        assert tokens["budget_value"] == 12345.0
        assert tokens["budget_text"] == "12,345 tokens"

        env.db.set_setting("summary.budget_mode", "money")
        assert env.svc.check_budget(stats)["budget_value"] == 0.25

        saved = env.client.put("/api/summary/settings",
                               json={"summary.budget_money": "0.5"}).json()
        assert saved["summary.budget_tokens"] == "12345"
        saved = env.client.put("/api/summary/settings",
                               json={"summary.budget_tokens": "999"}).json()
        assert saved["summary.budget_money"] == "0.5"

    def test_truncate_below_ten_still_over_returns_413(self, qa_api):
        """截断策略：N 递减到 <10 仍超预算 → 服务抛错 → API 413。"""
        env = qa_api
        env.db.set_setting("summary.budget_mode", "tokens")
        env.db.set_setting("summary.budget_tokens", "1")
        env.db.set_setting("summary.over_limit_action", "truncate_recent")
        env.db.set_setting("summary.truncate_recent_n", "200")

        with pytest.raises(SummaryOverBudgetError,
                           match="无法在预算内生成"):
            env.svc.apply_truncate(qa_messages(20), qa_stats(qa_messages(20)))

        r = env.client.post("/api/summary/generate", json=qa_gen_payload())
        assert r.status_code == 413
        assert r.json()["detail"] == OVER_BUDGET_UNREACHABLE
        assert env.chat.calls == [], "超预算不得调用 AI"
        assert env.db.list_group_summaries()[0] == 0, "失败不得落库"


def svc_hint(env) -> str:
    """读取当前 token 口径之外的金额提示（仅测试辅助）。"""
    return env.svc.check_budget(
        {"est_prompt_tokens": 20, "est_output_tokens": 2000})["budget_hint"]


# ===========================================================================
# B. 截断语义四处一致 + truncated_original 落库
# ===========================================================================
class TestQaTruncationSemantics:
    def test_four_surfaces_share_one_wording_and_persist(self, qa_api):
        """结果区 / txt / html / AI 语料首行同文案，truncated_original 落库。"""
        env = qa_api
        env.db.set_setting("summary.budget_mode", "tokens")
        env.db.set_setting("summary.budget_tokens", "2100")
        env.db.set_setting("summary.over_limit_action", "truncate_recent")
        env.db.set_setting("summary.truncate_recent_n", "20")

        row = env.svc.generate(QA_SESSION, QA_START_DAY, QA_END_DAY)
        assert row["truncated"] is True
        assert row["truncated_original"] == 20, "截断前条数须为筛选结果条数"
        assert row["msg_count"] == 10, "20→18→…→10 递减后正好等于预算"

        stored = env.db.get_group_summary(row["id"])
        assert stored["truncated"] == 1 and stored["truncated_original"] == 20

        expected = "已截断：仅统计最近 10 条（原共 20 条）"
        assert truncation_text(10, 20) == expected

        # ① AI 语料首行（只出现一次，不被 build_user_message 重复注入）
        user_message = env.chat.calls[0][1]
        assert user_message.count("已截断：仅统计最近") == 1
        assert expected in user_message
        lines = user_message.splitlines()
        first_corpus = next(i for i, ln in enumerate(lines) if ln.startswith("["))
        assert lines[first_corpus - 1] == expected

        # ② generate 结果行字段（前端结果区据此渲染统一文案）
        assert row["truncated"] and row["msg_count"] == 10

        # ③ txt ④ html，以及经 API 导出
        assert expected in env.svc.render_txt(row)
        assert expected in env.svc.render_html(row)
        for fmt in ("txt", "html"):
            exported = env.client.get(
                f"/api/summary/history/{row['id']}/export",
                params={"format": fmt})
            assert exported.status_code == 200
            assert expected in exported.text

        page = (APP_ROOT / "frontend/src/pages/GroupSummary.tsx").read_text(
            encoding="utf-8")
        assert re.search(r"已截断：仅统计最近 \$\{[^}]+\} 条", page)

    def test_truncation_text_suffix_rules(self):
        assert truncation_text(7, 0) == "已截断：仅统计最近 7 条"
        assert truncation_text(7, 7) == "已截断：仅统计最近 7 条（原共 7 条）"

    def test_parse_summary_json_fallback_edges(self):
        """JSON 容错：正文含花括号、closing 为数组、彻底非法时抛 AIError。"""
        with_braces = json.dumps(
            {"topics": ["含{花括号}的议题"], "closing": "ok"},
            ensure_ascii=False)
        assert parse_summary_json("说明文字 " + with_braces + " 尾注")[
            "topics"] == ["含{花括号}的议题"]
        assert parse_summary_json('{"closing":["甲","乙"]}')["closing"] == "甲 乙"
        with pytest.raises(analyzer.AIError):
            parse_summary_json("完全不是 JSON")


# ===========================================================================
# C. 时间口径铁律（跨天 / T 分隔 / SQL 整数比较）
# ===========================================================================
class TestQaTimeSemantics:
    def test_cross_day_range_passes_epoch_seconds(self, qa_api):
        """跨天时间范围必须原样换算成 epoch 秒并传给微信层。"""
        env = qa_api
        env.svc.members(QA_SESSION, QA_START, QA_END)
        _user, s_epoch, e_epoch, limit = env.wechat.range_calls[-1]
        assert s_epoch == int(datetime.strptime(QA_START,
                                                "%Y-%m-%d %H:%M:%S").timestamp())
        assert e_epoch == int(datetime.strptime(QA_END,
                                                "%Y-%m-%d %H:%M:%S").timestamp())
        assert e_epoch - s_epoch == 7200
        assert isinstance(s_epoch, int) and isinstance(e_epoch, int)
        assert limit == SUMMARY_MSG_LIMIT

    def test_t_separator_rejected_backend_and_normalized_in_corpus(self,
                                                                    qa_api):
        """后端拒绝 T 分隔（400）；语料行内的 T 被归一化为空格。"""
        env = qa_api
        with pytest.raises(ValueError, match="时间格式"):
            parse_time_range("2026-11-20T09:00:00", QA_END_DAY)
        r = env.client.get("/api/summary/members", params={
            "session": QA_SESSION, "start": "2026-11-20T09:00:00",
            "end": QA_END_DAY})
        assert r.status_code == 400
        assert r.json()["detail"] == "时间格式应为 YYYY-MM-DD HH:MM:SS"
        assert env.client.get("/api/summary/members", params={
            "session": QA_SESSION, "start": QA_START_DAY,
            "end": QA_END_DAY}).status_code == 200

        msg = {"content": "ab", "sender_name": "张三",
               "create_time": "2026-11-20T09:05:00"}
        corpus, stats = SummaryService.build_corpus([msg])
        assert corpus.startswith("[11-20 09:05] 张三: ab")
        assert stats["msg_count"] == 1 and stats["char_count"] == 2

    def test_wechatdb_uses_integer_params_no_python_t_string(self, monkeypatch):
        """微信库 SQL 内部只用整数 BETWEEN，不存在 Python 拼 T 字符串比较。"""
        wdb = object.__new__(WeChatDB)
        captured: dict = {}

        class FakeConn:
            def execute(self, sql, params):
                captured["sql"] = sql
                captured["params"] = tuple(params)
                return SimpleNamespace(fetchall=lambda: [])

        monkeypatch.setattr(wdb, "_run_msg_query",
                            lambda user, build: build([(FakeConn(), "message_0")]))
        start = int(datetime(2026, 11, 19, 23, 0, 0).timestamp())
        end = int(datetime(2026, 11, 20, 1, 0, 0).timestamp())
        assert wdb.get_messages_between(QA_SESSION, start, end, limit=7) == []

        assert "create_time BETWEEN ? AND ?" in captured["sql"]
        assert captured["params"] == (start, end, 7)
        assert all(isinstance(v, int) for v in captured["params"])
        # 不得出现形如 'YYYY-MM-DDT...' 的 Python 拼时间字符串
        assert not re.search(r"\d{4}-\d{2}-\d{2}T", captured["sql"])
        src = __import__("inspect").getsource(WeChatDB.get_messages_between)
        assert "strftime" not in src and 'replace("T"' not in src

    def test_frontend_converts_datetime_local_before_submit(self):
        """前端在提交前把 datetime-local 的 T 换成空格并补 :00。"""
        page = (APP_ROOT / "frontend/src/pages/GroupSummary.tsx").read_text(
            encoding="utf-8")
        assert "replace('T', ' ')" in page
        assert "text.length === 16" in page and "`${text}:00`" in page


# ===========================================================================
# D. 成员筛选（多选 / 空=全部 / 统计口径 / 显示名走取名链）
# ===========================================================================
class TestQaMemberFilter:
    def test_multi_select_empty_all_and_counts(self, qa_api):
        env = qa_api
        env.wechat.messages = [
            {"sender_wxid": "wxid_a", "sender_name": "甲原文",
             "content": "12", "create_time": "2026-11-20 10:00:00"},
            {"sender_wxid": "wxid_a", "sender_name": "甲原文",
             "content": "34", "create_time": "2026-11-20 10:01:00"},
            {"sender_wxid": "wxid_a", "sender_name": "甲原文",
             "content": "5", "create_time": "2026-11-20 10:02:00"},
            {"sender_wxid": "wxid_b", "sender_name": "乙原文",
             "content": "abcd", "create_time": "2026-11-20 10:03:00"},
            {"sender_wxid": "wxid_b", "sender_name": "乙原文",
             "content": "ef", "create_time": "2026-11-20 10:04:00"},
            {"sender_wxid": "wxid_c", "sender_name": "丙原文",
             "content": "xyz", "create_time": "2026-11-20 10:05:00"},
        ]
        env.wechat.member_display = {"wxid_a": "甲别名", "wxid_b": "乙别名"}

        all_data = env.svc.members(QA_SESSION, QA_START_DAY, QA_END_DAY)
        assert [x["sender_wxid"] for x in all_data["items"]] == [
            "wxid_a", "wxid_b", "wxid_c"]
        assert all_data["msg_count"] == 6
        assert all_data["char_count"] == 2 + 2 + 1 + 4 + 2 + 3
        assert all_data["est_prompt_tokens"] == all_data["char_count"]

        by_wxid = {x["sender_wxid"]: x for x in all_data["items"]}
        assert by_wxid["wxid_a"]["display_name"] == "甲别名", "显示名须走取名链"
        assert by_wxid["wxid_b"]["display_name"] == "乙别名"
        assert by_wxid["wxid_c"]["display_name"] == "丙原文", "无法解析时回退消息显示名"
        assert (QA_SESSION, "wxid_a") in env.wechat.display_calls

        multi = env.svc.members(QA_SESSION, QA_START_DAY, QA_END_DAY,
                                ["wxid_a", "wxid_b"])
        assert len(multi["items"]) == 3, "items 恒为全量成员"
        assert multi["msg_count"] == 5
        assert multi["char_count"] == 2 + 2 + 1 + 4 + 2

        via_api = env.client.get("/api/summary/members", params={
            "session": QA_SESSION, "start": QA_START_DAY, "end": QA_END_DAY,
            "senders": "wxid_a,wxid_a,wxid_b"}).json()
        assert via_api["msg_count"] == 5, "逗号列表须去重"

    def test_generate_member_count_unfiltered_and_names_snapshotted(self,
                                                                    qa_api):
        env = qa_api
        env.wechat.messages = [
            {"sender_wxid": "wxid_a", "sender_name": "甲别名",
             "content": "12", "create_time": "2026-11-20 10:00:00"},
            {"sender_wxid": "wxid_b", "sender_name": "乙别名",
             "content": "34", "create_time": "2026-11-20 10:01:00"},
            {"sender_wxid": "wxid_c", "sender_name": "丙别名",
             "content": "5", "create_time": "2026-11-20 10:02:00"},
        ]
        row = env.svc.generate(QA_SESSION, QA_START_DAY, QA_END_DAY, ["wxid_a"])
        assert row["member_count"] == 3, "成员数按未筛选口径"
        assert row["msg_count"] == 1
        assert json.loads(row["sender_wxids"]) == ["wxid_a"]
        assert json.loads(row["member_names"]) == ["甲别名"]


# ===========================================================================
# E. 安全与错误映射
# ===========================================================================
class TestQaSecurityAndErrors:
    @staticmethod
    def inject_row(db: Database) -> dict:
        return db.insert_group_summary(
            session_username="evil@chatroom",
            session_name="<script>alert(1)</script>群",
            time_start=QA_START_DAY, time_end=QA_END_DAY,
            msg_count=2, member_count=1,
            content=json.dumps({
                "reminders": ["<img src=x onerror=alert(2)>"],
                "topics": [], "comments": [], "followups": [],
                "others": [], "closing": "<b>粗体结语</b> & A 与 B",
            }, ensure_ascii=False),
            truncated=True, truncated_original=5,
        )

    def test_html_export_escapes_group_name_and_sections(self, qa_api):
        env = qa_api
        row = self.inject_row(env.db)
        rendered = env.svc.render_html(row)
        assert "<script>" not in rendered and "&lt;script&gt;" in rendered
        assert "<img src=x onerror=alert(2)>" not in rendered
        assert "&lt;img" in rendered and "&lt;b&gt;" in rendered
        assert "&amp;" in rendered and "@media print" in rendered

        exported = env.client.get(
            f"/api/summary/history/{row['id']}/export",
            params={"format": "html"})
        assert exported.status_code == 200
        assert "<script>" not in exported.text and "&lt;img" in exported.text

    def test_rfc5987_filename_exact_and_ascii_fallback(self, qa_api):
        env = qa_api
        row = env.db.insert_group_summary(
            session_username="g@chatroom", session_name="测试群&A",
            time_start=QA_START_DAY, time_end=QA_END_DAY,
            msg_count=1, member_count=1, content='{"closing":"ok"}')
        base = ("群消息总结-测试群&A-2026-11-20-09-00-00"
                "至2026-11-20-18-00-00")
        for fmt in ("txt", "html"):
            r = env.client.get(f"/api/summary/history/{row['id']}/export",
                               params={"format": fmt})
            disposition = r.headers["content-disposition"]
            assert f'filename="summary.{fmt}"' in disposition
            encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition)
            assert encoded is not None
            assert unquote(encoded.group(1)) == f"{base}.{fmt}"

    def test_error_matrix(self, qa_api):
        env = qa_api
        client = env.client

        env.wechat.messages = []
        r = client.post("/api/summary/generate", json=qa_gen_payload())
        assert r.status_code == 400 and "没有消息" in r.json()["detail"]
        env.wechat.messages = qa_messages()  # 恢复，避免污染后续断言

        assert client.get("/api/summary/history/999999").status_code == 404
        assert client.delete("/api/summary/history/999999").status_code == 404
        created = client.post("/api/summary/generate",
                              json=qa_gen_payload()).json()
        assert client.get(f"/api/summary/history/{created['id']}/export",
                          params={"format": "pdf"}).status_code == 400
        assert client.get("/api/summary/history/999999/export").status_code == 404
        assert client.put("/api/summary/settings",
                          json={"summary.unknown_key": "x"}).status_code == 422
        assert client.get("/api/summary/members", params={
            "session": QA_SESSION, "start": QA_END_DAY,
            "end": QA_START_DAY}).status_code == 400
        assert client.post("/api/summary/generate", json=qa_gen_payload(
            style="nope")).status_code == 400
        assert client.post("/api/summary/generate", json=qa_gen_payload(
            custom_prompt="x" * 501)).status_code == 422

        env.db.set_setting("summary.budget_mode", "tokens")
        env.db.set_setting("summary.budget_tokens", "1")
        r = client.post("/api/summary/generate", json=qa_gen_payload())
        assert r.status_code == 413 and "超预算" in r.json()["detail"]

        env.db.set_setting("summary.budget_tokens", "60000")
        env.chat.fail = True
        r = client.post("/api/summary/generate", json=qa_gen_payload())
        assert r.status_code == 502 and "AI" in r.json()["detail"]

        env.chat.fail = False
        env.wechat.error = "微信数据库不可用，请确认微信已登录"
        r = client.post("/api/summary/generate", json=qa_gen_payload())
        assert r.status_code == 503 and "微信" in r.json()["detail"]
        r = client.get("/api/summary/members", params={
            "session": QA_SESSION, "start": QA_START_DAY, "end": QA_END_DAY})
        assert r.status_code == 503

    @pytest.mark.parametrize("payload", [
        {"summary.budget_mode": "BAD"},
        {"summary.over_limit_action": "BAD"},
        {"summary.budget_money": "-1"},
        {"summary.budget_tokens": "abc"},
        {"summary.truncate_recent_n": "-5"},
        {"summary.schedule_enabled": "2"},
        {"summary.schedule_configs": "{}"},
        {"summary.schedule_configs": '[{"time":"25:00","session":"g"}]'},
        {"summary.schedule_configs": '[{"time":"18:00"}]'},
    ])
    def test_settings_invalid_values_422(self, qa_api, payload):
        assert qa_api.client.put("/api/summary/settings",
                                 json=payload).status_code == 422

    def test_settings_roundtrip_and_schedules_endpoint_removed(self, qa_api):
        env = qa_api
        all_keys = {
            "summary.budget_mode": "tokens",
            "summary.budget_money": "2.5",
            "summary.budget_tokens": "30000",
            "summary.over_limit_action": "truncate_recent",
            "summary.truncate_recent_n": "77",
            "summary.schedule_enabled": "1",
            "summary.schedule_configs": json.dumps([
                {"session": QA_SESSION, "display_name": "QA群",
                 "enabled": True, "time": "18:30"}], ensure_ascii=False),
        }
        r = env.client.put("/api/summary/settings", json=all_keys)
        assert r.status_code == 200 and r.json() == all_keys
        assert env.client.get("/api/summary/settings").json() == all_keys
        assert env.client.get("/api/summary/schedules").status_code == 404

    def test_ai_failure_never_persists(self, qa_api):
        env = qa_api
        env.chat.fail = True
        assert env.client.post("/api/summary/generate",
                               json=qa_gen_payload()).status_code == 502
        assert env.db.list_group_summaries()[0] == 0

    def test_missing_usage_skips_ai_usage_billing(self, qa_api):
        env = qa_api
        env.chat.last_usage = None
        created = env.client.post("/api/summary/generate",
                                  json=qa_gen_payload()).json()
        assert created["prompt_tokens"] is None
        assert created["completion_tokens"] is None
        assert env.db.usage_by_date(
            datetime.now().strftime("%Y-%m-%d"))["calls"] == 0


# ===========================================================================
# F. 全局无副作用 / 7 条定稿决策
# ===========================================================================
class TestQaGlobalContract:
    def test_no_send_chain_and_no_auto_cleanup_registration(self):
        summary_src = (APP_ROOT / "app/summary.py").read_text(encoding="utf-8")
        router_src = (APP_ROOT / "app/api/router_summary.py").read_text(
            encoding="utf-8")
        main_src = (APP_ROOT / "app/main.py").read_text(encoding="utf-8")
        fe_src = (APP_ROOT / "frontend/src/pages/GroupSummary.tsx").read_text(
            encoding="utf-8")
        for src in (summary_src, router_src):
            assert "Sender" not in src and "SendTask" not in src
            assert "threading" not in src and "Thread(" not in src
            assert "purge" not in src.lower()
        assert "SummaryScheduler" not in main_src, "P0 不得在 main 注册调度器"
        assert not re.search(r"Thread\([^)]*(purge|cleanup)", main_src, re.I)
        assert "发送到群" not in fe_src and "sendToGroup" not in fe_src

        paths = [getattr(route, "path", "")
                 for router in ALL_ROUTERS for route in router.routes]
        assert not any(p.startswith("/api/summary/schedules") for p in paths)
        assert not any(p.startswith("/api/summary") and "send" in p.lower()
                       for p in paths)
        assert not any("cleanup" in p.lower() or "auto_purge" in p.lower()
                       for p in paths)

    def test_scheduler_p0_marks_only_no_thread_no_generation(self, qa_api,
                                                             monkeypatch):
        env = qa_api
        env.db.set_setting("summary.schedule_enabled", "1")
        now_hm = datetime.now().strftime("%H:%M")
        env.db.set_setting("summary.schedule_configs", json.dumps([
            {"session": QA_SESSION, "display_name": "QA群",
             "enabled": True, "time": now_hm}], ensure_ascii=False))
        scheduler = SummaryScheduler(env.svc, SimpleNamespace())

        created: list[int] = []

        def forbidden_thread(*args, **kwargs):
            created.append(1)
            raise AssertionError("P0 不应创建任何后台线程")

        with monkeypatch.context() as patch:
            patch.setattr(threading, "Thread", forbidden_thread)
            scheduler.start()
            hits = scheduler.tick()
        assert scheduler.started is True and created == []
        assert len(hits) == 1, "P1 命中判断就绪，但 P0 只返回配置"
        assert env.chat.calls == []
        assert env.db.list_group_summaries()[0] == 0
        scheduler.stop()
        assert scheduler.started is False

    def test_schedule_defaults_retention_manual_only_and_three_styles(self,
                                                                      qa_api):
        from app.config import DEFAULT_SETTINGS
        from app.schemas import STYLE_VALUES
        from app.summary import build_system_prompt

        env = qa_api
        settings = env.client.get("/api/summary/settings").json()
        assert settings["summary.schedule_enabled"] == "0", "默认关闭不打扰"
        assert settings["summary.schedule_configs"] == "[]"
        assert env.client.get("/api/system/retention").json()[
            "summaries_days"] == 180
        assert "group_summaries" in Database._PURGE_TABLES
        assert "ai_usage" not in Database._PURGE_TABLES
        assert DEFAULT_SETTINGS["retention.summaries_days"] == "180"
        assert STYLE_VALUES == ("detailed", "concise", "bullets")
        for style in STYLE_VALUES:
            prompt = build_system_prompt(style, "重点看价格")
            assert '"reminders"' in prompt and "重点看价格" in prompt

    def test_purge_request_regex_and_html_light_only(self, qa_api):
        assert PurgeRequest(table="group_summaries", keep_days=180).table == \
            "group_summaries"
        with pytest.raises(ValidationError):
            PurgeRequest(table="ai_usage", keep_days=180)
        env = qa_api
        row = env.db.insert_group_summary(
            session_username="g@chatroom", session_name="QA群",
            time_start=QA_START_DAY, time_end=QA_END_DAY,
            msg_count=1, member_count=1, content='{"closing":"ok"}')
        rendered = env.svc.render_html(row)
        assert "background:#fff" in rendered and "@media print" in rendered
        assert "dark" not in rendered.lower()


# ===========================================================================
# G. 历史保留 / 手动 purge 复用现有端点
# ===========================================================================
class TestQaRetention:
    def test_manual_purge_dry_run_and_real(self, qa_api):
        env = qa_api
        created = env.client.post("/api/summary/generate",
                                  json=qa_gen_payload()).json()
        sid = created["id"]
        with env.db._lock:
            env.db._conn.execute(
                "UPDATE group_summaries SET created_at='2020-01-01 00:00:00' "
                "WHERE id=?", (sid,))
            env.db._conn.commit()

        preview = env.client.post("/api/system/purge", json={
            "table": "group_summaries", "keep_days": 180, "dry_run": True})
        assert preview.status_code == 200 and preview.json()["deleted"] == 1
        assert env.client.get(f"/api/summary/history/{sid}").status_code == 200

        real = env.client.post("/api/system/purge", json={
            "table": "group_summaries", "keep_days": 180, "dry_run": False})
        assert real.status_code == 200 and real.json()["deleted"] == 1
        assert env.client.get(f"/api/summary/history/{sid}").status_code == 404
        assert env.client.get("/api/system/purge/last").json()[
            "table"] == "group_summaries"
        # ai_usage 是账单数据源，任何手动清理都不得删它
        assert env.db.usage_by_date(
            datetime.now().strftime("%Y-%m-%d"))["calls"] >= 1


# ===========================================================================
# H. 前端构建产物一致性（工程师已 npm build，此处只验证产物）
# ===========================================================================
def test_frontend_dist_matches_summary_contract():
    dist = APP_ROOT / "frontend/dist"
    assert (dist / "index.html").is_file()
    bundles = sorted((dist / "assets").glob("*.js"))
    assert bundles, "缺少前端构建产物"
    text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in bundles)
    assert "群消息总结" in text and "/summary" in text
    assert "请先在设置页配置模型单价，否则金额预算不生效" in text
    assert "已截断：仅统计最近" in text
    assert "summary.schedule_configs" in text
    assert "发送到群" not in text

    app_src = (APP_ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
    i_summary = app_src.find('label="群消息总结"')
    i_logs = app_src.find('label="分析日志"')
    assert 0 <= i_summary < i_logs, "入口须在分析日志之前"

    newest_src = max(
        path.stat().st_mtime for path in (
            APP_ROOT / "frontend/src/pages/GroupSummary.tsx",
            APP_ROOT / "frontend/src/pages/SettingsPage.tsx",
            APP_ROOT / "frontend/src/api/types.ts",
            APP_ROOT / "frontend/src/App.tsx",
        ))
    assert min(path.stat().st_mtime for path in bundles) >= newest_src, \
        "构建产物早于相关源文件，需重新 npm run build"
