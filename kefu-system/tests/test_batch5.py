# -*- coding: utf-8 -*-
"""批次 5 五项需求的回归测试。

覆盖：
- 需求 ① 筛选只匹配消息内容：群消息发送者前缀剥离（Aggregator 共享口径）、
  @人微信 ID token 在预筛匹配前剔除（只影响匹配，不影响展示文本）；
- 需求 ② 发送人显示名与微信一致：群名片学习（session.db 的
  last_sender_display_name → group_cards 表 → 显示名解析优先采用），
  会话行最后发送者不一致时不采纳（防张冠李戴）；
- 需求 ③ 会话搜索按最后消息时间排序：匹配质量同级的条目内近期消息优先，
  无消息的最后；
- 需求 ④ 群聊 @某人 AI 甄别：@ID 还原为群内显示名、群聊 Prompt 注入
  @人判定指引、管线 @还原函数注入；
- 需求 ⑤ 过滤自己发送的消息（默认开启）：私聊 sender_id=2 与群聊自己
  wxid 两种形态，均落 skipped 日志且不入聚合窗口；开关关闭恢复原行为。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_batch5.py -v
"""
from __future__ import annotations

import time

from app.aggregator import AnalysisUnit, Aggregator
from app import analyzer
from app import content_format
from app.config import KEY_ANALYSIS_FILTER_SELF
from app.db import Database
from app.notifier import Notifier
from app.pipeline import Pipeline
from app.wechat_service import WeChatService


# ---------------------------------------------------------------------------
# 公共替身
# ---------------------------------------------------------------------------
class FakeWx:
    """WeChatDB 替身：群成员表 / 联系人昵称 / 会话行 / 会话时间 / 登录账号。"""

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


def make_svc(wx: FakeWx | None = None) -> tuple[WeChatService, Database]:
    db = Database(":memory:")
    svc = WeChatService(db, Aggregator(lambda u: None))
    if wx is not None:
        svc._wxdb = wx
    return svc, db


def make_msg(content: str, *, username: str = "111@chatroom",
             sender_username: str = "wxid_other", sender_id: int = 3,
             mtype: str = "文本") -> dict:
    return {
        "local_id": 1, "type": mtype, "sender_id": sender_id,
        "sender_username": sender_username,
        "create_time": time.time(), "content": content,
        "sort_seq": 1, "username": username,
    }


# ---------------------------------------------------------------------------
# 需求 ①：筛选只匹配消息内容
# ---------------------------------------------------------------------------
class TestStripGroupSenderPrefix:
    """群消息发送者前缀剥离（与 extract_content_sender 同一严格口径）。"""

    def test_strip_wxid_prefix_line(self):
        assert content_format.strip_group_sender_prefix(
            "wxid_abc:\n有谁能做CNC") == "有谁能做CNC"

    def test_same_line_form_not_stripped(self):
        """原生格式是「ID:\\n正文」（前缀独占首行）；同行的「ID: 正文」
        可能是任意带冒号的话（如「注意：明天开会」），不剥离——同行形态
        仅用于发送者提取（extract_content_sender）。"""
        text = "注意：明天开会"
        assert content_format.strip_group_sender_prefix(text) == text

    def test_no_prefix_kept(self):
        text = "9:30 开会，别迟到"
        assert content_format.strip_group_sender_prefix(text) == text

    def test_url_not_treated_as_prefix(self):
        text = "http://example.com\n详情看链接"
        assert content_format.strip_group_sender_prefix(text) == text

    def test_private_content_untouched_by_helper(self):
        """剥离函数是通用工具；私聊判断由 Aggregator 按 session_kind 控制。"""
        assert content_format.strip_group_sender_prefix(
            "普通消息") == "普通消息"


