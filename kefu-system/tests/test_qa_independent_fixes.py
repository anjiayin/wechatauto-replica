# -*- coding: utf-8 -*-
"""QA 独立回归：任务 #1（配置水位过滤）与任务 #2（私聊好友白名单）边界用例。

与工程师自测不同，本文件只盯「容易写错且自测容易漏」的边界：

修复1（monitor_since）
- 恰好等于水位必须放行（>= 而非 >）；
- 毫秒历史消息（不只是毫秒新消息）必须被丢弃；
- 时间缺失/非法/非正数、水位非法、DAO 异常、db=None 一律放行；
- 历史消息在 skipped 日志 / 图片入队之前就被拦截；
- 老库迁移连开 3 次幂等，且迁移后 insert_target 带水位可用。

修复2（好友白名单）
- 现场「淼淼」场景：多个含关键词的非好友 + 2 个真好友，搜索只回 2 个；
- 只在 get_contacts("friend") 里、不在会话表里的好友仍出现在候选；
- 企业微信好友 is_friend 复核抛异常时按保留处理（不误伤）；
- 群聊 / is_self 数量与语义不变；
- contact.db 读取异常时降级放行（记录该行为会漏过非好友，属既定取舍）。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.api.router_targets import create_target, update_target
from app.db import Database
from app.schemas import MonitorTargetCreate, MonitorTargetUpdate
from app.wechat_service import WeChatService
from wechatauto.db import WeChatDB

NOW = 1_760_000_000  # 2025-10 左右的 epoch 秒，作为测试水位


# ---------------------------------------------------------------------------
# 公共替身
# ---------------------------------------------------------------------------
class _SpyAgg:
    """记录进入商机合并窗口的消息。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def add(self, msg: dict, session_name: str, kind: str) -> bool:
        self.calls.append(msg)
        return True


class _ApiSvc:
    """router 依赖的最小服务替身。"""

    def __init__(self) -> None:
        self.registered: list[str] = []

    def get_display_name(self, username: str) -> str:
        return ""

    def is_self_username(self, username: str) -> bool:
        return False

    def register_target(self, username: str) -> None:
        self.registered.append(username)

    def unregister_target(self, username: str) -> None:
        return None


class _BrokenTargetDb:
    """get_target_by_username 抛异常的 DAO 替身（验证降级放行）。"""

    def get_target_by_username(self, username: str) -> dict:
        raise RuntimeError("contact/target db unavailable")


def _msg(username: str, create_time, *, mtype: str = "文本",
         content: str = "wxid_seller:\n我要一台发动机") -> dict:
    return {
        "local_id": 7,
        "type": mtype,
        "sender_id": 3,
        "sender_username": "wxid_seller",
        "create_time": create_time,
        "content": content,
        "sort_seq": 9,
        "username": username,
    }


def _svc_with_target(username: str = "111@chatroom", since: int = NOW,
                     kind: str = "group"):
    db = Database(":memory:")
    db.insert_target(username, "测试会话", kind, monitor_since=since)
    agg = _SpyAgg()
    svc = WeChatService(db, agg)  # type: ignore[arg-type]
    return db, svc, agg


