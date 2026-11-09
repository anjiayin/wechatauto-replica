# -*- coding: utf-8 -*-
"""第二轮 13 项用户反馈的回归测试（P0/P1 后端部分）。

覆盖：
- 反馈 1：analysis_logs 索引存在（created_at / session_username）；
- 反馈 5/6：群发送人取名链（备注优先、群显示名前缀反查、含空格
  显示名前缀反查——「长信拆车件18632162702」案例）；
- 反馈 3：content_format 非文本消息友好化（链接/图片/小程序/失败回退）；
- 反馈 4：direction 买卖货（DAO 落库/筛选 + AI 结果规整）；
- 反馈 7：delete_older_than 历史清理（白名单/天数/外键置空）；
- 反馈 12：notifier.clear_all_unread；
- 反馈 13：token 用量记录 + usage_by_date + 费用换算。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_feedback_round2.py -v
"""
from __future__ import annotations

import time
from typing import List

from app.aggregator import Aggregator
from app import analyzer
from app.content_format import format_content
from app.db import Database
from app.notifier import Notifier
from app.wechat_service import WeChatService


# ---------------------------------------------------------------------------
# 替身与工具
# ---------------------------------------------------------------------------
class Round2FakeWxDb:
    """群成员表 + 联系人昵称替身（复刻「长信」案例）。"""

    def __init__(self) -> None:
        self.nickname_calls: List[str] = []

    def get_group_members(self, chatroom: str) -> list[dict]:
        if chatroom == "21264829018@chatroom":
            return [
                # 非好友：contact.nick_name 即其群显示名（实测）
                {"username": "wxid_6qln8fhyfif022",
                 "nick_name": "长信拆车件18632162702", "remark": "",
                 "is_owner": False},
                # 好友：我给他的备注 + 他自定义的群昵称
                {"username": "wxid_friend", "nick_name": "老王(群里叫法)",
                 "remark": "王老板", "is_owner": False},
            ]
        return []

    def get_nickname(self, username: str) -> str:
        self.nickname_calls.append(username)
        return {
            "21264829018@chatroom": "拆车件群",
            "wxid_friend": "王老板",
        }.get(username, "")


def make_svc() -> WeChatService:
    db = Database(":memory:")
    svc = WeChatService(db, Aggregator(lambda u: None))
    svc._wxdb = Round2FakeWxDb()
    return svc


def make_group_msg(content: str, sender_username: str = "wxid_wrong_map",
                   mtype: str = "文本") -> dict:
    return {
        "local_id": 1, "type": mtype, "sender_id": 3,
        "sender_username": sender_username,
        "create_time": time.time(), "content": content,
        "sort_seq": 1, "username": "21264829018@chatroom",
    }


