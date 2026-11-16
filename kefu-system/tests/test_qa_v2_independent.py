# -*- coding: utf-8 -*-
"""QA 独立回归（第二轮，严过关）——两个修复的第二层质量关卡。

本文件**不复用**工程师自测用例的断言，独立设计以下高价值场景：

修复 1（启动时刻水位 + watermark 源头推进）：
- 三段消息一次性验证：配置前历史 / 配置后但启动前停机消息 / 启动后新消息，
  只有第三段进聚合，三段都不落 skipped 日志、不进视觉队列；
- 秒级边界：早 1 秒丢弃、同秒放行、浮点启动时刻**向下取整**（防误用 round）、
  毫秒 create_time 与浮点水位叠加；
- 双闸门独立：DAO 异常 / 仅单闸门 / 各类非法 create_time 一律放行；
- watermark 推进：`add_listener` 真语义（只在 user 不在 watermark 时赋值）
  不覆盖推进值、flush 持久化推进值、sort_seq 类型脏值过滤。

修复 2（私聊好友口径，结论「不改源码」）：
- 用**真实 contact.db** 亲自查证两名 lt=1 真好友存在，证明「收紧到 lt=3」
  会误删用户点名要的人；
- 按真实数据形态（2 名 lt=1 + 5 名 lt=3 + 各类非好友 + openim 脏数据）
  锁死白名单口径与搜索结果。

所有测试只用替身 / 内存库，不碰 UIA 实机、不改生产数据。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import wechatauto.db as wxdbmod

from app import wechat_service as wsmod
from app.db import Database
from app.wechat_service import WeChatService

# 真实 contact.db（只读复核用；不存在时相关测试 skip，不影响其它用例）
_REAL_CONTACT_DB = Path(__file__).resolve().parents[1] / \
    "data" / "wechat_cache" / "contact__contact.db"

_CONFIG = 1_700_000_000          # 配置完成时刻（epoch 秒）
_START = _CONFIG + 86_400        # 本次启动时刻（停机一整天后）


# ---------------------------------------------------------------------------
# 公共替身
# ---------------------------------------------------------------------------
class _SpyAgg:
    """记录进入商机合并窗口的消息（不触发真实 flush/AI）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def add(self, msg: dict, session_name: str, kind: str) -> bool:
        self.calls.append(msg)
        return True


def _msg(create_time, *, username: str = "111@chatroom",
         mtype: str = "文本", content: str = "我要一台发动机") -> dict:
    return {
        "local_id": 1,
        "type": mtype,
        "sender_id": 3,
        "sender_username": "wxid_seller",
        "create_time": create_time,
        "content": content,
        "sort_seq": 999,
        "username": username,
    }


def _svc(*, monitor_since: int = _CONFIG,
         session_start: float = 0.0):
    """真实内存库 + 配置目标 +  Spy 聚合，返回 (db, svc, agg)。"""
    db = Database(":memory:")
    db.insert_target("111@chatroom", "测试群", "group",
                     monitor_since=monitor_since)
    agg = _SpyAgg()
    svc = WeChatService(db, agg)  # type: ignore[arg-type]
    svc._session_start_ts = session_start
    return db, svc, agg


def _log_count(db: Database) -> int:
    return db._conn.execute(  # type: ignore[attr-defined]
        "SELECT COUNT(*) FROM analysis_logs").fetchone()[0]


