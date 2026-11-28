# -*- coding: utf-8 -*-
"""商机上下文消息发送人显示口径回归（企业微信「昵称@企业简称」）。

真因（D:/wechatauto_cache 只读探查）：
  企业微信私聊会话 25984982152595070@openim（安佳胤@我的车车）里，对方消息
  的 real_sender_id=1900 经 SenderName2Id 映射到 25984983893057228@openim，
  该账号 nick_name 是「杜飞」；而商机列表/详情头部按**会话 username** 展示，
  所以只有上下文框冒出「杜飞」。抽样 15 个 @openim / @kefu.openim 会话，
  来消息的 sender_username 全部是与会话对端不同的内部工作号（wxid_* 等）。

本测试全部使用受控桩，不读写真实微信/商机数据库。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from app.wechat_service import WeChatService  # noqa: E402

# 用户截图里的企业微信会话与内部工作号（真机数据形态）
OPENIM = "25984982152595070@openim"
OPENIM_INNER = "25984983893057228@openim"
FRIEND = "wxid_friend_001"
FRIEND_OTHER = "wxid_other_002"
GROUP = "21264829018@chatroom"
GROUP_MEMBER = "wxid_group_member"


class StubDb:
    """只提供上下文读取路径用到的方法；不碰真实磁盘。"""

    def get_int(self, key: str, default: int = 0) -> int:
        return default

    def get_setting(self, key: str, default: str = "") -> str:
        return default

    def get_voice_texts(self, username: str, local_ids: list[int]) -> dict:
        return {}

    def get_group_cards(self, chatroom: str) -> dict:
        return {}


class StubWxDb:
    """微信库替身：昵称、企业简称、消息、群成员均可控。"""

    def __init__(self, messages: list[dict], names: dict[str, str],
                 corp: dict[str, str] | None = None,
                 corp_fail: bool = False) -> None:
        self.messages = messages
        self.names = names
        self.corp = corp or {}
        self.corp_fail = corp_fail
        self.corp_calls = 0

    def get_messages(self, username: str, limit: int = 50) -> list[dict]:
        return [dict(m) for m in self.messages[:limit]]

    def get_nickname(self, username: str) -> str:
        return self.names.get(username, "")

    def get_openim_corp_names(self) -> dict:
        self.corp_calls += 1
        if self.corp_fail:
            raise RuntimeError("contact.db 正忙（模拟）")
        return dict(self.corp)

    def get_group_members(self, chatroom: str) -> list[dict]:
        return [{"username": GROUP_MEMBER, "nick_name": "渔",
                 "remark": "", "is_owner": False}]


def make_service(wxdb: StubWxDb) -> WeChatService:
    svc = WeChatService(StubDb(), None)  # type: ignore[arg-type]
    svc._wxdb = wxdb  # type: ignore[assignment]
    return svc


def make_message(local_id: int, mtype: str = "文本",
                 sender_id: int = 1900, sender: str = OPENIM_INNER,
                 content: str = "有谁要发动机吗？") -> dict:
    return {
        "local_id": local_id,
        "type": mtype,
        "sender_id": sender_id,
        "sender_username": sender,
        "content": content,
        "create_time": time.time(),
        "sort_seq": local_id,
    }


def test_openim_context_uses_session_display_with_corp():
    """企业微信私聊上下文：11 条消息（含语音/图片）都显示「昵称@企业简称」。"""
    msgs = [make_message(i + 1, mtype=(
        "文本" if i % 3 == 0 else "语音" if i % 3 == 1 else "图片"))
        for i in range(11)]
    wxdb = StubWxDb(
        msgs,
        names={OPENIM: "安佳胤", OPENIM_INNER: "杜飞"},
        corp={OPENIM: "我的车车"},
    )
    svc = make_service(wxdb)
    out, error = svc.get_messages_with_error(OPENIM, limit=11)
    assert error == ""
    assert len(out) == 11
    assert {m["sender_name"] for m in out} == {"安佳胤@我的车车"}
    assert all("杜飞" not in m["sender_name"] for m in out)


def test_openim_corp_read_failure_degrades_to_nickname():
    """企业简称读失败：降级为会话昵称，不抛异常、不显示内部工作号昵称。"""
    wxdb = StubWxDb(
        [make_message(1)],
        names={OPENIM: "安佳胤", OPENIM_INNER: "杜飞"},
        corp_fail=True,
    )
    svc = make_service(wxdb)
    out, error = svc.get_messages_with_error(OPENIM, limit=5)
    assert error == ""
    assert out[0]["sender_name"] == "安佳胤"


def test_openim_self_message_still_me():
    """sender_id=2 的自己消息仍显示「我」，且早于企业名解析。"""
    wxdb = StubWxDb(
        [make_message(1, sender_id=2, sender="", content="333")],
        names={OPENIM: "安佳胤"}, corp={OPENIM: "我的车车"},
    )
    svc = make_service(wxdb)
    out, error = svc.get_messages_with_error(OPENIM, limit=5)
    assert error == ""
    assert out[0]["sender_name"] == "我"
    assert wxdb.corp_calls == 0


# ---------------------------------------------------------------------------
# 普通好友私聊（多微信切换后 sender_username 映射错位，2026-11-28 反馈）
# ---------------------------------------------------------------------------
# 真机样本：会话 wxid_hu7azjb674f721（安佳胤）里对方消息 sender_id=64，
# 行内按 message_resource.SenderName2Id 映射成 wxid_3akl98wezb6922
# （「A梵听 جينغ」），而该消息所在 message_0.db 自己的 Name2Id 里
# id=64 正是 wxid_hu7azjb674f721（安佳胤）。
WRONG_FRIEND = "wxid_3akl98wezb6922"


def test_private_friend_context_uses_session_partner():
    """普通好友私聊上下文：发送方显示会话对端，不用行内错位映射。"""
    msgs = [
        make_message(1, sender_id=64, sender=WRONG_FRIEND,
                     content="我要一块车窗玻璃"),
        make_message(2, sender_id=2, sender="", content="好的"),
    ]
    wxdb = StubWxDb(msgs, names={FRIEND: "安佳胤",
                                 WRONG_FRIEND: "A梵听 جينغ"})
    svc = make_service(wxdb)
    out, error = svc.get_messages_with_error(FRIEND, limit=5)
    assert error == ""
    assert [m["sender_name"] for m in out] == ["安佳胤", "我"]


def test_private_chat_ignores_row_sender_mapping():
    """_resolve_row_sender 私聊分支按会话 username 解析，不信任行内 ID。"""
    svc = make_service(StubWxDb([], names={FRIEND: "安佳胤",
                                            WRONG_FRIEND: "A梵听 جينغ"}))
    name, clean = svc._resolve_row_sender(
        FRIEND, "我要一块车窗玻璃", WRONG_FRIEND, 64)
    assert (name, clean) == ("安佳胤", "我要一块车窗玻璃")


def test_friend_private_uses_session_partner_without_corp():
    """普通好友私聊：只按会话 username 解析，不拼企业名。

    2026-11-28 用户反馈：多微信切换后行内 sender_username 映射错位
    （上下文显示「A梵听 جينغ」而非「安佳胤」）。私聊发送人只可能是
    「我」或会话对端，与实时监听口径一致（旧断言「按行内 ID 解析」
    正是本次要修掉的行为）。
    """
    wxdb = StubWxDb(
        [make_message(1, sender=FRIEND_OTHER, content="你好")],
        names={FRIEND: "会话昵称", FRIEND_OTHER: "张三"},
        corp={FRIEND: "误加的企业名", FRIEND_OTHER: "误加的企业名"},
    )
    svc = make_service(wxdb)
    out, error = svc.get_messages_with_error(FRIEND, limit=5)
    assert error == ""
    assert out[0]["sender_name"] == "会话昵称"
    assert wxdb.corp_calls == 0


def test_group_context_unchanged():
    """群聊：正文前缀解析群昵称并剥前缀，行为保持原样。"""
    wxdb = StubWxDb(
        [make_message(1, sender="wxid_wrong_map",
                      content=f"{GROUP_MEMBER}:\n我要一箱机油")],
        names={},
    )
    svc = make_service(wxdb)
    out, error = svc.get_messages_with_error(GROUP, limit=5)
    assert error == ""
    assert out[0]["sender_name"] == "渔"
    assert out[0]["content"] == "我要一箱机油"
    assert wxdb.corp_calls == 0


def test_corp_map_read_once_per_context_batch():
    """企业简称表整批只读一次（600s 进程内缓存不因逐条解析被打破）。"""
    wxdb = StubWxDb(
        [make_message(i + 1) for i in range(11)],
        names={OPENIM: "安佳胤", OPENIM_INNER: "杜飞"},
        corp={OPENIM: "我的车车"},
    )
    svc = make_service(wxdb)
    out, _error = svc.get_messages_with_error(OPENIM, limit=11)
    assert len(out) == 11
    assert wxdb.corp_calls == 1
