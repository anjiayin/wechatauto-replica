# -*- coding: utf-8 -*-
"""任务 #2：私聊监控候选只展示真实好友。

现场：``_build_contact_list`` 从 ``get_sessions(limit=100)`` 取近期会话时，
把所有非 @chatroom 的 username 都标成 kind="friend"——群里非好友的临时
会话、已删除的好友因此混进私聊候选，搜索「淼淼」列出一堆非好友。

本文件锁死四条契约：非好友被过滤、真好友保留、企业微信好友保留、
群聊候选与 is_self（给自己发消息）语义不受影响。
"""
from __future__ import annotations

from app.wechat_service import WeChatService


class _FakeWx:
    """WeChatDB 替身：区分「会话表出现」与「通讯录真好友」。"""

    def __init__(self, *, sessions: list[dict], friends: list[dict],
                 openim: list[dict] | None = None,
                 chatrooms: list[dict] | None = None,
                 fail_contacts: bool = False,
                 self_id: str = "wxid_self",
                 friend_check: set[str] | None = None):
        self.sessions = sessions
        self.contacts = {
            "friend": friends,
            "openim": openim or [],
            "chatroom": chatrooms or [],
        }
        self.fail_contacts = fail_contacts
        self.self_id = self_id
        self.friend_check = friend_check

    def is_friend(self, username: str) -> bool:
        """可选：模拟 WeChatDB.is_friend 的 local_type/delete_flag 复核。"""
        if self.friend_check is None:
            return True
        return username in self.friend_check

    def get_sessions(self, limit: int = 100) -> list[dict]:
        return list(self.sessions)

    def get_contacts(self, kind: str = "friend", limit: int = 100
                     ) -> list[dict]:
        if self.fail_contacts and kind in ("friend", "openim"):
            raise RuntimeError("contact.db 暂不可用")
        return list(self.contacts.get(kind, []))

    def get_openim_corp_names(self) -> dict[str, str]:
        return {}

    def get_self_info(self) -> dict:
        return {"username": self.self_id, "nick_name": "我", "remark": ""}

    def get_nickname(self, username: str) -> str:
        for rows in self.contacts.values():
            for r in rows:
                if r["username"] == username:
                    return r.get("remark") or r.get("nick_name") or ""
        return ""


def _svc(wx: _FakeWx) -> WeChatService:
    svc = WeChatService(None, None)  # type: ignore[arg-type]
    svc._wxdb = wx  # type: ignore[assignment]
    return svc


_FRIENDS = [
    {"username": "wxid_3f76xukxkwj8b72", "nick_name": "MIKE 淼淼英语老师"},
    {"username": "wxid_9941039410111", "nick_name": "张鹏 大淼淼爸爸"},
]


class TestPrivateCandidates:
    """私聊候选严格过滤非好友。"""

    def test_non_friend_session_filtered(self):
        """群里非好友的临时会话不作为私聊候选（本次反馈的核心）。"""
        wx = _FakeWx(
            sessions=[{"username": u} for u in (
                "wxid_3f76xukxkwj8b72", "wxid_9941039410111",
                "wxid_nonfriend_miaomiao", "wxid_deleted_friend")],
            friends=_FRIENDS)
        items = {i["username"]: i for i in _svc(wx)._build_contact_list()}
        assert "wxid_3f76xukxkwj8b72" in items
        assert "wxid_9941039410111" in items
        assert "wxid_nonfriend_miaomiao" not in items
        assert "wxid_deleted_friend" not in items

    def test_search_only_returns_real_friends(self):
        wx = _FakeWx(
            sessions=[{"username": u} for u in (
                "wxid_3f76xukxkwj8b72", "wxid_9941039410111",
                "wxid_nonfriend_miaomiao")],
            friends=_FRIENDS)
        hits = _svc(wx).search_sessions(keyword="淼淼", kind="friend")
        assert sorted(i["username"] for i in hits) == [
            "wxid_3f76xukxkwj8b72", "wxid_9941039410111"]

    def test_openim_friend_kept(self):
        """企业微信好友（local_type 5-7）同样可私信，保留。"""
        wx = _FakeWx(
            sessions=[{"username": "25984984314758775@openim"},
                      {"username": "wxid_not_friend"}],
            friends=[],
            openim=[{"username": "25984984314758775@openim",
                     "nick_name": "淼淼企业微信老师"}])
        items = {i["username"]: i for i in _svc(wx)._build_contact_list()}
        assert "25984984314758775@openim" in items
        assert items["25984984314758775@openim"]["kind"] == "friend"
        assert items["25984984314758775@openim"]["openim"] is True
        assert "wxid_not_friend" not in items

    def test_openim_dirty_local_type_rechecked_by_is_friend(self):
        """@openim 但 is_friend=False（如 local_type=0 脏数据）必须被过滤。

        get_contacts("openim") 只按 username 后缀选取，不校验 local_type；
        企业微信好友口径以 WeChatDB.is_friend（5/6/7 + delete_flag）为准。
        """
        wx = _FakeWx(
            sessions=[{"username": "25984984314758775@openim"},
                      {"username": "999@dirty.openim"}],
            friends=[],
            openim=[{"username": "25984984314758775@openim",
                     "nick_name": "真企业微信好友"},
                    {"username": "999@dirty.openim", "nick_name": "脏数据"}],
            friend_check={"25984984314758775@openim"})
        items = {i["username"]: i for i in _svc(wx)._build_contact_list()}
        assert "25984984314758775@openim" in items
        assert "999@dirty.openim" not in items


class TestUnaffectedSemantics:
    """群聊、is_self、降级语义不被误伤。"""

    def test_group_candidates_unchanged(self):
        """群聊候选不受好友过滤影响（含仅出现在会话表里的群）。"""
        wx = _FakeWx(
            sessions=[{"username": "50640000722@chatroom"}],
            friends=[],
            chatrooms=[{"username": "999@chatroom", "nick_name": "另一个群"}])
        items = {i["username"]: i for i in _svc(wx)._build_contact_list()}
        assert items["50640000722@chatroom"]["kind"] == "group"
        assert items["999@chatroom"]["kind"] == "group"

    def test_self_chat_kept(self):
        """「给自己发消息」会话即使不在好友表也保留，is_self 标记不变。"""
        wx = _FakeWx(
            sessions=[{"username": "wxid_self"},
                      {"username": "wxid_nonfriend"}],
            friends=[])
        items = {i["username"]: i for i in _svc(wx)._build_contact_list()}
        assert items["wxid_self"]["is_self"] is True
        assert "wxid_nonfriend" not in items

    def test_contact_db_failure_degrades_without_filtering(self):
        """好友表读不到时放行会话候选（降级不误伤，保持原行为）。"""
        wx = _FakeWx(
            sessions=[{"username": "wxid_unknown"}],
            friends=[], fail_contacts=True)
        items = {i["username"]: i for i in _svc(wx)._build_contact_list()}
        assert "wxid_unknown" in items