# ===========================================================================
# 1. 三段消息一体化验证
# ===========================================================================
class TestThreeSegmentFlow:
    """配置前 / 停机期 / 启动后三段消息，只有最后一段进入处理链。"""

    def test_only_after_start_segment_reaches_aggregation_and_ai(self):
        """三段各一条：前两段静默丢弃，第三段进聚合；全程零 skipped 日志。"""
        db, svc, agg = _svc(session_start=float(_START))
        enqueued: list[dict] = []
        svc._enqueue_image_message = (  # type: ignore[method-assign]
            lambda msg, **kw: enqueued.append(msg))

        # ① 配置前历史（远早于配置水位）：配置闸门拦
        svc._on_message(_msg(_CONFIG - 3_600, mtype="图片", content="[图片]"))
        # ② 配置后但启动前（停机期间，晚于配置水位）：启动闸门拦
        svc._on_message(_msg(_CONFIG + 100, mtype="图片", content="[图片]"))
        # ③ 启动后真新消息：放行
        svc._on_message(_msg(_START + 5))

        assert len(agg.calls) == 1
        assert agg.calls[0]["create_time"] == _START + 5
        assert enqueued == [], "停机/历史图片不得进视觉队列"
        assert _log_count(db) == 0, "历史消息必须静默丢弃，不落 skipped 日志"

    def test_three_segments_with_millisecond_timestamps(self):
        """毫秒 create_time 下三段口径保持一致。"""
        db, svc, agg = _svc(session_start=float(_START))
        svc._on_message(_msg((_CONFIG - 100) * 1000))
        svc._on_message(_msg((_CONFIG + 100) * 1000))
        svc._on_message(_msg((_START + 5) * 1000))
        assert len(agg.calls) == 1
        assert agg.calls[0]["create_time"] == (_START + 5) * 1000
        assert _log_count(db) == 0


# ===========================================================================
# 2. 启动水位边界（重点：同秒放行 / 早 1 秒丢弃 / 浮点取整）
# ===========================================================================
class TestSessionStartBoundary:
    """秒级口径的精确边界，防 int() 被误改成 round()/ceil()。"""

    def test_one_second_before_start_dropped_same_second_allowed(self):
        """启动秒前一秒丢弃、启动秒本身放行（int 向下取整语义）。"""
        _, svc, agg = _svc(session_start=float(_START) + 0.7)
        svc._on_message(_msg(_START - 1))   # int(start)-1 → 丢弃
        svc._on_message(_msg(_START))       # == int(start) → 放行
        assert len(agg.calls) == 1
        assert agg.calls[0]["create_time"] == _START

    def test_fractional_start_floors_not_rounds(self):
        """启动时刻 .9 时必须向下取整：同秒消息放行（round 会误杀）。"""
        start = 1_700_100_000
        _, svc, agg = _svc(monitor_since=_CONFIG,
                           session_start=float(start) + 0.9)
        svc._on_message(_msg(start))
        assert len(agg.calls) == 1, "round() 会把同秒消息误判为启动前"

    def test_downtime_last_fractional_second_dropped(self):
        """停机期间带小数的最后一秒（< int(start)）必须被拦截。"""
        start = 1_700_100_000
        _, svc, agg = _svc(monitor_since=_CONFIG,
                           session_start=float(start) + 0.2)
        svc._on_message(_msg(start - 1 + 0.9))   # start-0.1 < int(start)
        assert agg.calls == []

    def test_millisecond_create_time_with_fractional_start(self):
        """毫秒归一 + 浮点启动水位叠加：归一后仍按秒级比较。"""
        start = 1_700_100_000
        _, svc, agg = _svc(monitor_since=_CONFIG,
                           session_start=float(start) + 0.5)
        svc._on_message(_msg(start * 1000))            # 同秒 → 放行
        svc._on_message(_msg((start - 1) * 1000))      # 早 1 秒 → 丢弃
        assert len(agg.calls) == 1
        assert agg.calls[0]["create_time"] == start * 1000

    def test_config_gate_strict_with_fractional_create_time(self):
        """配置水位严格 <：带小数的 create_time 早于配置秒即拦，等于放行。"""
        _, svc, agg = _svc(session_start=0.0)  # 只留配置闸门
        svc._on_message(_msg(_CONFIG - 0.5))   # < since → 丢弃
        svc._on_message(_msg(_CONFIG))         # == since → 放行
        assert len(agg.calls) == 1
        assert agg.calls[0]["create_time"] == _CONFIG

    def test_whatsapp_clock_skew_message_far_in_future_allowed(self):
        """微信时钟快于系统时钟（create_time 远大于启动）：不能误杀。"""
        _, svc, agg = _svc(session_start=float(_START))
        svc._on_message(_msg(_START + 3_600))
        assert len(agg.calls) == 1


