# -*- coding: utf-8 -*-
"""按真实 contact.db 数据锁死好友口径（local_type 语义修正）。

现场复核（kefu-system/data/wechat_cache/contact__contact.db，微信 4.x）：
- 含「淼淼」的 7 条记录里，用户通讯录中真正的 2 位好友都是 local_type=1：
  * wxid_9941039410111  nick=张鹏   remark=张鹏 大淼淼爸爸  alias=Xiangyanedu  flag=3
  * wxid_3f76xuokjw8b72  nick=MiKE  remark=MiKE 淼淼英语老师 alias=uzairsultan  flag=3
- 另 5 条（淼淼 / 🍎田淼淼~康宝莱营养顾问 / 肖淼淼 / 悠水瑜伽淼淼 / 鑫淼淼）
  全是 local_type=3、flag=4、alias=''、remark='' → 群里见过的非好友；
- 全表分布（delete_flag=0）：lt=1 共 1620 条（1213 有 alias、558 有 remark）；
  lt=3 共 32022 条（alias/remark 全表无一条非空、flag 全 4）；
  lt=0 共 805 条（remark 全空、flag 无好友位）；lt=5/6/7 为企业微信。

历史代码把 3 当通讯录好友、把 1 当混合桶，语义完全颠倒；本文件用真实
数据形态的替身锁死：**好友只认 local_type=1（+ 企业微信 5/6/7）**。
"""
from __future__ import annotations

from app.wechat_service import WeChatService

# 用户点名的两个真好友（真实数据里 local_type=1，alias/remark 非空）
_TRUE_FRIENDS = [
    {"username": "wxid_9941039410111", "nick_name": "张鹏",
     "alias": "Xiangyanedu", "remark": "张鹏 大淼淼爸爸",
     "local_type": 1, "delete_flag": 0},
    {"username": "wxid_3f76xuokjw8b72", "nick_name": "MiKE",
     "alias": "uzairsultan", "remark": "MiKE 淼淼英语老师",
     "local_type": 1, "delete_flag": 0},
]
# 昵称含「淼淼」的 5 个 local_type=3 群内非好友（真实数据形态）
_GROUP_NON_FRIENDS = [
    {"username": "wxid_o12edjzelv8r22", "nick_name": "淼淼",
     "alias": "", "remark": "", "local_type": 3, "delete_flag": 0},
    {"username": "wxid_n53xa2hqwz7e21", "nick_name": "🍎田淼淼~康宝莱营养顾问",
     "alias": "", "remark": "", "local_type": 3, "delete_flag": 0},
    {"username": "wxid_ybdhzq01eiz322", "nick_name": "肖淼淼",
     "alias": "", "remark": "", "local_type": 3, "delete_flag": 0},
    {"username": "wxid_vpf9h0izg19b22", "nick_name": "悠水瑜伽淼淼",
     "alias": "", "remark": "", "local_type": 3, "delete_flag": 0},
    {"username": "wxid_0ecrf6i90u1p21", "nick_name": "鑫淼淼",
     "alias": "", "remark": "", "local_type": 3, "delete_flag": 0},
]


class _RealisticWx:
    """按修正后的生产 get_contacts SQL 口径返回的 WeChatDB 替身。"""

    def __init__(self) -> None:
        self.friend_rows = list(_TRUE_FRIENDS)
        self.non_friend_rows = list(_GROUP_NON_FRIENDS)
        self.openim_rows = [
            {"username": "12345@openim", "nick_name": "企业微信好友",
             "alias": "", "remark": "", "local_type": 5, "delete_flag": 0},
        ]
        self.sessions = [
            {"username": u["username"]}
            for u in (self.friend_rows + self.non_friend_rows
                      + self.openim_rows)
        ] + [
            # 44 个非好友会话的真实形态
            {"username": "wxid_nonfriend_interact"},   # 不在 contact 表
            {"username": "newsapp"},                    # 系统号
            {"username": "wxid_deleted_friend"},        # delete_flag=1
            {"username": "ww1688851297501162@qy_g"},    # lt=2 假群
        ]

    def get_sessions(self, limit: int = 100) -> list[dict]:
        return list(self.sessions)

    def get_contacts(self, kind: str = "friend", limit: int = 0) -> list[dict]:
        if kind == "friend":
            # 修正后 SQL 只取 local_type=1（企业微信由 openim 分支取）
            return [dict(r) for r in self.friend_rows
                    if r["delete_flag"] == 0]
        if kind == "openim":
            return [dict(r) for r in self.openim_rows
                    if r["delete_flag"] == 0]
        return []

    def is_friend(self, username: str) -> bool:
        for row in self.openim_rows:
            if row["username"] == username:
                return row["delete_flag"] == 0 and row["local_type"] in (5, 6, 7)
        for row in self.friend_rows + self.non_friend_rows:
            if row["username"] == username:
                return row["delete_flag"] == 0 and row["local_type"] == 1
        return False

    def get_alias(self, username: str) -> str:
        for row in self.friend_rows + self.non_friend_rows + self.openim_rows:
            if row["username"] == username:
                return row.get("alias") or ""
        return ""

    def get_openim_corp_names(self) -> dict[str, str]:
        return {}

    def get_self_info(self) -> dict:
        return {"username": "wxid_self", "nick_name": "我", "remark": ""}

    def get_nickname(self, username: str) -> str:
        for row in self.friend_rows + self.non_friend_rows + self.openim_rows:
            if row["username"] == username:
                return row.get("remark") or row.get("nick_name") or ""
        return ""