class _FakeWx:
    """WeChatDB 替身：区分会话表 / 通讯录好友 / 企业微信联系人。"""

    def __init__(self, *, sessions, friends, openim=None, chatrooms=None,
                 fail_friend=False, fail_openim=False,
                 is_friend_raises=False, friend_check=None,
                 self_id="wxid_self"):
        self.sessions = sessions
        self.contacts = {
            "friend": friends,
            "openim": openim or [],
            "chatroom": chatrooms or [],
        }
        self.fail_friend = fail_friend
        self.fail_openim = fail_openim
        self.is_friend_raises = is_friend_raises
        self.friend_check = friend_check
        self.self_id = self_id

    def get_sessions(self, limit: int = 100):
        return list(self.sessions)

    def get_contacts(self, kind: str = "friend", limit: int = 0):
        if kind == "friend" and self.fail_friend:
            raise RuntimeError("contact.db 暂不可用")
        if kind == "openim" and self.fail_openim:
            raise RuntimeError("openim 表暂不可用")
        return list(self.contacts.get(kind, []))

    def is_friend(self, username: str) -> bool:
        if self.is_friend_raises:
            raise RuntimeError("is_friend 复核失败")
        if self.friend_check is not None:
            return username in self.friend_check
        for rows in self.contacts.values():
            for r in rows:
                if r.get("username") == username:
                    return True
        return False

    def get_openim_corp_names(self):
        return {}

    def get_self_info(self):
        return {"username": self.self_id, "nick_name": "我", "remark": ""}

    def get_nickname(self, username: str) -> str:
        for rows in self.contacts.values():
            for r in rows:
                if r.get("username") == username:
                    return r.get("remark") or r.get("nick_name") or ""
        return ""


def _qa_svc(wx: _FakeWx) -> WeChatService:
    svc = WeChatService(None, None)  # type: ignore[arg-type]
    svc._wxdb = wx  # type: ignore[assignment]
    return svc


# ===========================================================================
# 修复1：时间过滤边界
# ===========================================================================
class TestQAMonitorSinceEdges:
    """配置水位比较的量纲、边界与异常分支。"""

    def test_exactly_at_watermark_is_allowed(self):
        """边界取等必须放行（实现应为 epoch >= since）。"""
        _, svc, agg = _svc_with_target()
        svc._on_message(_msg("111@chatroom", NOW))
        assert len(agg.calls) == 1, "恰好等于水位应正常进入商机分析"

    def test_one_second_before_watermark_dropped(self):
        _, svc, agg = _svc_with_target()
        svc._on_message(_msg("111@chatroom", NOW - 1))
        assert agg.calls == []

    def test_millisecond_history_message_dropped(self):
        """毫秒量纲的历史消息（now-100 秒）必须被丢弃，而非被放大后放行。"""
        _, svc, agg = _svc_with_target()
        svc._on_message(_msg("111@chatroom", (NOW - 100) * 1000))
        assert agg.calls == [], "毫秒历史消息误入商机分析"

    def test_millisecond_exactly_at_watermark_allowed(self):
        _, svc, agg = _svc_with_target()
        svc._on_message(_msg("111@chatroom", NOW * 1000))
        assert len(agg.calls) == 1

    def test_numeric_string_create_time(self):
        """Listener 偶发把 create_time 序列化成字符串数值时仍可比较。"""
        _, svc, agg = _svc_with_target()
        svc._on_message(_msg("111@chatroom", str(NOW - 1)))
        svc._on_message(_msg("111@chatroom", str(NOW + 1)))
        assert len(agg.calls) == 1
        assert agg.calls[0]["create_time"] == str(NOW + 1)

    def test_iso8601_and_garbage_create_time_allowed(self):
        """对外 API 的 ISO8601 / 非法串无法 float() 时应放行，不误伤。"""
        _, svc, agg = _svc_with_target()
        for bad in ("2025-10-01T00:00:00", "not-a-time", [], {}):
            svc._on_message(_msg("111@chatroom", bad))
        assert len(agg.calls) == 4

    def test_none_zero_negative_create_time_allowed(self):
        _, svc, agg = _svc_with_target()
        for bad in (None, 0, -5):
            svc._on_message(_msg("111@chatroom", bad))
        assert len(agg.calls) == 3

    def test_broken_monitor_since_value_allowed(self):
        """水位列被人工改成非法值时应放行（since 归 0）。"""
        db, svc, agg = _svc_with_target()
        db._conn.execute(
            "UPDATE monitor_targets SET monitor_since='not-a-number' "
            "WHERE username='111@chatroom'")
        db._conn.commit()
        svc._on_message(_msg("111@chatroom", 1_640_000_000))
        assert len(agg.calls) == 1

    def test_target_dao_exception_degrades_to_allow(self):
        """读水位失败（替身/降级）时不拦截，保持原行为。"""
        svc = WeChatService(_BrokenTargetDb(), None)  # type: ignore[arg-type]
        assert svc._is_before_monitor_since("111@chatroom",
                                            _msg("111@chatroom", 1)) is False

    def test_no_db_degrades_to_allow(self):
        svc = WeChatService(None, None)  # type: ignore[arg-type]
        assert svc._is_before_monitor_since("111@chatroom",
                                            _msg("111@chatroom", 1)) is False

    def test_history_image_does_not_enter_vision_queue(self):
        """历史图片必须在图片入队前被拦截（用户报告的队列打满场景）。"""
        _, svc, agg = _svc_with_target()
        enqueued: list[dict] = []
        svc._enqueue_image_message = (  # type: ignore[method-assign]
            lambda msg, **kw: enqueued.append({**kw, "msg": msg}))
        svc._on_message(_msg("111@chatroom", NOW - 10, mtype="图片",
                             content="[图片]"))
        assert enqueued == []
        logs = svc._db._conn.execute(  # type: ignore[union-attr]
            "SELECT COUNT(*) FROM analysis_logs").fetchone()[0]
        assert logs == 0, "历史图片不应落 skipped 日志"

    def test_mixed_batch_only_after_messages_enter(self):
        """同一会话回放一批消息：只有水位之后的进入聚合。"""
        _, svc, agg = _svc_with_target()
        for ts in (NOW - 300, NOW - 2, NOW - 1):
            svc._on_message(_msg("111@chatroom", ts))
        svc._on_message(_msg("111@chatroom", NOW))
        svc._on_message(_msg("111@chatroom", NOW + 1))
        assert [m["create_time"] for m in agg.calls] == [NOW, NOW + 1]

    def test_friend_session_history_dropped_and_no_skip_log(self):
        """私聊会话同样在 skipped 日志之前拦截。"""
        _, svc, agg = _svc_with_target("wxid_friend", kind="friend")
        svc._on_message(_msg("wxid_friend", 1_640_000_000,
                             mtype="图片", content="[图片]"))
        assert agg.calls == []
        logs = svc._db._conn.execute(  # type: ignore[union-attr]
            "SELECT COUNT(*) FROM analysis_logs").fetchone()[0]
        assert logs == 0