# ===========================================================================
# 3. 双闸门独立 & 异常兜底
# ===========================================================================
class TestGateRobustness:
    """DAO 异常 / 单闸门 / 非法时间一律放行，避免误伤正常消息。"""

    def test_config_dao_exception_start_gate_still_filters(self):
        """配置水位读取抛异常时，启动闸门仍独立生效。"""
        db, svc, agg = _svc(session_start=float(_START))

        def _boom(_username):
            raise RuntimeError("session.db 锁定")

        db.get_target_by_username = _boom  # type: ignore[method-assign]
        svc._on_message(_msg(_START - 100))   # 停机消息 → 启动闸门拦
        svc._on_message(_msg(_START + 5))     # 新消息 → 放行
        assert len(agg.calls) == 1
        assert agg.calls[0]["create_time"] == _START + 5

    def test_config_dao_exception_without_start_gate_allows_all(self):
        """两道闸门都不可用时完全放行（存量/降级行为不变）。"""
        db, svc, agg = _svc(session_start=0.0)
        db.get_target_by_username = (  # type: ignore[method-assign]
            lambda _username: (_ for _ in ()).throw(RuntimeError("boom")))
        svc._on_message(_msg(1_640_000_000))   # 2022 年历史也放行
        assert len(agg.calls) == 1

    @pytest.mark.parametrize("bad_ts", [None, "", "abc", "not-a-time",
                                        0, -1, "0", -99.5])
    def test_invalid_create_time_always_allowed(self, bad_ts):
        """时间缺失/非法一律放行，双闸门都不误伤。"""
        _, svc, agg = _svc(session_start=float(_START))
        svc._on_message(_msg(bad_ts))
        assert len(agg.calls) == 1

    def test_missing_username_allowed(self):
        """无 username 的消息不做水位判断（直接放行）。"""
        _, svc, agg = _svc(session_start=float(_START))
        msg = _msg(_START - 100)
        msg["username"] = ""
        svc._on_message(msg)
        assert len(agg.calls) == 1

    def test_only_start_gate_legacy_target(self):
        """存量 target（monitor_since=0）仍受启动水位约束。"""
        _, svc, agg = _svc(monitor_since=0, session_start=float(_START))
        svc._on_message(_msg(_START - 100))
        svc._on_message(_msg(_START + 5))
        assert len(agg.calls) == 1
        assert agg.calls[0]["create_time"] == _START + 5


# ===========================================================================
# 4. watermark 推进：add_listener 真语义 / flush 持久化 / 脏值
# ===========================================================================
class _FakeWxdb:
    """按 get_messages(sort_seq DESC) 语义返回指定会话的最新一条。"""

    def __init__(self, latest: dict[str, list[dict]]) -> None:
        self.latest = latest
        self.calls: list[tuple[str, int]] = []

    def get_messages(self, user: str, limit: int = 1) -> list[dict]:
        self.calls.append((user, limit))
        return list(self.latest.get(user, []))[:limit]


class _FakeDb:
    """最小 DAO 替身：水位读写 + 已启用会话列表。"""

    def __init__(self, marks: dict | None = None,
                 users: list[str] | None = None) -> None:
        self.marks = marks if marks is not None else {
            "old@chatroom": 100, "gone@chatroom": 5}
        self.users = users if users is not None else [
            "old@chatroom", "new@chatroom"]
        self.saved: list[dict] = []

    def load_watermarks(self) -> dict:
        return dict(self.marks)

    def list_enabled_target_usernames(self) -> list[str]:
        return list(self.users)

    def save_watermarks(self, marks: dict) -> None:
        self.saved.append(dict(marks))

    def set_setting(self, _key, _value) -> None:
        pass

    def get_setting(self, _key, default=None):
        return default