def _svc() -> WeChatService:
    svc = WeChatService(None, None)  # type: ignore[arg-type]
    svc._wxdb = _RealisticWx()  # type: ignore[assignment]
    return svc


class TestRealDataFriendSemantics:
    """真实数据口径：只保 local_type=1 真好友，5 个 lt=3 非好友全过滤。"""

    def test_two_named_true_friends_survive(self):
        """用户点名的两人（local_type=1）必须保留。"""
        items = {i["username"]: i for i in _svc()._build_contact_list()}
        assert "wxid_3f76xuokjw8b72" in items
        assert "wxid_9941039410111" in items
        assert items["wxid_3f76xuokjw8b72"]["display_name"] == \
            "MiKE 淼淼英语老师"
        assert items["wxid_9941039410111"]["display_name"] == \
            "张鹏 大淼淼爸爸"

    def test_search_miaomiao_returns_only_two_lt1_friends(self):
        """搜「淼淼」只返回 2 位 lt=1 真好友，5 个 lt=3 非好友被过滤。"""
        hits = {i["username"] for i in
                _svc().search_sessions(keyword="淼淼", kind="friend")}
        assert hits == {
            "wxid_3f76xuokjw8b72", "wxid_9941039410111",
        }

    def test_all_five_lt3_non_friends_filtered(self):
        """5 个 local_type=3 群内非好友一个都不能进私聊候选列表。"""
        items = {i["username"]: i for i in _svc()._build_contact_list()}
        for row in _GROUP_NON_FRIENDS:
            assert row["username"] not in items, \
                f"local_type=3 非好友漏过：{row['username']}"

    def test_lt3_not_private_messageable(self):
        """私信回复按钮对 lt=3 非好友置灰。"""
        svc = _svc()
        for row in _GROUP_NON_FRIENDS:
            assert svc.is_friend(row["username"]) is False

    def test_lt1_and_openim_still_private_messageable(self):
        """真好友（lt=1）与企业微信好友（lt=5）仍可私信。"""
        svc = _svc()
        for row in _TRUE_FRIENDS:
            assert svc.is_friend(row["username"]) is True
        assert svc.is_friend("12345@openim") is True

    def test_non_friend_session_shapes_filtered(self):
        """不在 contact 表 / 系统号 / delete_flag=1 / lt=2 假群：全部挡掉。"""
        items = {i["username"]: i for i in _svc()._build_contact_list()}
        for username in ("wxid_nonfriend_interact", "newsapp",
                         "wxid_deleted_friend", "ww1688851297501162@qy_g"):
            assert username not in items, f"非好友会话漏过：{username}"

    def test_openim_friend_kept(self):
        """企业微信好友（local_type=5）继续保留。"""
        items = {i["username"]: i for i in _svc()._build_contact_list()}
        assert items["12345@openim"]["kind"] == "friend"
        assert items["12345@openim"]["openim"] is True

    def test_alias_exposed_for_true_friends(self):
        """会话候选带 alias 字段（界面用它替换 wxid 显示）。"""
        items = {i["username"]: i for i in _svc()._build_contact_list()}
        assert items["wxid_3f76xuokjw8b72"]["alias"] == "uzairsultan"
        assert items["wxid_9941039410111"]["alias"] == "Xiangyanedu"