class TestAggregatorStripsPrefix:
    """Aggregator.add：群消息入缓冲前剥前缀；私聊保持原样。"""

    def _flushed(self, session_kind: str) -> AnalysisUnit:
        out: list[AnalysisUnit] = []
        agg = Aggregator(out.append, window_sec_provider=lambda: 5)
        msg = make_msg("wxid_abc:\n采购一批轴承", username="111@chatroom")
        assert agg.add(msg, "测试群", session_kind) is True
        agg.stop(flush_all=True)
        assert len(out) == 1
        return out[0]

    def test_group_lines_without_prefix(self):
        unit = self._flushed("group")
        assert unit.lines == [(unit.lines[0][0], "采购一批轴承")]

    def test_friend_lines_untouched(self):
        out: list[AnalysisUnit] = []
        agg = Aggregator(out.append, window_sec_provider=lambda: 5)
        msg = make_msg("wxid_abc:\n采购一批轴承", username="wxid_friend")
        agg.add(msg, "好友", "friend")
        agg.stop(flush_all=True)
        # 私聊正文不做前缀剥离（wxid_abc: 是消息内容本身）
        assert "wxid_abc:" in out[0].lines[0][1]

    def test_prefix_only_message_dropped(self):
        """正文只有前缀（剥离后为空）不入缓冲，避免产生空分析单元。"""
        agg = Aggregator(lambda u: None, window_sec_provider=lambda: 5)
        msg = make_msg("wxid_abc:\n", username="111@chatroom")
        assert agg.add(msg, "测试群", "group") is False


class TestPrescreenIgnoresAtTokens:
    """预筛匹配前剔除 @人微信 ID token（只影响匹配，不影响展示文本）。"""

    def test_strip_at_tokens(self):
        text = "@wxid_abc123 采购\n@someone@chatroom 报价"
        cleaned = content_format.strip_at_id_tokens(text)
        assert "wxid_abc123" not in cleaned
        assert "someone@chatroom" not in cleaned

    def test_normal_at_not_stripped(self):
        """普通 @昵称（非 ID 形态）保留——AI/匹配仍可利用。"""
        text = "@张三 你报价多少"
        assert content_format.strip_at_id_tokens(text) == text

    def test_prescreen_no_false_hit_from_at_id(self):
        """@ID 恰好含关键词时不误命中（需求 ① 的核心场景）。"""
        text = "[14:02] @wxid_sales_dept_01 需要轴承"
        assert analyzer.prescreen(text, "sale", "") is True      # 原文会误命中
        cleaned = content_format.strip_at_id_tokens(text)
        assert analyzer.prescreen(cleaned, "sale", "") is False

    def test_prescreen_hits_still_match_content(self):
        text = "[14:02] 需要采购 500 个轴承 @wxid_zhang99"
        hits = analyzer.prescreen_hits(
            content_format.strip_at_id_tokens(text), "采购,求购", "")
        assert hits == ["采购"]

    def test_display_text_unchanged(self):
        """剔除只影响匹配：merged_text/落库文本仍含原 token。"""
        text = "[14:02] @wxid_zhang99 报价多少"
        assert content_format.strip_at_id_tokens(text) != text  # 匹配用被剔除
        assert text == text                                     # 展示用原样