class _RealisticListener:
    """严格复刻 wechatauto.db.Listener.add_listener 的水位语义。

    真实实现 line 3302-3304：``if user not in self._watermark: ...``。
    这里刻意让「兜底查询」返回一个**远大于**推进值的 sort_seq，
    一旦实现被改成无条件覆盖，用例立刻失败。
    """

    FALLBACK_SEQ = {"old@chatroom": 999_999, "new@chatroom": 888_888}

    def __init__(self, db, interval, watermark):
        self.db = db
        self.interval = interval
        self.wm = dict(watermark or {})
        self.registered: list[str] = []
        self.started = False

    def add_listener(self, user, callback):
        self.registered.append(user)
        if user not in self.wm:
            self.wm[user] = self.FALLBACK_SEQ.get(user, 0)

    def start(self):
        self.started = True

    def stop(self):
        pass

    @property
    def watermark(self):
        return dict(self.wm)


def _started_svc(monkeypatch, tmp_path, wx):
    """用真实 add_listener 语义替身完成一次 start()，返回 (svc, listener)。"""
    created: list[_RealisticListener] = []

    def _listener_factory(db, interval, watermark):
        listener = _RealisticListener(db, interval, watermark)
        created.append(listener)
        return listener

    monkeypatch.setattr(wxdbmod, "Listener", _listener_factory)
    monkeypatch.setattr(wxdbmod, "WeChatDB", lambda **kwargs: wx)
    db = _FakeDb()
    svc = WeChatService(db, None)  # type: ignore[arg-type]
    svc.configured_cache_dir = lambda: tmp_path  # type: ignore[method-assign]
    svc._adopt_legacy_keys = lambda path: None  # type: ignore[method-assign]
    svc._voice_retry.start = lambda: None  # type: ignore[method-assign]
    svc._start_vision_workers = lambda: None  # type: ignore[method-assign]
    svc._image_retry.start = lambda: None  # type: ignore[method-assign]
    svc.start()
    return svc, created[0]


class TestWatermarkAdvanceIntegration:
    """推进值必须真正落到 Listener，并被持久化；add_listener 不得覆盖。"""

    def test_add_listener_does_not_overwrite_advanced_watermark(
            self, monkeypatch, tmp_path):
        """注册监听时不会用兜底查询覆盖已推进的 sort_seq。"""
        wx = _FakeWxdb({"old@chatroom": [{"sort_seq": 5000}],
                        "new@chatroom": [{"sort_seq": 8000}]})
        svc, listener = _started_svc(monkeypatch, tmp_path, wx)
        try:
            assert listener.wm["old@chatroom"] == 5000
            assert listener.wm["new@chatroom"] == 8000
            assert listener.wm["gone@chatroom"] == 5
            assert listener.started is True
            assert listener.registered == ["old@chatroom", "new@chatroom"]
            assert svc._session_start_ts > 0
        finally:
            svc._stop.set()

    def test_flush_persists_advanced_watermark(self, monkeypatch, tmp_path):
        """30s flush 把推进后的水位（而非旧水位/0）刷回持久层。"""
        wx = _FakeWxdb({"old@chatroom": [{"sort_seq": 5000}],
                        "new@chatroom": [{"sort_seq": 8000}]})
        svc, listener = _started_svc(monkeypatch, tmp_path, wx)
        try:
            db = svc._db
            monkeypatch.setattr(wsmod, "WATERMARK_FLUSH_SEC", 0.01)
            original_save = db.save_watermarks  # 先存原方法，避免递归调用自身

            def _save_and_stop(marks):
                original_save(marks)
                svc._stop.set()  # 首轮刷回后退出循环，避免忙等

            db.save_watermarks = _save_and_stop  # type: ignore[method-assign]
            svc._flush_loop()
            assert db.saved and db.saved[-1] == listener.wm
            assert db.saved[-1]["old@chatroom"] == 5000
            assert db.saved[-1]["new@chatroom"] == 8000
        finally:
            svc._stop.set()

    @pytest.mark.parametrize("raw_seq,expected_keep_old", [
        (None, True), ("5000", True), (True, True), (False, True),
        (0.0, False),  # 合法数字 0 → 允许推进（虽然真实数据极少）
        (5000.9, False),
    ])
    def test_sort_seq_dirty_value_filtering(self, raw_seq, expected_keep_old):
        """非数字/bool/None 的 sort_seq 保留旧水位；合法数字推进。"""
        db = _FakeDb()
        wx = _FakeWxdb({"old@chatroom": [{"sort_seq": raw_seq}]})
        svc = WeChatService(db, None)  # type: ignore[arg-type]
        svc._wxdb = wx  # type: ignore[assignment]
        result = svc._advance_start_watermarks(db.marks)
        if expected_keep_old:
            assert result["old@chatroom"] == 100
        else:
            assert result["old@chatroom"] == int(raw_seq)

    def test_advance_uses_limit_one_and_does_not_mutate_input(self):
        """推进只读最新一条（limit=1），且不修改调用方传入的字典。"""
        db = _FakeDb(marks={"old@chatroom": 100}, users=["old@chatroom"])
        wx = _FakeWxdb({"old@chatroom": [{"sort_seq": 4321}]})
        svc = WeChatService(db, None)  # type: ignore[arg-type]
        svc._wxdb = wx  # type: ignore[assignment]
        original = dict(db.marks)
        result = svc._advance_start_watermarks(db.marks)
        assert result["old@chatroom"] == 4321
        assert db.marks == original
        assert wx.calls == [("old@chatroom", 1)]


