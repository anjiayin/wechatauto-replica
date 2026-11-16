# -*- coding: utf-8 -*-
"""底层口径测试：get_contacts("friend") 的 SQL 与 is_friend 对 local_type 的判定。

用临时 SQLite（字段形态对齐真实 contact.db）直接跑生产 SQL，避免替身把
SQL 口径绕过去。真实数据证据见 test_friend_filter_realdata_regression.py。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from wechatauto.db import WeChatDB  # noqa: E402

_SELF = "wxid_self"


def _make_db(path: str) -> WeChatDB:
    """造一个只读临时 contact 库的 WeChatDB（不碰真实微信缓存）。"""
    db = object.__new__(WeChatDB)
    # wxid 是由 account 去掉末尾 _<4位> 得到的 property（见 WeChatDB.wxid）
    db.account = f"{_SELF}_aaaa"
    db.get_self_info = lambda: {"username": _SELF}  # type: ignore[method-assign]

    def _conn() -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    db._contact_conn = _conn  # type: ignore[method-assign]
    return db


@pytest.fixture
def contact_db(tmp_path):
    """7 个含「淼淼」真实记录 + 企业微信 + 系统号 + 边界类型。"""
    path = str(tmp_path / "contact.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE contact ("
        "username TEXT, local_type INTEGER, alias TEXT, nick_name TEXT, "
        "remark TEXT, description TEXT, big_head_url TEXT, delete_flag INTEGER)"
    )
    rows = [
        # 用户通讯录里真正的 2 位好友：local_type=1
        ("wxid_9941039410111", 1, "Xiangyanedu", "张鹏", "张鹏 大淼淼爸爸", "", "", 0),
        ("wxid_3f76xuokjw8b72", 1, "uzairsultan", "MiKE", "MiKE 淼淼英语老师", "", "", 0),
        # 5 个群里见过的非好友：local_type=3，alias/remark 全空
        ("wxid_o12edjzelv8r22", 3, "", "淼淼", "", "", "", 0),
        ("wxid_n53xa2hqwz7e21", 3, "", "🍎田淼淼~康宝莱营养顾问", "", "", "", 0),
        ("wxid_ybdhzq01eiz322", 3, "", "肖淼淼", "", "", "", 0),
        ("wxid_vpf9h0izg19b22", 3, "", "悠水瑜伽淼淼", "", "", "", 0),
        ("wxid_0ecrf6i90u1p21", 3, "", "鑫淼淼", "", "", "", 0),
        # 企业微信 5/6/7
        ("12345@openim", 5, "", "企业微信好友", "", "", "", 0),
        ("23456@openim", 6, "", "企业微信好友2", "", "", "", 0),
        ("34567@kefu.openim", 7, "", "客服", "", "", "", 0),
        # 边界类型：0 非好友交互 / 2 群 / 4 公众号 / 1 里的 gh_ 与系统号 / 已删
        ("wxid_lt0", 0, "some_alias", "单向好友", "", "", "", 0),
        ("123@chatroom", 2, "", "某群", "", "", "", 0),
        ("gh_abc", 4, "gh_alias", "公众号", "", "", "", 0),
        ("gh_in_lt1", 1, "wxzhifu", "微信支付", "", "", "", 0),
        ("newsapp", 1, "", "系统号", "", "", "", 0),
        ("wxid_deleted", 1, "deleted_alias", "已删好友", "", "", "", 1),
    ]
    conn.executemany("INSERT INTO contact VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


class TestGetContactsSQL:
    """生产 SQL 只把 local_type=1 的人类账号当好友。"""

    def test_friend_excludes_lt3_and_keeps_lt1(self, contact_db):
        db = _make_db(contact_db)
        usernames = {c["username"] for c in db.get_contacts("friend")}
        assert "wxid_9941039410111" in usernames
        assert "wxid_3f76xuokjw8b72" in usernames
        # 5 个 local_type=3 群内非好友全部排除
        for u in ("wxid_o12edjzelv8r22", "wxid_n53xa2hqwz7e21",
                  "wxid_ybdhzq01eiz322", "wxid_vpf9h0izg19b22",
                  "wxid_0ecrf6i90u1p21"):
            assert u not in usernames, f"lt=3 非好友漏过：{u}"

    def test_friend_excludes_system_chatroom_official_openim_deleted(
            self, contact_db):
        db = _make_db(contact_db)
        usernames = {c["username"] for c in db.get_contacts("friend")}
        for u in ("12345@openim", "23456@openim", "34567@kefu.openim",
                  "wxid_lt0", "123@chatroom", "gh_abc", "gh_in_lt1",
                  "newsapp", "wxid_deleted"):
            assert u not in usernames, f"非好友/异常账号漏过：{u}"

    def test_openim_kind_still_returns_enterprise_contacts(self, contact_db):
        db = _make_db(contact_db)
        usernames = {c["username"] for c in db.get_contacts("openim")}
        assert usernames == {"12345@openim", "23456@openim",
                             "34567@kefu.openim"}

    def test_contact_dict_carries_alias(self, contact_db):
        """get_contacts 返回 dict 带 alias，供前端微信号显示透出。"""
        db = _make_db(contact_db)
        by_name = {c["username"]: c for c in db.get_contacts("friend")}
        assert by_name["wxid_9941039410111"]["alias"] == "Xiangyanedu"
        assert by_name["wxid_3f76xuokjw8b72"]["alias"] == "uzairsultan"


class TestIsFriendSQL:
    """is_friend 与 get_contacts("friend") 口径一致。"""

    def test_lt1_true_lt3_false(self, contact_db):
        db = _make_db(contact_db)
        assert db.is_friend("wxid_9941039410111") is True
        assert db.is_friend("wxid_o12edjzelv8r22") is False

    def test_lt0_and_missing_false(self, contact_db):
        db = _make_db(contact_db)
        assert db.is_friend("wxid_lt0") is False
        assert db.is_friend("wxid_not_in_db") is False

    def test_openim_true(self, contact_db):
        db = _make_db(contact_db)
        for u in ("12345@openim", "23456@openim", "34567@kefu.openim"):
            assert db.is_friend(u) is True

    def test_deleted_false(self, contact_db):
        db = _make_db(contact_db)
        assert db.is_friend("wxid_deleted") is False


class TestGetAliasSQL:
    """get_alias 只读 contact.alias，读不到返回空串。"""

    def test_returns_alias(self, contact_db):
        db = _make_db(contact_db)
        assert db.get_alias("wxid_9941039410111") == "Xiangyanedu"

    def test_empty_alias_returns_empty(self, contact_db):
        db = _make_db(contact_db)
        assert db.get_alias("wxid_o12edjzelv8r22") == ""

    def test_missing_returns_empty(self, contact_db):
        db = _make_db(contact_db)
        assert db.get_alias("wxid_not_in_db") == ""