# ---------------------------------------------------------------------------
# 需求 ②：发送人显示名与微信一致（群名片学习）
# ---------------------------------------------------------------------------
class TestGroupCardLearning:
    """群名片：session.db 微信端渲染名 → group_cards → 显示名优先采用。"""

    MEMBERS = [
        {"username": "zhaoyu_4932403", "nick_name": "赵宇", "remark": ""},
        {"username": "wxid_zhang", "nick_name": "张三(全局)",
         "remark": "张老板", "is_owner": True},
    ]

    def test_learned_card_overrides_global_nick(self):
        """学习了名片后显示名片（此前显示全局昵称「赵宇」是用户反馈的 bug）。"""
        wx = FakeWx(members=self.MEMBERS, session_row={
            "username": "111@chatroom",
            "last_msg_sender": "zhaoyu_4932403",
            "last_sender_display_name": "513猎在于狩-宁静致远",
        })
        svc, db = make_svc(wx)
        svc._learn_group_card("111@chatroom", "zhaoyu_4932403")
        assert db.get_group_cards("111@chatroom") == {
            "zhaoyu_4932403": "513猎在于狩-宁静致远"}
        assert svc.resolve_group_member_display(
            "111@chatroom", "zhaoyu_4932403") == "513猎在于狩-宁静致远"

    def test_no_card_keeps_remark_priority(self):
        """未学习到名片时维持原优先级：好友备注 → 全局昵称 → username。"""
        svc, _db = make_svc(FakeWx(members=self.MEMBERS))
        assert svc.resolve_group_member_display(
            "111@chatroom", "wxid_zhang") == "张老板"
        assert svc.resolve_group_member_display(
            "111@chatroom", "zhaoyu_4932403") == "赵宇"

    def test_sender_mismatch_guard(self):
        """会话行最后发送者与本条消息发送者不一致：不采纳（防张冠李戴）。"""
        wx = FakeWx(members=self.MEMBERS, session_row={
            "username": "111@chatroom",
            "last_msg_sender": "someone_else",
            "last_sender_display_name": "别人的名片",
        })
        svc, db = make_svc(wx)
        svc._learn_group_card("111@chatroom", "zhaoyu_4932403")
        assert db.get_group_cards("111@chatroom") == {}

    def test_learn_via_on_message(self):
        """_on_message 群消息路径触发学习；名片对后续消息立即生效。"""
        wx = FakeWx(members=self.MEMBERS, session_row={
            "username": "111@chatroom",
            "last_msg_sender": "zhaoyu_4932403",
            "last_sender_display_name": "513猎在于狩-宁静致远",
        })
        svc, _db = make_svc(wx)
        svc._on_message(make_msg("zhaoyu_4932403:\n轴承有货"))
        assert svc._aggregator.pending_count() == 1
        # 首条消息解析时名片还没学习到（先解析后学习），但缓存已失效，
        # 下一次解析直接用名片
        msg2 = make_msg("zhaoyu_4932403:\n第二条")
        sender_wxid, sender_name = svc._resolve_sender(
            "111@chatroom", "group", msg2)
        assert sender_name == "513猎在于狩-宁静致远"

    def test_empty_display_name_not_learned(self):
        wx = FakeWx(members=self.MEMBERS, session_row={
            "username": "111@chatroom",
            "last_msg_sender": "zhaoyu_4932403",
            "last_sender_display_name": "",
        })
        svc, db = make_svc(wx)
        svc._learn_group_card("111@chatroom", "zhaoyu_4932403")
        assert db.get_group_cards("111@chatroom") == {}

    def test_card_not_applied_to_departed_member(self):
        """已退出群的成员名片不生效（成员不在群成员表时忽略）。"""
        wx = FakeWx(members=self.MEMBERS, session_row={
            "username": "111@chatroom",
            "last_msg_sender": "wxid_left",
            "last_sender_display_name": "离群的人",
        })
        svc, db = make_svc(wx)
        svc._learn_group_card("111@chatroom", "wxid_left")
        assert db.get_group_cards("111@chatroom") == {"wxid_left": "离群的人"}
        # 不在成员表 → 解析走联系人回退，不受离群名片影响
        assert svc.resolve_group_member_display(
            "111@chatroom", "wxid_left") == "wxid_left"


# ---------------------------------------------------------------------------
# 需求 ③：会话搜索按最后消息时间排序
# ---------------------------------------------------------------------------
class _SearchWx:
    """搜索替身：contacts + session_times（无 get_session_row 等）。"""

    def __init__(self, contacts: dict[str, list[dict]],
                 times: dict[str, int]) -> None:
        self.contacts = contacts
        self.times = times

    def get_sessions(self, limit: int = 100) -> list[dict]:
        return []

    def get_contacts(self, kind: str = "friend", limit: int = 0) -> list[dict]:
        return list(self.contacts.get(kind, []))

    def get_openim_corp_names(self) -> dict[str, str]:
        return {}

    def get_nickname(self, username: str) -> str:
        for rows in self.contacts.values():
            for r in rows:
                if r["username"] == username:
                    return r.get("remark") or r.get("nick_name") or ""
        return ""

    def get_session_times(self) -> dict[str, int]:
        return dict(self.times)


