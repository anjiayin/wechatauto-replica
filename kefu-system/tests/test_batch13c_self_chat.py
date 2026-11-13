# -*- coding: utf-8 -*-
"""「给自己发消息」收不到（2026-11-13 实机反馈）。

现场：用户给自己的微信（昵称「安佳胤」）发消息，PC 端和手机端都发了，
分析日志里一条都没有。

只读探查结论（微信 4.x）：

- 「给自己发消息」是一个**普通会话**，username 就是当前登录账号的 wxid
  （本机 ``wxid_hu7azjb674f721``），不是文件传输助手；
- 该会话里的消息**确实落库**（local_id 1~5，含文本与语音），
  ``get_new_messages`` 也能按水位读出来；
- 但系统只轮询「监控对象」里注册过的会话，而它从未被加入监控列表 →
  一条都不会被读到（分析日志自然为空）。

本文件锁死三件事：
1. 会话候选里能认出「我自己」（``is_self``），不会和同名好友混淆；
2. 「自己 ↔ 自己」的会话（self 会话 / 文件传输助手）不受「过滤自己
   发送的消息」影响——否则开关默认开启时这条通道整体失效；
3. 加入监控时显示名补「（我自己）」，右侧列表能区分。
"""
from __future__ import annotations

from app.aggregator import Aggregator
from app.api.router_targets import create_target
from app.config import KEY_ANALYSIS_FILTER_SELF
from app.db import Database
from app.schemas import MonitorTargetCreate, SessionItemOut
from app.wechat_service import WeChatService

SELF = "wxid_hu7azjb674f721"


class _FakeWx:
    """WeChatDB 替身：只需要「会话 / 联系人 / 登录账号 / 昵称」。"""

    def __init__(self, self_wxid: str = SELF,
                 sessions: list[str] | None = None,
                 contacts: dict[str, list[dict]] | None = None,
                 names: dict[str, str] | None = None) -> None:
        self.self_wxid = self_wxid
        self.sessions = sessions or []
        self.contacts = contacts or {}
        self.names = names or {}

    def get_sessions(self, limit: int = 100) -> list[dict]:
        return [{"username": u} for u in self.sessions]

    def get_contacts(self, kind: str = "friend",
                     limit: int = 0) -> list[dict]:
        return list(self.contacts.get(kind, []))

    def get_openim_corp_names(self) -> dict[str, str]:
        return {}

    def get_nickname(self, username: str) -> str:
        return self.names.get(username, "")

    def get_self_info(self) -> dict:
        return {"username": self.self_wxid,
                "nick_name": self.names.get(self.self_wxid, "安佳胤")}


def _svc(wx: _FakeWx | None = None) -> tuple[WeChatService, Database]:
    db = Database(":memory:")
    svc = WeChatService(db, Aggregator(lambda u: None))
    if wx is not None:
        svc._wxdb = wx
    return svc, db


def _msg(content: str, *, username: str = SELF, sender_id: int = 2) -> dict:
    return {"local_id": 1, "type": "文本", "sender_id": sender_id,
            "sender_username": "", "create_time": 1794571099,
            "content": content, "sort_seq": 1, "username": username}


class TestSelfSessionInCandidates:
    """候选列表：自己的会话要能认出来。"""

    def _wx(self) -> _FakeWx:
        return _FakeWx(
            sessions=[SELF, "filehelper", "wxid_friend"],
            contacts={"friend": [{"username": "wxid_friend",
                                  "nick_name": "安佳胤"}]},
            names={SELF: "安佳胤", "wxid_friend": "安佳胤"})

    def test_self_session_flagged(self):
        svc, _db = _svc(self._wx())
        items = {i["username"]: i for i in svc._build_contact_list()}
        assert items[SELF]["is_self"] is True
        assert items[SELF]["display_name"] == "安佳胤"
        assert items["wxid_friend"]["is_self"] is False

    def test_search_finds_self_and_friend_separately(self):
        svc, _db = _svc(self._wx())
        hits = svc.search_sessions("安佳胤")
        by_user = {h["username"]: h for h in hits}
        assert by_user[SELF]["is_self"] is True
        assert by_user["wxid_friend"]["is_self"] is False

    def test_schema_accepts_is_self(self):
        out = SessionItemOut(username=SELF, display_name="安佳胤",
                             kind="friend", is_self=True)
        assert out.is_self is True
        # 未传时默认 False（旧前端/旧数据不受影响）
        assert SessionItemOut(username="wxid_x", display_name="x",
                              kind="friend").is_self is False

    def test_no_self_info_degrades_to_false(self):
        """读不到登录账号（微信未登录/降级）时不能误标。"""
        class _NoSelf(_FakeWx):
            def get_self_info(self) -> dict:
                raise RuntimeError("库不可用")

        svc, _db = _svc(_NoSelf(sessions=[SELF], names={SELF: "安佳胤"}))
        assert all(not i["is_self"] for i in svc._build_contact_list())


class TestSelfChatNotFiltered:
    """「自己 ↔ 自己」会话不受「过滤自己发送的消息」影响。"""

    def _svc_on(self, self_wxid: str = SELF):
        svc, db = _svc(_FakeWx(self_wxid=self_wxid,
                               names={self_wxid: "安佳胤"}))
        db.set_setting(KEY_ANALYSIS_FILTER_SELF, "1")   # 默认值：开启过滤
        return svc, db

    def test_self_chat_helper(self):
        svc, _db = self._svc_on()
        assert svc._is_self_chat(SELF) is True
        assert svc._is_self_chat("filehelper") is True
        assert svc._is_self_chat("wxid_friend") is False
        assert svc._is_self_chat("111@chatroom") is False
        assert svc._is_self_chat("") is False

    def test_self_message_still_analyzed(self):
        svc, db = self._svc_on()
        svc._on_message(_msg("我要机油"))
        _total, skipped = db.list_analysis_logs(status="skipped")
        assert skipped == []
        assert svc._aggregator.pending_count() == 1

    def test_filehelper_message_still_analyzed(self):
        svc, db = self._svc_on()
        svc._on_message(_msg("我要机油", username="filehelper"))
        _total, skipped = db.list_analysis_logs(status="skipped")
        assert skipped == []
        assert svc._aggregator.pending_count() == 1

    def test_self_message_in_friend_chat_still_filtered(self):
        """回归保护：**别人**会话里我发的消息（sender_id=2）仍要被过滤。"""
        svc, db = self._svc_on()
        svc._on_message(_msg("我方报价 1200", username="wxid_friend"))
        _total, skipped = db.list_analysis_logs(status="skipped")
        assert len(skipped) == 1
        assert "自己发送的消息已过滤" in skipped[0]["error"]
        assert svc._aggregator.pending_count() == 0


class TestAddSelfTarget:
    """加入监控时显示名补「（我自己）」，避免与同名好友混淆。"""

    def test_create_target_marks_self(self):
        svc, db = _svc(_FakeWx(names={SELF: "安佳胤"}))
        target = create_target(
            MonitorTargetCreate(username=SELF, display_name="安佳胤"),
            db=db, svc=svc)
        assert target.display_name == "安佳胤（我自己）"
        assert target.kind == "friend"
        assert db.list_enabled_target_usernames() == [SELF]

    def test_create_target_other_unchanged(self):
        svc, db = _svc(_FakeWx(names={SELF: "安佳胤"}))
        target = create_target(
            MonitorTargetCreate(username="wxid_friend",
                                display_name="安佳胤"),
            db=db, svc=svc)
        assert target.display_name == "安佳胤"