# ---------------------------------------------------------------------------
# 反馈 1：索引
# ---------------------------------------------------------------------------
class TestIndexes:
    def test_analysis_logs_indexes_exist(self):
        db = Database(":memory:")
        names = {r["name"] for r in
                 db._conn.execute(
                     "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_logs_created" in names
        assert "idx_logs_session" in names

    def test_query_uses_index(self):
        db = Database(":memory:")
        plan = db._conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM analysis_logs "
            "ORDER BY created_at DESC LIMIT 20").fetchall()
        assert any("idx_logs_created" in (r["detail"] or "") for r in plan)


# ---------------------------------------------------------------------------
# 反馈 5/6：取名链
# ---------------------------------------------------------------------------
class TestNameChain:
    def test_friend_remark_overrides_group_nick(self):
        """好友在群内发言：显示我给他的备注，而非其群昵称（反馈 5）。"""
        svc = make_svc()
        msg = make_group_msg("wxid_friend:\n有谁能做CNC")
        svc._on_message(msg)
        assert msg["sender_username"] == "wxid_friend"
        assert msg["sender_display_name"] == "王老板"

    def test_group_display_name_for_non_friend(self):
        """非好友：contact 昵称即群显示名（反馈 6 实测数据）。"""
        svc = make_svc()
        msg = make_group_msg("wxid_6qln8fhyfif022:\n出售拆车件")
        svc._on_message(msg)
        assert msg["sender_username"] == "wxid_6qln8fhyfif022"
        assert msg["sender_display_name"] == "长信拆车件18632162702"

    def test_display_name_prefix_reverse_lookup(self):
        """正文前缀是群显示名（非 wxid）→ 反查真实成员（反馈 6）。"""
        svc = make_svc()
        msg = make_group_msg("长信拆车件18632162702:\n出售拆车件")
        svc._on_message(msg)
        assert msg["sender_username"] == "wxid_6qln8fhyfif022"
        assert msg["sender_display_name"] == "长信拆车件18632162702"

    def test_spaced_display_name_prefix_reverse_lookup(self):
        """含空格的显示名前缀（严格正则不命中）→ 宽松前缀反查。"""
        svc = make_svc()
        svc._wxdb.get_group_members = lambda c: [  # noqa: E731
            {"username": "wxid_x", "nick_name": "长信 无欲无求",
             "remark": "", "is_owner": False},
        ]
        svc._group_members_cache.clear()
        msg = make_group_msg("长信 无欲无求:\n正文")
        svc._on_message(msg)
        assert msg["sender_username"] == "wxid_x"
        assert msg["sender_display_name"] == "长信 无欲无求"

    def test_unknown_display_prefix_falls_back(self):
        """前缀既非成员 wxid 也非成员显示名 → 保留前缀原文。

        不回退 sender_username 映射：该映射已被证实不可靠（会张冠李戴），
        保留微信正文里的原始前缀比显示另一个人的名字更可信。
        """
        svc = make_svc()
        msg = make_group_msg("陌生人: 你好", sender_username="wxid_wrong_map")
        svc._on_message(msg)
        assert msg["sender_display_name"] == "陌生人"


# ---------------------------------------------------------------------------
# 反馈 3：非文本消息友好化
# ---------------------------------------------------------------------------
class TestContentFormat:
    def test_link(self):
        xml = ('<?xml version="1.0"?><msg><appmsg type="5">'
               "<title>拆车件价格表</title><des>本周更新</des>"
               "<url>https://example.com/price</url></appmsg></msg>")
        out = format_content("文件、链接、卡片", xml)
        assert out.startswith("[链接] 拆车件价格表")
        assert "https://example.com/price" in out
        assert "<" not in out

    def test_mini_program(self):
        xml = ('<msg><appmsg type="33"><title>某配件小程序</title>'
               "</appmsg></msg>")
        assert format_content("文件、链接、卡片", xml) == "[小程序] 某配件小程序"

    def test_image_and_emoji(self):
        assert format_content("图片", '[图片 md5="abc"]') == "[图片]"
        assert format_content("动画表情", "<msg><emoji/></msg>") == "[动画表情]"

    def test_parse_failure_falls_back(self):
        out = format_content("文件、链接、卡片", "<msg><appmsg><title>截断")
        assert out == "[链接]"

    def test_text_passthrough(self):
        assert format_content("文本", "普通正文") == "普通正文"


# ---------------------------------------------------------------------------
# 反馈 4：买/卖货方向
# ---------------------------------------------------------------------------
class TestDirection:
    def _insert(self, db: Database, direction: str) -> dict:
        return db.insert_opportunity(
            session_username="a@chatroom", session_name="群",
            sender_wxid="wx1", sender_name="s",
            window_start="2026-11-09T10:00:00",
            window_end="2026-11-09T10:01:00",
            merged_count=1, raw_text="x", type_id=None, type_name="t",
            summary="p", key_quote="q", confidence=80, direction=direction)

    def test_insert_and_filter(self):
        db = Database(":memory:")
        self._insert(db, "buy")
        self._insert(db, "sell")
        self._insert(db, "unknown")  # 非法值归空
        assert db.list_opportunities(direction="buy")[0] == 1
        assert db.list_opportunities(direction="sell")[0] == 1
        assert db.list_opportunities()[0] == 3

    def test_normalize_direction(self):
        assert analyzer._normalize_result({
            "hit": True, "type": "供货", "points": [], "confidence": 70,
            "quote": "q", "direction": "SELL"})["direction"] == "sell"
        assert analyzer._normalize_result({
            "hit": True, "type": "供货", "points": [], "confidence": 70,
            "quote": "q", "direction": "whatever"})["direction"] == ""

    def test_prompt_mentions_direction(self):
        prompt = analyzer.build_system_prompt(
            [{"name": "供货", "description": "出售信息"}], "balanced")
        assert "direction" in prompt


# ---------------------------------------------------------------------------
# 反馈 7：历史数据管理
# ---------------------------------------------------------------------------
class TestPurge:
    def test_whitelist(self):
        db = Database(":memory:")
        for bad in ("settings", "watermarks", "sqlite_master", ""):
            try:
                db.delete_older_than(bad, 30)
                raise AssertionError("应拒绝非白名单表")
            except ValueError:
                pass

    def test_days_validation(self):
        db = Database(":memory:")
        try:
            db.delete_older_than("analysis_logs", 0)
            raise AssertionError("天数 0 应被拒绝")
        except ValueError:
            pass

    def test_purge_old_rows_and_fk_nulling(self):
        db = Database(":memory:")
        opp = db.insert_opportunity(
            session_username="a@chatroom", session_name="g",
            sender_wxid="w", sender_name="s",
            window_start="2020-01-01T00:00:00",
            window_end="2020-01-01T00:01:00",
            merged_count=1, raw_text="x", type_id=None, type_name="t",
            summary="p", key_quote="q", confidence=80)
        # 手工把 created_at 改成 1 年前（绕过 default now）
        db._conn.execute(
            "UPDATE opportunities SET created_at='2020-01-01T00:00:00' "
            "WHERE id=?", (opp["id"],))
        log_id = db.insert_analysis_log(
            session_username="a@chatroom", msg_count=1, status="ok",
            opportunity_id=opp["id"])
        db._conn.execute(
            "UPDATE analysis_logs SET created_at='2020-01-01T00:00:00' "
            "WHERE id=?", (log_id,))
        db._conn.commit()
        deleted = db.delete_older_than("opportunities", 365)
        assert deleted == 1
        row = db._conn.execute(
            "SELECT opportunity_id FROM analysis_logs WHERE id=?",
            (log_id,)).fetchone()
        assert row["opportunity_id"] is None  # 外键已置空，日志保留
        assert db.get_opportunity(opp["id"]) is None

    def test_purge_logs_only(self):
        db = Database(":memory:")
        log_id = db.insert_analysis_log(
            session_username="a@chatroom", msg_count=1, status="skipped")
        db._conn.execute(
            "UPDATE analysis_logs SET created_at='2020-01-01T00:00:00' "
            "WHERE id=?", (log_id,))
        db._conn.commit()
        assert db.delete_older_than("analysis_logs", 90) == 1
        assert db.list_analysis_logs()[0] == 0


# ---------------------------------------------------------------------------
# 反馈 12：全部已读
# ---------------------------------------------------------------------------
class TestReadAll:
    def test_clear_all_unread(self):
        notifier = Notifier()
        notifier.mark_new_opportunity(1)
        notifier.mark_new_opportunity(2)
        assert notifier.unread_count == 2
        assert notifier.clear_all_unread() == 2
        assert notifier.unread_count == 0
        assert notifier.clear_all_unread() == 0


# ---------------------------------------------------------------------------
# 反馈 13：token 用量与账单
# ---------------------------------------------------------------------------
class TestBilling:
    def test_usage_recorded_and_queried(self):
        db = Database(":memory:")
        db.insert_analysis_log(session_username="a@chatroom", msg_count=1,
                               status="ok", model="m",
                               prompt_tokens=1500, completion_tokens=300)
        db.insert_analysis_log(session_username="a@chatroom", msg_count=1,
                               status="error")  # 失败调用无 usage，不计费
        usage = db.usage_by_date("2026-11-09")
        assert usage["calls"] == 1
        assert usage["prompt_tokens"] == 1500
        assert usage["completion_tokens"] == 300

    def test_cost_per_k(self):
        from app.api.router_system import _usage_cost
        billing = {"unit": "per_k", "input_price": 2.0, "output_price": 8.0}
        # 输入 1500/千×2 + 输出 300/千×8 = 3 + 2.4 = 5.4
        assert _usage_cost(
            {"prompt_tokens": 1500, "completion_tokens": 300}, billing) == 5.4

    def test_cost_per_m(self):
        from app.api.router_system import _usage_cost
        billing = {"unit": "per_m", "input_price": 2.0, "output_price": 8.0}
        cost = _usage_cost(
            {"prompt_tokens": 1_500_000, "completion_tokens": 0}, billing)
        assert abs(cost - 3.0) < 1e-9
