# -*- coding: utf-8 -*-
"""第 15 批：群商机「私信回复」的好友判定（local_type 语义修正后）。

现场需求：群商机详情页要区分「群内回复」和「私信回复」，只有通讯录真好友
（或企业微信联系人）能私信；群里见过但没加好友的人点开聊天框也发不出去，
前端按钮必须置灰。

微信 4.x 本机 contact.db 实锤（delete_flag=0）：
- local_type=1：1620 条，1213 有微信号、558 有备注 → **通讯录真好友**；
- local_type=3：32022 条，alias/remark 全表皆空、flag=4 → **群里见过的
  非好友**，不可私信（搜「淼淼」命中的 5 个额外账号全是这类）；
- local_type=0：805 条，remark 全空 → 非好友交互/单向，不可私信；
- local_type=5/6/7：企业微信联系人，可私信。

本文件锁定两条规则：
1. ``WeChatDB.is_friend`` 的 local_type 判定表（口径与 ``get_contacts`` 的
   ``kind="friend"`` 一致：1=好友、3/0=非好友、5-7=企业微信）；
2. ``WeChatService.is_friend`` 的降级行为——微信库不可用时返回 False，
   宁可置灰也不要给一个点了会失败的按钮。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from wechatauto.db import WeChatDB          # noqa: E402  (需先补 sys.path)
from app.wechat_service import WeChatService  # noqa: E402


def _db(rows: dict, wxid: str = "wxid_self") -> WeChatDB:
    """构造一个只用于判定好友的空 WeChatDB（不碰任何真实微信库）。

    ``wxid`` 是属性（从目录名 ``<账号>_<4位哈希>`` 里剥出），所以这里直接
    摆 ``account``，末尾那个 ``_aaaa`` 会被正则剥掉。
    """
    db = object.__new__(WeChatDB)
    db.account = f"{wxid}_aaaa"
    db.get_contact = lambda u: rows.get(u)   # type: ignore[method-assign]
    return db


def _row(username: str, local_type: int, delete_flag: int = 0) -> dict:
    return {"username": username, "nick_name": username, "remark": "",
            "local_type": local_type, "delete_flag": delete_flag}


def test_contacts_friend_lt1_is_private_messageable():
    """通讯录真好友（local_type=1，有微信号/备注）可以私信。"""
    db = _db({"wxid_a": _row("wxid_a", 1)})
    assert db.is_friend("wxid_a") is True


def test_group_interactor_lt3_is_not_private_messageable():
    """群里见过但非好友（local_type=3）必须置灰，不能承诺可私信。

    这是本次口径修正的主因：旧代码把 lt=3 当好友，群商机里给陌生人开的
    私信按钮点了必失败。真实数据里 lt=3 的 32022 条 alias/remark 全空。
    """
    db = _db({"wxid_group_stranger": _row("wxid_group_stranger", 3)})
    assert db.is_friend("wxid_group_stranger") is False


def test_lt0_interactor_is_not_friend():
    """local_type=0（非好友交互/单向，805 条 remark 全空）不可私信。"""
    db = _db({"wxid_b": _row("wxid_b", 0)})
    assert db.is_friend("wxid_b") is False


def test_openim_contact_counts_as_friend():
    """企业微信联系人（local_type=5）也能私信（发送链路支持 @openim）。"""
    db = _db({"abc@openim": _row("abc@openim", 5)})
    assert db.is_friend("abc@openim") is True


def test_all_openim_types_count_as_friend():
    """企业微信 local_type=5/6/7 三种都应可私信。"""
    for lt in (5, 6, 7):
        db = _db({f"abc{lt}@kefu.openim": _row(f"abc{lt}@kefu.openim", lt)})
        assert db.is_friend(f"abc{lt}@kefu.openim") is True


def test_missing_contact_is_not_friend():
    db = _db({})
    assert db.is_friend("wxid_unknown") is False


def test_deleted_contact_is_not_friend():
    """删掉的好友（delete_flag=1）不能在界面上承诺可私信。"""
    db = _db({"wxid_a": _row("wxid_a", 1, delete_flag=1)})
    assert db.is_friend("wxid_a") is False


def test_chatroom_and_official_are_not_friend():
    db = _db({
        "111@chatroom": _row("111@chatroom", 2),
        "gh_abc": _row("gh_abc", 1),
        "app_x@app": _row("app_x@app", 1),
    })
    assert db.is_friend("111@chatroom") is False
    assert db.is_friend("gh_abc") is False
    assert db.is_friend("app_x@app") is False


def test_self_is_not_friend():
    db = _db({"wxid_self": _row("wxid_self", 1)})
    assert db.is_friend("wxid_self") is False


def test_service_is_friend_false_without_wechat_db():
    """微信库未就绪（降级）：一律 False，前端按钮置灰而不是点下去报错。"""
    svc = object.__new__(WeChatService)
    svc._wxdb = None
    svc._self_wxid = "wxid_self"
    assert svc.is_friend("wxid_a") is False


def test_service_is_friend_delegates_and_excludes_self():
    class FakeWx:
        @staticmethod
        def is_friend(username: str) -> bool:
            return username in ("wxid_a", "wxid_self")

    svc = object.__new__(WeChatService)
    svc._wxdb = FakeWx()
    svc._self_wxid = "wxid_self"
    assert svc.is_friend("wxid_a") is True
    assert svc.is_friend("wxid_self") is False   # 自己不算
    assert svc.is_friend("wxid_other") is False


def test_service_is_friend_false_on_read_error():
    class BrokenWx:
        @staticmethod
        def is_friend(username: str) -> bool:
            raise RuntimeError("contact.db 正被微信写入")

    svc = object.__new__(WeChatService)
    svc._wxdb = BrokenWx()
    svc._self_wxid = "wxid_self"
    assert svc.is_friend("wxid_a") is False