# ===========================================================================
# 5. 好友口径：真实数据独立复核 + 白名单/搜索锁死
# ===========================================================================
class TestRealContactDbEvidence:
    """亲自读真实 contact.db，证明「收紧到 lt=3」会误删用户点名好友。"""

    @pytest.mark.skipif(not _REAL_CONTACT_DB.exists(),
                        reason="真实 contact.db 不存在，跳过数据复核")
    def test_two_named_friends_are_local_type_1_in_real_db(self):
        """两名用户点名的真好友确实是 lt=1、del=0，不能收紧口径。"""
        con = sqlite3.connect(f"file:{_REAL_CONTACT_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            for uid in ("wxid_3f76xuokjw8b72", "wxid_9941039410111"):
                row = con.execute(
                    "SELECT local_type, delete_flag FROM contact "
                    "WHERE username=?", (uid,)).fetchone()
                assert row is not None, f"真实库中找不到 {uid}"
                assert row["local_type"] == 1, \
                    f"{uid} 不是 lt=1，工程师结论需复核"
                assert row["delete_flag"] == 0
        finally:
            con.close()

    @pytest.mark.skipif(not _REAL_CONTACT_DB.exists(),
                        reason="真实 contact.db 不存在，跳过数据复核")
    def test_lt3_only_would_drop_named_friends(self):
        """反证：若把口径收紧成「只留 lt=3」，两名真好友会消失。"""
        con = sqlite3.connect(f"file:{_REAL_CONTACT_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            lt3 = {r[0] for r in con.execute(
                "SELECT username FROM contact WHERE local_type=3 "
                "AND delete_flag=0")}
            for uid in ("wxid_3f76xuokjw8b72", "wxid_9941039410111"):
                assert uid not in lt3, "真实数据已变，需重新评估好友口径"
        finally:
            con.close()