class TestQAWaterMarkApiAndMigration:
    """水位写入 API 与老库迁移的额外边界。"""

    def test_reenable_then_second_reenable_only_refreshes_once(self):
        """停用→启用刷新；再停用→启用应再次刷新（每次都是重新配置）。"""
        db = Database(":memory:")
        svc = _ApiSvc()
        t = db.insert_target("wxid_f", "张三", "friend",
                             monitor_since=1_700_000_000)
        update_target(t["id"], MonitorTargetUpdate(enabled=False),
                      db, svc)  # type: ignore[arg-type]
        update_target(t["id"], MonitorTargetUpdate(enabled=True),
                      db, svc)  # type: ignore[arg-type]
        first = db.get_target(t["id"])["monitor_since"]
        update_target(t["id"], MonitorTargetUpdate(enabled=False),
                      db, svc)  # type: ignore[arg-type]
        update_target(t["id"], MonitorTargetUpdate(enabled=True),
                      db, svc)  # type: ignore[arg-type]
        second = db.get_target(t["id"])["monitor_since"]
        assert first >= 1_700_000_000 and second >= first

    def test_api_create_then_history_filtered(self):
        """端到端：API 创建 target 后，早于创建时刻的消息被过滤。"""
        db = Database(":memory:")
        out = create_target(
            MonitorTargetCreate(username="50640000722@chatroom",
                                display_name="测试群"),
            db, _ApiSvc())  # type: ignore[arg-type]
        since = db.get_target(out.id)["monitor_since"]
        agg = _SpyAgg()
        svc = WeChatService(db, agg)  # type: ignore[arg-type]
        svc._on_message(_msg("50640000722@chatroom", since - 3600))
        svc._on_message(_msg("50640000722@chatroom", since + 1))
        assert len(agg.calls) == 1

    def test_legacy_db_migration_three_times_and_insert_after(self, tmp_path):
        """老库连开 3 次不报错，且迁移后带水位插入可用。"""
        path = str(tmp_path / "legacy_qa.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE monitor_targets ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, "
            "kind TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')))")
        conn.execute(
            "INSERT INTO monitor_targets (username, display_name, kind) "
            "VALUES ('wxid_old_qa', '老目标', 'friend')")
        conn.commit()
        conn.close()

        for i in range(3):
            db = Database(path)
            cols = {r["name"] for r in db._conn.execute(
                "PRAGMA table_info(monitor_targets)")}
            assert "monitor_since" in cols, f"第 {i + 1} 次初始化缺列"
            row = db.get_target_by_username("wxid_old_qa")
            assert row is not None and row["monitor_since"] == 0
            if i == 2:
                t = db.insert_target("wxid_new_qa", "新目标", "friend",
                                     monitor_since=NOW)
                assert db.get_target(t["id"])["monitor_since"] == NOW
            db.close()

    def test_new_db_reopen_keeps_column(self, tmp_path):
        """新库 DDL 带列，重开仍是同一列、不重复 ALTER。"""
        path = str(tmp_path / "fresh_qa.db")
        db = Database(path)
        db.insert_target("wxid_a", "A", "friend", monitor_since=NOW)
        db.close()
        db = Database(path)
        assert db.get_target_by_username("wxid_a")["monitor_since"] == NOW
        db.close()