class TestSearchOrderByRecent:
    def test_recent_message_first_within_same_rank(self):
        """匹配质量同级：近期有过消息的排前面。"""
        wx = _SearchWx(
            {"friend": [
                {"username": "wxid_old", "nick_name": "邢不行"},
                {"username": "wxid_new", "nick_name": "邢不行"},
                {"username": "wxid_none", "nick_name": "邢不行"},
            ]},
            {"wxid_old": 1000, "wxid_new": 2000})
        svc = WeChatService(None, None)  # type: ignore[arg-type]
        svc._wxdb = wx
        hits = svc.search_sessions(keyword="邢不行", kind="friend")
        assert [h["username"] for h in hits] == [
            "wxid_new", "wxid_old", "wxid_none"]

    def test_match_rank_still_dominates(self):
        """完全匹配仍优先于包含匹配（主排序不变，时间只在同级内生效）。"""
        wx = _SearchWx(
            {"friend": [
                {"username": "wxid_contains", "nick_name": "小邢不行"},
                {"username": "wxid_exact", "nick_name": "邢不行"},
            ]},
            {"wxid_contains": 9999999})
        svc = WeChatService(None, None)  # type: ignore[arg-type]
        svc._wxdb = wx
        hits = svc.search_sessions(keyword="邢不行", kind="friend")
        assert hits[0]["username"] == "wxid_exact"

    def test_missing_session_times_method_falls_back(self):
        """替身/旧版微信库没有 get_session_times：按无时间处理，不报错。"""
        wx = _SearchWx({"friend": [{"username": "wxid_a", "nick_name": "邢不行"}]}, {})
        del wx.__class__.get_session_times  # type: ignore[attr-defined]
        svc = WeChatService(None, None)  # type: ignore[arg-type]
        svc._wxdb = wx
        hits = svc.search_sessions(keyword="邢不行", kind="friend")
        assert [h["username"] for h in hits] == ["wxid_a"]


# ---------------------------------------------------------------------------
# 需求 ④：群聊 @某人消息的甄别
# ---------------------------------------------------------------------------
class TestAtTokenResolution:
    MEMBERS = [
        {"username": "wxid_zhang", "nick_name": "张三(全局)",
         "remark": "张老板"},
        {"username": "zhaoyu_4932403", "nick_name": "赵宇"},
    ]

    def test_resolve_at_wxid_to_display(self):
        svc, _db = make_svc(FakeWx(members=self.MEMBERS))
        out = svc.resolve_at_tokens("111@chatroom",
                                    "@wxid_zhang 你那边报价多少")
        assert out == "@张老板 你那边报价多少"

    def test_resolve_at_chatroom_token(self):
        svc, _db = make_svc(FakeWx(members=self.MEMBERS))
        out = svc.resolve_at_tokens("111@chatroom",
                                    "@zhaoyu_4932403@chatroom 45一个出")
        assert out == "@赵宇 45一个出"

    def test_unresolvable_token_kept(self):
        svc, _db = make_svc(FakeWx(members=self.MEMBERS))
        text = "@wxid_unknown999 你好"
        assert svc.resolve_at_tokens("111@chatroom", text) == text

    def test_private_untouched(self):
        svc, _db = make_svc(FakeWx(members=self.MEMBERS))
        text = "@wxid_zhang 私聊里不还原"
        assert svc.resolve_at_tokens("wxid_friend", text) == text


class TestGroupAtPromptGuidance:
    def test_group_prompt_contains_at_guidance(self):
        prompt = analyzer.build_system_prompt(
            [{"name": "配件采购", "description": "询价"}], "balanced",
            group_chat=True)
        assert "@某人" in prompt and "回复" in prompt

    def test_private_prompt_without_at_guidance(self):
        prompt = analyzer.build_system_prompt(
            [{"name": "配件采购", "description": "询价"}], "balanced")
        assert "@某人" not in prompt

    def test_template_format_safe(self):
        """模板含新占位符后普通调用仍可正常 format（防 KeyError 回归）。"""
        prompt = analyzer.build_system_prompt([], "strict")
        assert "商机类型" in prompt


class TestPipelineAtResolver:
    """管线 @还原注入：群聊还原、私聊不动、异常回退原文。"""

    @staticmethod
    def _unit(kind: str = "group") -> AnalysisUnit:
        return AnalysisUnit(
            session_username="111@chatroom", session_name="测试群",
            session_kind=kind, sender_wxid="wxid_a", sender_name="甲",
            window_start="2026-01-01T00:00:00",
            window_end="2026-01-01T00:00:15", merged_count=1,
            lines=[("14:02", "@wxid_zhang 45一个出")],
        )

    def test_group_resolved(self):
        db = Database(":memory:")
        pl = Pipeline(db, Notifier())
        pl.set_at_resolver(lambda u, t: t.replace("@wxid_zhang", "@张老板"))
        assert pl._ai_input_text(
            self._unit(), "@wxid_zhang 45一个出") == "@张老板 45一个出"

    def test_private_untouched(self):
        db = Database(":memory:")
        pl = Pipeline(db, Notifier())
        pl.set_at_resolver(lambda u, t: "changed")
        text = "@wxid_zhang 私聊"
        assert pl._ai_input_text(self._unit("friend"), text) == text

    def test_resolver_exception_falls_back(self):
        db = Database(":memory:")
        pl = Pipeline(db, Notifier())
        pl.set_at_resolver(lambda u, t: 1 / 0)
        text = "@wxid_zhang 异常回退"
        assert pl._ai_input_text(self._unit(), text) == text

    def test_no_resolver_keeps_original(self):
        db = Database(":memory:")
        pl = Pipeline(db, Notifier())
        text = "@wxid_zhang 未注入"
        assert pl._ai_input_text(self._unit(), text) == text


