# -*- coding: utf-8 -*-
"""批次 6 需求回归测试（A 组：判定与筛选 + 后端筛选/提示词）。

覆盖：
- 需求 6 自己发送的消息未被过滤：群聊里自己发的消息在微信 4.x 本地库中
  ``real_sender_id = 2``、``sender_username`` 为空、正文**没有**发送者前缀。
  修复前会被判成「未知用户」并当成别人发的商机；修复后：
  ① 开关开启时落 skipped 日志且不进聚合器；
  ② 开关关闭时正常进聚合器（显示「我」）；
  ③ 历史消息 ``_resolve_row_sender`` 群消息 sender_id=2 → ("我", 原文)；
  ④ 普通群成员（sender_id=54）不受影响，仍按正文前缀解析发送人。
- 需求 2 分析日志「来源会话」支持会话名筛选：``distinct_log_sessions``
  去重排序 + ``list_analysis_logs(session_in=...)`` + 路由层「显示名 →
  username」解析（贴群名能搜到、贴 username 片段仍能搜到、无匹配为 0、
  单条显示名解析异常不 500）。
- 需求 3 买卖方向判定优化：Prompt 注入独立「买卖方向判定」小节（含
  联系方式≠卖货 的约束与正反例），群聊/私聊均能正常构造；
  ``parse_ai_json`` 对非法 direction 仍归 ""（回归）。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_batch6.py -v --basetemp=D:/tmp/kefu-pytest-b6a
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import analyzer
from app.aggregator import Aggregator
from app.api import ALL_ROUTERS, deps
from app.config import KEY_ANALYSIS_FILTER_SELF
from app.db import Database
from app.notifier import Notifier
from app.wechat_service import WeChatService
from tests.test_api import FakePipeline, FakeSender, FakeWechatService


# ---------------------------------------------------------------------------
# 公共替身（与 test_batch5 同构，避免跨文件耦合）
# ---------------------------------------------------------------------------
class FakeWx:
    """WeChatDB 替身：群成员表 / 联系人昵称 / 会话行 / 登录账号。"""

    def __init__(self, *, members: list[dict] | None = None,
                 session_row: dict | None = None,
                 self_wxid: str = "wxid_self") -> None:
        self.members = members or []
        self.session_row = session_row or {}
        self.self_wxid = self_wxid

    def get_group_members(self, chatroom: str) -> list[dict]:
        return list(self.members)

    def get_nickname(self, username: str) -> str:
        for m in self.members:
            if m["username"] == username:
                return m.get("remark") or m.get("nick_name") or username
        return ""

    def get_session_row(self, username: str) -> dict | None:
        return dict(self.session_row) if self.session_row else None

    def get_self_info(self) -> dict:
        return {"username": self.self_wxid, "nick_name": "我", "remark": ""}


GROUP = "111@chatroom"


def make_svc(wx: FakeWx | None = None,
             units: list | None = None) -> tuple[WeChatService, Database]:
    """构造被测服务；(可选) 收集 flush 出来的分析单元便于断言。"""
    db = Database(":memory:")
    sink = (units.append if units is not None else (lambda u: None))
    svc = WeChatService(db, Aggregator(sink))
    if wx is not None:
        svc._wxdb = wx
    return svc, db


def make_group_msg(content: str, *, sender_id: int,
                   sender_username: str = "") -> dict:
    """群消息（微信 4.x 原始形态：自己发的没有发送者前缀）。"""
    return {
        "local_id": 1, "type": "文本", "sender_id": sender_id,
        "sender_username": sender_username,
        "create_time": time.time(), "content": content,
        "sort_seq": 1, "username": GROUP,
    }


def skipped_logs(db: Database) -> list[dict]:
    _total, items = db.list_analysis_logs(status="skipped")
    return items


# ---------------------------------------------------------------------------
# 需求 6：群聊里自己发送的消息（sender_id=2，无前缀）
# ---------------------------------------------------------------------------
class TestGroupSelfMessageSenderId2:
    def test_filtered_when_enabled(self):
        """① 开关开启（默认）：落 skipped 日志，且不进聚合器。"""
        svc, db = make_svc(FakeWx())
        svc._on_message(make_group_msg("我要一台发动机", sender_id=2))
        logs = skipped_logs(db)
        assert len(logs) == 1
        assert "自己发送的消息已过滤" in logs[0]["error"]
        assert logs[0]["input_text"] == "我要一台发动机"
        assert svc._aggregator.pending_count() == 0

    def test_kept_when_filter_disabled(self):
        """② 开关关闭：正常进聚合器，发送人显示「我」（不再「未知用户」）。"""
        units: list = []
        svc, db = make_svc(FakeWx(), units)
        db.set_setting(KEY_ANALYSIS_FILTER_SELF, "0")
        svc._on_message(make_group_msg("我要一台发动机", sender_id=2))
        assert skipped_logs(db) == []
        assert svc._aggregator.pending_count() == 1
        svc._aggregator.stop(flush_all=True)
        assert len(units) == 1
        assert units[0].sender_name == "我"
        assert units[0].lines[0][1] == "我要一台发动机"

    def test_resolve_row_sender_group_self(self):
        """③ 历史消息：群聊 sender_id=2 → ("我", 原文)，不剥前缀。"""
        svc, _db = make_svc(FakeWx())
        text = "LSGMJ5P578V016154"
        assert svc._resolve_row_sender(GROUP, text, "", 2) == ("我", text)

    def test_other_member_still_resolved_by_prefix(self):
        """④ 普通群成员（sender_id=54）不受影响：仍按正文前缀解析。"""
        members = [{"username": "wxid_other", "nick_name": "李四", "remark": ""}]
        svc, _db = make_svc(FakeWx(members=members))
        text = "wxid_other:\n客户要一批轴承"
        assert svc._resolve_row_sender(GROUP, text, "", 54) == (
            "李四", "客户要一批轴承")

    def test_resolve_sender_group_self_display_name(self):
        """实时监听：sender_id=2 → (自己 wxid, "我")，不走前缀解析。"""
        svc, _db = make_svc(FakeWx(self_wxid="wxid_self"))
        msg = make_group_msg("我要一台发动机", sender_id=2)
        assert svc._resolve_sender(GROUP, "group", msg) == ("wxid_self", "我")

    def test_resolve_sender_other_member(self):
        """实时监听：sender_id=54 仍走前缀 + 群成员映射。"""
        members = [{"username": "wxid_other", "nick_name": "李四", "remark": ""}]
        svc, _db = make_svc(FakeWx(members=members))
        msg = make_group_msg("wxid_other:\n客户要一批轴承", sender_id=54)
        assert svc._resolve_sender(GROUP, "group", msg) == ("wxid_other", "李四")

    def test_is_self_message_group_sender_id2(self):
        """群聊 sender_id=2 不依赖 self wxid 也能判定为自己。"""
        svc, _db = make_svc(FakeWx())
        svc._self_wxid = ""  # 模拟读不到登录账号
        msg = make_group_msg("我要一台发动机", sender_id=2)
        assert svc._is_self_message("group", msg, "") is True

    def test_is_self_message_group_other_member_false(self):
        svc, _db = make_svc(FakeWx(self_wxid="wxid_self"))
        msg = make_group_msg("wxid_other:\n客户消息", sender_id=54)
        assert svc._is_self_message("group", msg, "wxid_other") is False

    def test_filter_self_default_unchanged(self):
        """不要改开关语义：默认仍为 "1"（开启）。"""
        _svc, db = make_svc(None)
        assert db.get_setting(KEY_ANALYSIS_FILTER_SELF, "") == "1"


# ---------------------------------------------------------------------------
# 需求 2：分析日志「来源会话」按会话名筛选
# ---------------------------------------------------------------------------
class TestDistinctLogSessions:
    def _db(self) -> Database:
        db = Database(":memory:")
        db.insert_analysis_log(session_username="18248046934@chatroom",
                               msg_count=1, input_text="我要一台发动机",
                               status="ok")
        db.insert_analysis_log(session_username="wxid_zhang", msg_count=1,
                               input_text="求购轴承", status="ok")
        db.insert_analysis_log(session_username="18248046934@chatroom",
                               msg_count=1, input_text="LSGMJ5P578V016154",
                               status="ok")
        db.insert_analysis_log(session_username="", msg_count=1,
                               input_text="无会话日志", status="error")
        return db

    def test_dedupe_and_order(self):
        db = self._db()
        names = db.distinct_log_sessions()
        # 去重（同群只出现一次）+ 最近出现倒序 + 排除空串
        assert names == ["18248046934@chatroom", "wxid_zhang"]

    def test_limit(self):
        db = self._db()
        assert db.distinct_log_sessions(limit=1) == ["18248046934@chatroom"]

    def test_session_in_filter(self):
        db = self._db()
        total, rows = db.list_analysis_logs(
            session="家人群", session_in=["18248046934@chatroom"])
        assert total == 2
        assert all(r["session_username"] == "18248046934@chatroom"
                   for r in rows)

    def test_session_in_empty_keeps_like_behavior(self):
        db = self._db()
        total, rows = db.list_analysis_logs(session="zhang")
        assert total == 1
        assert rows[0]["session_username"] == "wxid_zhang"


class DisplayFakeWechatService(FakeWechatService):
    """在 FakeWechatService 上补一份「username → 显示名」映射。"""

    def __init__(self, names: dict[str, str] | None = None) -> None:
        super().__init__()
        self.names = names or {}
        self.display_error: set[str] = set()

    def get_display_name(self, username: str) -> str:
        if username in self.display_error:
            raise RuntimeError("显示名解析失败")
        return self.names.get(username, username)

    def build_log_displays(self, session_username: str,
                           input_text: str) -> tuple[str, str]:
        return self.get_display_name(session_username), input_text


@pytest.fixture()
def env(tmp_path):
    """真实路由 + 替身依赖 + 临时库（与 test_api.env 同构）。"""
    db = Database(str(tmp_path / "batch6.sqlite3"))
    svc = DisplayFakeWechatService(names={"18248046934@chatroom": "家人群"})
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


def _insert_logs(db: Database) -> None:
    db.insert_analysis_log(session_username="18248046934@chatroom",
                           msg_count=1, input_text="我要一台发动机", status="ok")
    db.insert_analysis_log(session_username="18248046934@chatroom",
                           msg_count=1, input_text="LSGMJ5P578V016154",
                           status="ok")
    db.insert_analysis_log(session_username="wxid_zhang", msg_count=1,
                           input_text="求购轴承", status="ok")


class TestAnalysisLogSessionNameFilter:
    def test_filter_by_group_display_name(self, env):
        """输入群名 → 命中该群的两条日志（修复前只能用 username 搜）。"""
        _insert_logs(env.db)
        body = env.client.get(
            "/api/analysis-logs", params={"session": "家人群"}).json()
        assert body["total"] == 2
        assert all(i["session_username"] == "18248046934@chatroom"
                   for i in body["items"])
        assert all(i["session_display"] == "家人群" for i in body["items"])

    def test_filter_by_username_fragment_still_works(self, env):
        """输入 username 片段 → 仍命中（原 LIKE 路径保留）。"""
        _insert_logs(env.db)
        body = env.client.get(
            "/api/analysis-logs", params={"session": "zhang"}).json()
        assert body["total"] == 1
        assert body["items"][0]["session_username"] == "wxid_zhang"

    def test_no_match_returns_zero(self, env):
        _insert_logs(env.db)
        body = env.client.get(
            "/api/analysis-logs", params={"session": "不存在的群"}).json()
        assert body["total"] == 0

    def test_display_name_error_does_not_500(self, env):
        """单条显示名解析失败只跳过该条，不能整页 500。"""
        _insert_logs(env.db)
        env.svc.display_error.add("wxid_zhang")
        resp = env.client.get(
            "/api/analysis-logs", params={"session": "家人群"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

    def test_empty_session_returns_all(self, env):
        _insert_logs(env.db)
        body = env.client.get("/api/analysis-logs").json()
        assert body["total"] == 3

    def test_session_with_status_combined(self, env):
        """会话名 + 状态组合筛选仍然生效。"""
        _insert_logs(env.db)
        env.db.insert_analysis_log(session_username="18248046934@chatroom",
                                   msg_count=1, input_text="闲聊",
                                   status="skipped", error="预筛未命中")
        body = env.client.get("/api/analysis-logs",
                              params={"session": "家人群",
                                      "status": "ok"}).json()
        assert body["total"] == 2


# ---------------------------------------------------------------------------
# 需求 3：买卖方向判定优化
# ---------------------------------------------------------------------------
class TestDirectionGuidance:
    TYPES = [{"name": "配件采购", "description": "询价/求购汽车配件"}]

    def _prompt(self, **kwargs) -> str:
        return analyzer.build_system_prompt(self.TYPES, "balanced", **kwargs)

    def test_prompt_contains_direction_section(self):
        prompt = self._prompt()
        assert "买卖方向判定" in prompt
        assert "联系方式只是联系渠道" in prompt

    def test_prompt_contains_buy_and_sell_signals(self):
        prompt = self._prompt()
        for token in ("有的", "谁有", "打我电话", "求购", "现货", "低价",
                      "可以发货", "库存"):
            assert token in prompt

    def test_prompt_contains_cases(self):
        """3 条正例 + 2 条反例，且含用户实测误判原句。"""
        prompt = self._prompt()
        assert "那个有车壳有的打电话15887775311" in prompt
        assert "正例" in prompt and "反例" in prompt

    def test_group_and_private_prompt_both_build(self):
        """group_chat 两种取值都不报错（模板 format 参数完整）。"""
        assert "买卖方向判定" in self._prompt(group_chat=True)
        assert "买卖方向判定" in self._prompt(group_chat=False)

    def test_template_format_args_intact(self):
        prompt = self._prompt(examples=["求购 42431-0K090 一批"])
        assert "商机示例（用户确认）" in prompt

    def test_invalid_direction_normalized_to_empty(self):
        """回归：direction 非 buy/sell 一律归 ""（不改 JSON 输出字段）。"""
        result = analyzer.parse_ai_json(
            '{"hit": false, "type": null, "points": [], "confidence": 10,'
            ' "quote": "", "direction": "maybe"}')
        assert result["direction"] == ""
        ok = analyzer.parse_ai_json(
            '{"hit": true, "type": "配件采购", "points": ["车壳"],'
            ' "confidence": 80, "quote": "有的打电话", "direction": "BUY"}')
        assert ok["direction"] == "buy"