# ===========================================================================
# 修复2：好友白名单
# ===========================================================================
class TestQAFriendWhitelist:
    """私聊候选白名单的现场场景与口径边界。"""

    def test_miaomiao_scene_only_two_real_friends(self):
        """多个含「淼淼」的非好友会话 + 2 个真好友，搜索只回真好友。"""
        wx = _FakeWx(
            sessions=[
                {"username": "wxid_miao_teacher"},
                {"username": "wxid_miao_daddy"},
                {"username": "wxid_group_member_miao"},
                {"username": "wxid_deleted_miao"},
                {"username": "wxid_stranger_miao"},
            ],
            friends=[
                {"username": "wxid_miao_teacher",
                 "nick_name": "MIKE 淼淼英语老师"},
                {"username": "wxid_miao_daddy",
                 "nick_name": "张鹏 大淼淼爸爸"},
            ])
        hits = _qa_svc(wx).search_sessions(keyword="淼淼", kind="friend")
        assert sorted(i["username"] for i in hits) == [
            "wxid_miao_daddy", "wxid_miao_teacher"]

    def test_friend_only_in_contacts_still_listed(self):
        """只在通讯录里、未出现在近期会话表的好友仍应出现在候选。"""
        wx = _FakeWx(
            sessions=[{"username": "wxid_stranger"}],
            friends=[{"username": "wxid_friend_only",
                      "nick_name": "通讯录好友"}])
        items = {i["username"]: i for i in _qa_svc(wx)._build_contact_list()}
        assert items["wxid_friend_only"]["kind"] == "friend"
        assert "wxid_stranger" not in items

    def test_openim_is_friend_check_exception_keeps_contact(self):
        """复核企业微信好友关系抛异常时按保留处理，不误伤真好友。"""
        wx = _FakeWx(
            sessions=[{"username": "123@openim"}],
            friends=[],
            openim=[{"username": "123@openim", "nick_name": "企业好友"}],
            is_friend_raises=True)
        items = {i["username"]: i for i in _qa_svc(wx)._build_contact_list()}
        assert "123@openim" in items
        assert items["123@openim"]["kind"] == "friend"

    def test_group_and_self_counts_unchanged(self):
        """群聊与 is_self 不受好友过滤影响，数量与标记保持。"""
        wx = _FakeWx(
            sessions=[{"username": "g1@chatroom"},
                      {"username": "g2@chatroom"},
                      {"username": "wxid_self"},
                      {"username": "wxid_nonfriend"}],
            friends=[])
        items = _qa_svc(wx)._build_contact_list()
        groups = [i for i in items if i["kind"] == "group"]
        selfs = [i for i in items if i["is_self"]]
        assert sorted(i["username"] for i in groups) == [
            "g1@chatroom", "g2@chatroom"]
        assert [i["username"] for i in selfs] == ["wxid_self"]
        assert "wxid_nonfriend" not in {i["username"] for i in items}

    def test_contact_db_failure_degrades_and_leaks_nonfriend_documented(self):
        """降级语义：好友表读不到时放行全部会话（会漏过非好友，既定取舍）。"""
        wx = _FakeWx(
            sessions=[{"username": "wxid_unknown_a"},
                      {"username": "wxid_unknown_b"}],
            friends=[], fail_friend=True)
        items = {i["username"]: i for i in _qa_svc(wx)._build_contact_list()}
        assert "wxid_unknown_a" in items and "wxid_unknown_b" in items

    def test_openim_read_failure_disables_strict_filter_documented(self):
        """记录现状：openim 读失败时 strict_friend_filter 整体关闭。

        这是既有降级取舍（宁可多显示也不漏真好友），本用例锁住当前行为，
        避免后续改动时无声变化；若产品要求「好友表可用就必须严格过滤」
        则需工程师调整 strict_friend_filter 的耦合方式。
        """
        wx = _FakeWx(
            sessions=[{"username": "wxid_real_friend"},
                      {"username": "wxid_nonfriend"}],
            friends=[{"username": "wxid_real_friend", "nick_name": "真好友"}],
            fail_openim=True)
        items = {i["username"]: i for i in _qa_svc(wx)._build_contact_list()}
        assert "wxid_real_friend" in items
        assert "wxid_nonfriend" in items  # 降级放行