class _WhitelistWx:
    """按生产 SQL 口径构造的真实数据形态替身。"""

    TRUE_FRIENDS = [
        {"username": "wxid_3f76xuokjw8b72", "remark": "MiKE 淼淼英语老师",
         "nick_name": "MiKE", "local_type": 1},
        {"username": "wxid_9941039410111", "remark": "张鹏 大淼淼爸爸",
         "nick_name": "张鹏", "local_type": 1},
    ]
    LT3_FRIENDS = [
        {"username": "wxid_o12edjzelv8r22", "remark": "", "nick_name": "淼淼",
         "local_type": 3},
        {"username": "wxid_n53xa2hqwz7e21", "remark": "",
         "nick_name": "🍎田淼淼~康宝莱营养顾问", "local_type": 3},
        {"username": "wxid_ybdhzq01eiz322", "remark": "", "nick_name": "肖淼淼",
         "local_type": 3},
        {"username": "wxid_vpf9h0izg19b22", "remark": "",
         "nick_name": "悠水瑜伽淼淼", "local_type": 3},
        {"username": "wxid_0ecrf6i90u1p21", "remark": "", "nick_name": "鑫淼淼",
         "local_type": 3},
    ]
    OPENIM = [{"username": "12345@openim", "remark": "",
               "nick_name": "淼淼企业微信好友", "local_type": 5}]

    def __init__(self):
        self._all = self.TRUE_FRIENDS + self.LT3_FRIENDS

    def get_sessions(self, limit=100):
        return [{"username": u["username"]} for u in self._all] + [
            {"username": "12345@openim"},
            {"username": "wxid_nonfriend_interact"},   # 不在 contact 表
            {"username": "wxid_lt0_interact"},          # local_type=0
            {"username": "wxid_deleted_friend"},        # delete_flag=1
            {"username": "ww1688851297501162@qy_g"},    # local_type=2 假群
            {"username": "50640000722@chatroom"},       # 真群
        ]

    def get_contacts(self, kind="friend", limit=0):
        if kind == "friend":
            return [dict(r) for r in self._all]  # 真实 SQL 已排除 del=1/lt=0/2
        if kind == "openim":
            return [dict(r) for r in self.OPENIM]
        if kind == "chatroom":
            return [{"username": "999@chatroom", "nick_name": "另一个群"}]
        return []

    def is_friend(self, username):
        for row in self.OPENIM + self._all:
            if row["username"] == username:
                return row["local_type"] in (1, 3, 5, 6, 7)
        return False

    def get_openim_corp_names(self):
        return {}

    def get_self_info(self):
        return {"username": "wxid_self", "nick_name": "我", "remark": ""}

    def get_nickname(self, username):
        for row in self.OPENIM + self._all:
            if row["username"] == username:
                return row.get("remark") or row.get("nick_name") or ""
        return ""


def _wl_svc():
    svc = WeChatService(None, None)  # type: ignore[arg-type]
    svc._wxdb = _WhitelistWx()  # type: ignore[assignment]
    return svc


class TestFriendWhitelistIndependent:
    """按真实形态锁死白名单：两 lt=1 + 五 lt=3 + openim lt=5 全保留。"""

    def test_search_miaomiao_returns_all_seven_real_friends(self):
        """搜索「淼淼」返回 2 名 lt=1 + 5 名 lt=3，不含任何非好友。"""
        hits = {i["username"] for i in
                _wl_svc().search_sessions(keyword="淼淼", kind="friend")}
        expected = {
            "wxid_3f76xuokjw8b72", "wxid_9941039410111",
            "wxid_o12edjzelv8r22", "wxid_n53xa2hqwz7e21",
            "wxid_ybdhzq01eiz322", "wxid_vpf9h0izg19b22",
            "wxid_0ecrf6i90u1p21", "12345@openim",
        }
        assert hits == expected

    def test_non_friend_shapes_all_blocked(self):
        """不在表 / lt=0 / del=1 / lt=2 假群的私聊会话全部挡掉。"""
        items = {i["username"]: i for i in _wl_svc()._build_contact_list()}
        for uid in ("wxid_nonfriend_interact", "wxid_lt0_interact",
                    "wxid_deleted_friend", "ww1688851297501162@qy_g"):
            assert uid not in items, f"非好友会话漏过：{uid}"

    def test_groups_unaffected_by_friend_filter(self):
        """群聊不受好友白名单影响，仍按 group 返回。"""
        items = {i["username"]: i for i in _wl_svc()._build_contact_list()}
        assert items["50640000722@chatroom"]["kind"] == "group"
        assert items["999@chatroom"]["kind"] == "group"


if __name__ == "__main__":
    pytest.main([__file__])