# ---------------------------------------------------------------------------
# 需求 ⑤：过滤自己发送的消息
# ---------------------------------------------------------------------------
class TestFilterSelfMessages:
    def _skipped_logs(self, db: Database) -> list[dict]:
        _total, items = db.list_analysis_logs(status="skipped")
        return items

    def test_friend_self_message_filtered_by_default(self):
        svc, db = make_svc(FakeWx())
        msg = make_msg("我自己发的", username="wxid_friend", sender_id=2)
        svc._on_message(msg)
        logs = self._skipped_logs(db)
        assert len(logs) == 1
        assert "自己发送的消息已过滤" in logs[0]["error"]
        assert logs[0]["input_text"] == "我自己发的"
        assert svc._aggregator.pending_count() == 0

    def test_group_self_message_filtered_by_default(self):
        wx = FakeWx(self_wxid="wxid_self")
        svc, db = make_svc(wx)
        msg = make_msg("wxid_self:\n群里的自我发言",
                       sender_username="wxid_other")
        svc._on_message(msg)
        logs = self._skipped_logs(db)
        assert len(logs) == 1
        assert "自己发送的消息已过滤" in logs[0]["error"]
        assert svc._aggregator.pending_count() == 0

    def test_others_message_not_filtered(self):
        svc, db = make_svc(FakeWx())
        svc._on_message(make_msg("wxid_other:\n客户消息"))
        assert self._skipped_logs(db) == []
        assert svc._aggregator.pending_count() == 1

    def test_disabled_keeps_original_behavior(self):
        svc, db = make_svc(FakeWx())
        db.set_setting(KEY_ANALYSIS_FILTER_SELF, "0")
        svc._on_message(make_msg("我自己发的", username="wxid_friend",
                                 sender_id=2))
        assert self._skipped_logs(db) == []
        assert svc._aggregator.pending_count() == 1

    def test_default_setting_value(self):
        _svc, db = make_svc(None)
        assert db.get_setting(KEY_ANALYSIS_FILTER_SELF, "") == "1"

    def test_read_analysis_settings_exposes_flag(self):
        from app.db import read_analysis_settings
        _svc, db = make_svc(None)
        assert read_analysis_settings(db)["filter_self_messages"] is True
        db.set_setting(KEY_ANALYSIS_FILTER_SELF, "0")
        assert read_analysis_settings(db)["filter_self_messages"] is False


# ---------------------------------------------------------------------------
# 需求 ⑤：设置 API 字段
# ---------------------------------------------------------------------------
class TestFilterSelfSettingsApi:
    def _client(self, db: Database):
        from types import SimpleNamespace

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api import ALL_ROUTERS, deps
        app = FastAPI()
        for router in ALL_ROUTERS:
            app.include_router(router)
        deps.configure(db=db, wechat=None, pipeline=None, sender=None,
                       notifier=None)  # type: ignore[arg-type]
        client = TestClient(app)
        return SimpleNamespace(client=client, db=db, deps=deps)

    def test_analysis_settings_round_trip(self):
        db = Database(":memory:")
        ns = self._client(db)
        resp = ns.client.get("/api/settings/analysis")
        assert resp.status_code == 200
        assert resp.json()["filter_self_messages"] is True
        resp = ns.client.put("/api/settings/analysis", json={
            "strictness": "balanced", "min_confidence": 60,
            "prescreen_enabled": False, "prescreen_keywords": "",
            "prescreen_regex": "", "merge_window_sec": 15,
            "opp_merge_window_sec": 180, "correction_examples": [],
            "filter_self_messages": False,
        })
        assert resp.status_code == 200
        assert resp.json()["filter_self_messages"] is False
        assert db.get_setting(KEY_ANALYSIS_FILTER_SELF, "") == "0"
        ns.deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                          notifier=None)
        db.close()