# ===========================================================================
# 补强验证：openim 脏 local_type 复核 + 第二轮 contacts 白名单约束
# ===========================================================================
class _FakeWxNoFriendCheck(_FakeWx):
    """无 is_friend 能力的替身（模拟旧版 wechatauto / 降级场景）。"""

    @property
    def is_friend(self):
        raise AttributeError("is_friend not implemented")


class _RealContactWeChatDb(WeChatDB):
    """真实 WeChatDB.get_contacts / is_friend 口径 + 受控 contact.db。

    绕过需要解密的 __init__，只替换 _contact_conn 指向临时 contact.db，
    从而用**生产 SQL** 验证 local_type / delete_flag 白名单口径。
    """

    def __init__(self, db_path: str, rows: list[tuple],
                 sessions: list[dict]) -> None:
        self.db_dir = ""
        # wxid 是只读 property：由 account 去掉 _XXXX 后缀推导
        # （"wxid_me" 后缀不是 4 位，故保持原值）。
        self.account = "wxid_me"
        self.account_dir = ""
        self.workdir = ""
        self._db_files: list = []
        self._qa_db_path = db_path
        self._qa_sessions = list(sessions)
        conn = self._contact_conn()
        try:
            conn.execute(
                "CREATE TABLE contact (username TEXT, alias TEXT, "
                "nick_name TEXT, remark TEXT, description TEXT, "
                "big_head_url TEXT, local_type INTEGER, delete_flag INTEGER)")
            conn.executemany(
                "INSERT INTO contact VALUES (?,?,?,?,?,?,?,?)", rows)
            conn.commit()
        finally:
            conn.close()

    def _contact_conn(self):
        conn = sqlite3.connect(self._qa_db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_sessions(self, limit: int = 100):
        return list(self._qa_sessions)

    def get_openim_corp_names(self):
        return {}

    def get_self_info(self):
        return {"username": self.wxid, "nick_name": "我", "remark": ""}

    def get_nickname(self, username: str) -> str:
        conn = self._contact_conn()
        try:
            row = conn.execute(
                "SELECT nick_name, remark FROM contact WHERE username=?",
                (username,)).fetchone()
        finally:
            conn.close()
        return (row["remark"] or row["nick_name"]) if row else ""


def _contact_row(username: str, local_type: int, *,
                 delete_flag: int = 0, nick: str = "") -> tuple:
    return (username, "", nick or username, "", "", "",
            local_type, delete_flag)


class TestQAOpenimWhitelistHardening:
    """补强点：openim 脏 local_type=0 必须挡；第二轮 contacts 同样受白名单。"""

    def test_second_loop_openim_dirty_contact_filtered(self):
        """脏 @openim 只在通讯录、不在会话表时，也不能从第二轮混进候选。"""
        wx = _FakeWx(
            sessions=[{"username": "wxid_stranger"}],
            friends=[],
            openim=[{"username": "999_dirty@openim", "nick_name": "脏数据"},
                    {"username": "666_real@openim", "nick_name": "真企业好友"}],
            friend_check={"666_real@openim"})
        items = {i["username"]: i for i in _qa_svc(wx)._build_contact_list()}
        assert "999_dirty@openim" not in items, "第二轮 contacts 漏过 openim 脏数据"
        assert "666_real@openim" in items
        assert "wxid_stranger" not in items

    def test_real_wxdb_sql_semantics_both_loops(self, tmp_path):
        """用真实 get_contacts SQL + is_friend 验证 local_type/delete_flag 口径。"""
        path = str(tmp_path / "qa_contact.db")
        rows = [
            _contact_row("wxid_real_friend", 3, nick="通讯录好友"),
            _contact_row("wxid_chat_only", 1, nick="仅聊天好友"),
            _contact_row("12345@openim", 5, nick="企业微信好友5"),
            _contact_row("67890@openim", 7, nick="企业微信好友7"),
            _contact_row("999_dirty@openim", 0, nick="脏数据local_type0"),
            _contact_row("777_deleted@openim", 5, delete_flag=1,
                         nick="已删企业好友"),
            _contact_row("888_contactonly@openim", 0,
                         nick="仅通讯录脏数据"),
            _contact_row("666_contactonly@openim", 6,
                         nick="仅通讯录真企业好友"),
            _contact_row("g1@chatroom", 2, nick="测试群"),
        ]
        sessions = [
            {"username": "wxid_real_friend"},
            {"username": "wxid_chat_only"},
            {"username": "12345@openim"},
            {"username": "67890@openim"},
            {"username": "999_dirty@openim"},
            {"username": "777_deleted@openim"},
            {"username": "wxid_stranger"},
            {"username": "g1@chatroom"},
            {"username": "wxid_me"},
        ]
        wx = _RealContactWeChatDb(path, rows, sessions)
        svc = WeChatService(None, None)  # type: ignore[arg-type]
        svc._wxdb = wx  # type: ignore[assignment]
        items = {i["username"]: i for i in svc._build_contact_list()}
        # 会话路径：真好友与 5/7 企业微信好友保留
        assert "wxid_real_friend" in items and "wxid_chat_only" in items
        assert "12345@openim" in items and "67890@openim" in items
        # 会话路径：脏数据 / 已删好友 / 非好友临时会话全部挡掉
        assert "999_dirty@openim" not in items
        assert "777_deleted@openim" not in items
        assert "wxid_stranger" not in items
        # 第二轮 contacts 路径（新补强）：真企业好友留、脏数据挡
        assert "666_contactonly@openim" in items
        assert "888_contactonly@openim" not in items
        # 群 / self 不受影响
        assert items["g1@chatroom"]["kind"] == "group"
        assert items["wxid_me"]["is_self"] is True

    def test_no_is_friend_method_degrades_keep_in_both_loops(self):
        """无 is_friend 能力时两路都降级放行，不误伤（沿用 get_contacts 结果）。"""
        wx = _FakeWxNoFriendCheck(
            sessions=[{"username": "999_dirty@openim"}],
            friends=[],
            openim=[{"username": "999_dirty@openim", "nick_name": "脏"},
                    {"username": "888_contactonly@openim",
                     "nick_name": "仅通讯录脏"}])
        items = {i["username"]: i for i in _qa_svc(wx)._build_contact_list()}
        assert "999_dirty@openim" in items
        assert "888_contactonly@openim" in items

    def test_is_friend_exception_degrades_keep_in_both_loops(self):
        """is_friend 复核抛异常时按保留处理（会话路径 + 第二轮路径）。"""
        wx = _FakeWx(
            sessions=[{"username": "999_dirty@openim"}],
            friends=[],
            openim=[{"username": "999_dirty@openim", "nick_name": "脏"},
                    {"username": "888_contactonly@openim",
                     "nick_name": "仅通讯录脏"}],
            is_friend_raises=True)
        items = {i["username"]: i for i in _qa_svc(wx)._build_contact_list()}
        assert "999_dirty@openim" in items
        assert "888_contactonly@openim" in items

    def test_recheck_offline_does_not_hide_real_friend_session(self):
        """降级保留时真好友仍在会话路径出现（不会被白名单误删）。"""
        wx = _FakeWx(
            sessions=[{"username": "wxid_real_friend"},
                      {"username": "123@openim"}],
            friends=[{"username": "wxid_real_friend", "nick_name": "真好友"}],
            openim=[{"username": "123@openim", "nick_name": "企业好友"}],
            is_friend_raises=True)
        items = {i["username"]: i for i in _qa_svc(wx)._build_contact_list()}
        assert "wxid_real_friend" in items and "123@openim" in items


class TestQAReplayPaths:
    """三条真实 watermark 回放路径最终都汇聚到 _on_message，闸门须拦住。"""

    @pytest.mark.parametrize("path", [
        "a_add_listener_first_read_empty_watermark_0",
        "b_stale_watermark_loaded_from_db",
        "c_poll_three_failures_watermark_0",
    ])
    def test_history_blocked_regardless_of_replay_path(self, path):
        _, svc, agg = _svc_with_target()
        # 三条路径仅在 Listener 侧把 watermark 归零/变旧的机制不同；
        # 投递到本系统的消息形态一致：create_time 是消息库原始 epoch 秒。
        svc._on_message(_msg("111@chatroom", 1_640_000_000))  # 2022 历史
        assert agg.calls == [], f"{path}: 历史消息未被闸门拦住"
        logs = svc._db._conn.execute(  # type: ignore[union-attr]
            "SELECT COUNT(*) FROM analysis_logs").fetchone()[0]
        assert logs == 0, f"{path}: 历史消息落了 skipped 日志"
        svc._on_message(_msg("111@chatroom", NOW + 1))
        assert len(agg.calls) == 1, f"{path}: 配置后消息被误伤"

    def test_listener_watermark_zero_premise(self):
        """前提核实：add_listener 首读为空 → watermark=0；灌入旧值 → 保持旧值。"""
        from wechatauto.db import Listener

        class _EmptyDb:
            def get_messages(self, user, limit=20, offset=0):
                return []

        lst = Listener(_EmptyDb())  # type: ignore[arg-type]
        lst.add_listener("111@chatroom", lambda m, l: None)
        assert lst.watermark["111@chatroom"] == 0

        stale = Listener(_EmptyDb(),  # type: ignore[arg-type]
                         watermark={"111@chatroom": 5})
        assert stale.watermark["111@chatroom"] == 5
