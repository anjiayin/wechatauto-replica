# -*- coding: utf-8 -*-
"""5 项用户反馈改进的回归测试。

覆盖：
- 修复 5（最重要）发送人权威解析：群消息正文前缀（标准 wxid / 自定义
  微信号 / 无前缀回退）、私聊发送人归属、sender_id=2 自己发送、
  群成员缓存命中；
- 修复 2 商机新状态 handled：DDL 迁移（旧库 CHECK 重建）、DAO 筛选、
  默认列表不排除 handled；
- 修复 4 分析日志展示字段：session_display 备注优先、input_text_display
  群消息前缀解析（[HH:MM] 行格式兼容）、私聊原样返回。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_feedback_fixes.py -v
"""
from __future__ import annotations

import time
from typing import List

import pytest

from app.aggregator import Aggregator
from app.db import Database
from app.wechat_service import WeChatService


class SenderFakeWxDb:
    """WeChatDB 替身：群成员表 + 联系人昵称（观测调用次数验证缓存）。"""

    def __init__(self) -> None:
        self.group_members_calls: List[str] = []
        self.nickname_calls: List[str] = []

    def get_group_members(self, chatroom: str) -> list[dict]:
        self.group_members_calls.append(chatroom)
        if chatroom == "111@chatroom":
            return [
                # 备注优先（反馈 5：好友显示我给他的备注，而非其群昵称）
                {"username": "wxid_zhang", "nick_name": "张三(群昵称)",
                 "remark": "zhang的备注", "is_owner": True},
                # 自定义微信号、无备注 → 用群显示名/昵称
                {"username": "andyyu", "nick_name": "", "remark": "余老板",
                 "is_owner": False},
            ]
        return []

    def get_nickname(self, username: str) -> str:
        self.nickname_calls.append(username)
        # 联系人备注/昵称（get_nickname 语义：备注优先）
        return {"wxid_zhang": "张三备注", "wxid_li": "李四(备注)",
                "111@chatroom": "加工外协群"}.get(username, "")


def make_sender_svc() -> WeChatService:
    db = Database(":memory:")
    svc = WeChatService(db, Aggregator(lambda u: None))
    svc._wxdb = SenderFakeWxDb()  # 注入替身（不触碰真实微信）
    return svc


def make_group_msg(content: str, sender_username: str = "wxid_wrong_map",
                   sender_id: int = 3, username: str = "111@chatroom",
                   mtype: str = "文本") -> dict:
    """构造群消息回调 dict（sender_username 默认模拟不可靠的错误映射）。"""
    return {
        "local_id": 1, "type": mtype, "sender_id": sender_id,
        "sender_username": sender_username,
        "create_time": time.time(), "content": content,
        "sort_seq": 1, "username": username,
    }


class TestGroupSenderResolution:
    """修复 5：群消息发送人 = 正文自带前缀（权威），映射仅回退。"""

    def test_standard_wxid_prefix(self):
        svc = make_sender_svc()
        msg = make_group_msg("wxid_zhang:\n有谁能做铝合金CNC")
        svc._on_message(msg)
        assert msg["sender_username"] == "wxid_zhang"
        assert msg["sender_display_name"] == "zhang的备注"  # 好友备注优先（反馈 5）

    def test_custom_weixin_id_prefix(self):
        """自定义微信号前缀（非 wxid_ 开头）同样解析；无群昵称回退 remark。"""
        svc = make_sender_svc()
        msg = make_group_msg("andyyu:\n报价多少")
        svc._on_message(msg)
        assert msg["sender_username"] == "andyyu"
        assert msg["sender_display_name"] == "余老板"

    def test_no_prefix_falls_back_to_sender_username(self):
        """正文无前缀 → 回退 sender_username 映射，再按联系人备注解析。"""
        svc = make_sender_svc()
        msg = make_group_msg("普通文本消息，没有前缀")
        svc._on_message(msg)
        assert msg["sender_username"] == "wxid_wrong_map"
        # 不在群成员表 → 回退联系人备注/昵称（替身未命中 → 原值保留）
        assert msg["sender_display_name"] == "wxid_wrong_map"

    def test_no_prefix_resolves_via_contact(self):
        """无前缀且回退 ID 不在群成员表但在联系人库有备注 → 用备注。"""
        svc = make_sender_svc()
        msg = make_group_msg("无前缀消息", sender_username="wxid_li")
        svc._on_message(msg)
        assert msg["sender_username"] == "wxid_li"
        assert msg["sender_display_name"] == "李四(备注)"

    def test_group_member_cache_hit(self):
        """同一群两次解析只读一次群成员表（缓存 TTL 600s 命中）。"""
        svc = make_sender_svc()
        svc._on_message(make_group_msg("wxid_zhang:\n第一条"))
        svc._on_message(make_group_msg("andyyu:\n第二条"))
        assert svc._wxdb.group_members_calls == ["111@chatroom"]
        # 过期后重新读取（缓存条目为 (ts, names, ok) 三元组）
        cached = svc._group_members_cache["111@chatroom"]
        svc._group_members_cache["111@chatroom"] = (
            cached[0] - 601, cached[1], cached[2])
        svc._on_message(make_group_msg("wxid_zhang:\n第三条"))
        assert svc._wxdb.group_members_calls == ["111@chatroom"] * 2

    def test_extract_content_sender_unit(self):
        assert WeChatService.extract_content_sender("wxid_a:\n正文") == "wxid_a"
        assert WeChatService.extract_content_sender("andyyu: 报价") == "andyyu"
        assert WeChatService.extract_content_sender("无前缀正文") == ""
        assert WeChatService.extract_content_sender("") == ""

    def test_non_text_log_uses_authoritative_sender(self):
        """非文本消息的 skipped 日志 sender_wxid 同步使用解析后的权威值。"""
        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        svc._wxdb = SenderFakeWxDb()
        svc._on_message(make_group_msg("[图片]", mtype="图片",
                                       sender_username="wxid_zhang",
                                       sender_id=3))
        row = db._conn.execute(
            "SELECT sender_wxid FROM analysis_logs LIMIT 1").fetchone()
        assert row["sender_wxid"] == "wxid_zhang"


class TestPrivateSenderResolution:
    """修复 5：私聊发送人 = 会话对方（与 sender_username 映射无关）。"""

    def test_private_sender_is_session_peer(self):
        """真实案例：映射错误解析成 'andyyu'，修复后发送人 = 会话对方。"""
        svc = make_sender_svc()
        msg = make_group_msg("在吗？你们那能加工吗",
                             sender_username="andyyu",  # 错误映射值
                             sender_id=3,
                             username="wxid_li")        # 私聊会话 = 李四
        svc._on_message(msg)
        assert msg["sender_username"] == "wxid_li"
        assert msg["sender_display_name"] == "李四(备注)"  # get_nickname 备注优先

    def test_private_own_message_shows_me(self):
        """sender_id=2 为自己发送：显示「我」，wxid 置空。"""
        svc = make_sender_svc()
        msg = make_group_msg("我方回复内容", sender_username="andyyu",
                             sender_id=2, username="wxid_li")
        svc._on_message(msg)
        assert msg["sender_username"] == ""
        assert msg["sender_display_name"] == "我"


class TestHandledStatus:
    """修复 2：商机新状态 handled（已自行处理）。"""

    def test_fresh_db_accepts_handled(self):
        db = Database(":memory:")
        opp = db.insert_opportunity(
            session_username="111@chatroom", session_name="群",
            sender_wxid="wxid_a", sender_name="张三",
            window_start="2026-11-02T14:02:00",
            window_end="2026-11-02T14:05:00", merged_count=1,
            raw_text="原文", type_id=None, type_name="t",
            summary="要点", key_quote="", confidence=80)
        assert db.update_opportunity_status(opp["id"], "handled") == 1
        assert db.get_opportunity(opp["id"])["status"] == "handled"

    def test_handled_shown_in_default_list(self):
        """默认列表（status 为空）只排除 ignored，handled 正常显示。"""
        db = Database(":memory:")
        opp = db.insert_opportunity(
            session_username="111@chatroom", session_name="群",
            sender_wxid="wxid_a", sender_name="张三",
            window_start="2026-11-02T14:02:00",
            window_end="2026-11-02T14:05:00", merged_count=1,
            raw_text="原文", type_id=None, type_name="t",
            summary="要点", key_quote="", confidence=80)
        db.update_opportunity_status(opp["id"], "handled")
        total, rows = db.list_opportunities()
        assert total == 1 and rows[0]["status"] == "handled"
        # 精确筛选 handled
        total, _ = db.list_opportunities(status="handled")
        assert total == 1

    def test_legacy_db_migration(self, tmp_path):
        """旧库 CHECK 不含 handled → 打开时自动重建表，历史数据保留。"""
        from app.db import _OPPORTUNITIES_COLUMNS
        path = str(tmp_path / "legacy.sqlite3")
        db1 = Database(path)
        opp = db1.insert_opportunity(
            session_username="111@chatroom", session_name="群",
            sender_wxid="wxid_a", sender_name="张三",
            window_start="2026-11-02T14:02:00",
            window_end="2026-11-02T14:05:00", merged_count=1,
            raw_text="原文", type_id=None, type_name="t",
            summary="要点", key_quote="", confidence=80)
        # 模拟旧库：按旧 CHECK 约束重建表
        old_columns = _OPPORTUNITIES_COLUMNS.replace(
            "'pending','replied','ignored','handled'",
            "'pending','replied','ignored'")
        db1._conn.commit()
        db1._conn.execute("PRAGMA foreign_keys=OFF")
        db1._conn.execute(
            "CREATE TABLE opportunities_legacy (%s)" % old_columns)
        db1._conn.execute("INSERT INTO opportunities_legacy SELECT * FROM opportunities")
        db1._conn.execute("DROP TABLE opportunities")
        db1._conn.execute(
            "ALTER TABLE opportunities_legacy RENAME TO opportunities")
        db1._conn.commit()
        db1._conn.execute("PRAGMA foreign_keys=ON")
        db1.close()

        db2 = Database(path)  # 打开即触发迁移
        # 历史数据保留
        assert db2.get_opportunity(opp["id"])["status"] == "pending"
        # 新状态可写入
        assert db2.update_opportunity_status(opp["id"], "handled") == 1
        assert db2.get_opportunity(opp["id"])["status"] == "handled"
        # analysis_logs 外键仍指向 opportunities（迁移后引用未破坏）
        log_id = db2.insert_analysis_log(
            session_username="111@chatroom", msg_count=1, status="ok",
            opportunity_id=opp["id"])
        assert log_id > 0
        db2.close()

    def test_migration_is_idempotent(self, tmp_path):
        path = str(tmp_path / "idem.sqlite3")
        Database(path).close()
        db2 = Database(path)  # 第二次打开：已含 handled，不重复重建
        assert db2.get_setting("analysis.strictness") == "balanced"
        db2.close()


class TestLogDisplays:
    """修复 4：分析日志 session_display 与 input_text_display。"""

    def test_session_display_prefers_remark(self):
        svc = make_sender_svc()
        display, _ = svc.build_log_displays("111@chatroom", "正文")
        assert display == "加工外协群"  # get_nickname 备注/昵称优先

    def test_session_display_fallback_to_username(self):
        db = Database(":memory:")
        svc = WeChatService(db, Aggregator(lambda u: None))
        display, _ = svc.build_log_displays("unknown_wxid", "正文")
        assert display == "unknown_wxid"

    def test_group_input_text_prefix_resolved(self):
        """[HH:MM] 行格式兼容：前缀行与内联前缀行都解析为显示名。"""
        svc = make_sender_svc()
        text = "[14:02] wxid_zhang:\n[14:02] 有谁能做铝合金CNC"
        _, display_text = svc.build_log_displays("111@chatroom", text)
        assert "[14:02] zhang的备注:" in display_text  # 好友备注优先（反馈 5）
        assert "有谁能做铝合金CNC" in display_text

    def test_group_input_text_inline_prefix(self):
        """无换行的内联前缀（wxid_xxx: 正文）同样解析。"""
        svc = make_sender_svc()
        _, display_text = svc.build_log_displays(
            "111@chatroom", "andyyu: 报价多少")
        assert display_text == "余老板: 报价多少"

    def test_group_input_text_unresolvable_keeps_original(self):
        """前缀解析失败（非群成员也非联系人）→ 整行保留原值，不误伤正文。"""
        svc = make_sender_svc()
        _, display_text = svc.build_log_displays(
            "111@chatroom", "[14:02] 型号ABC: 需要报价")
        assert display_text == "[14:02] 型号ABC: 需要报价"

    def test_private_input_text_unchanged(self):
        """私聊无发送者前缀行，原文原样返回。"""
        svc = make_sender_svc()
        text = "[14:02] 在吗？能做CNC吗"
        _, display_text = svc.build_log_displays("wxid_li", text)
        assert display_text == text
