# -*- coding: utf-8 -*-
"""联系人搜索：企业微信联系人与全量好友（反馈：搜不到企业微信好友）。

现场：微信联系人库中「邢不行」有两条 —— 普通好友 wxid_xxx（local_type=3）
与企业微信联系人 25984984314758775@openim（local_type=5）。添加私聊监控
时后者完全搜不到，前者也常搜不到，原因是：

1. ``_build_contact_list`` 只取 kind="friend"/"chatroom"，而 wechatauto 的
   friend 过滤器明确排除 ``@openim``/``@kefu`` → 全部企业微信联系人丢失；
2. ``get_contacts(kind="friend", limit=2000)`` 对 3 万条好友按 username
   排序后截断，绝大多数好友落在 2000 之外 → 「明明有这个人却搜不到」。

本文件锁死修复后的三条契约：企业微信联系人可见、好友全量不截断、
搜索按匹配质量排序。
"""
from __future__ import annotations

from app.schemas import SessionItemOut
from app.wechat_service import WeChatService, _is_system_account


class _FakeWx:
    """WeChatDB 替身：记录 get_contacts 的 (kind, limit) 调用参数。"""

    def __init__(self, sessions: list[dict], contacts: dict[str, list[dict]],
                 corps: dict[str, str] | None = None):
        self.sessions = sessions
        self.contacts = contacts
        self.corps = corps or {}
        self.calls: list[tuple[str, int]] = []

    def get_sessions(self, limit: int = 100) -> list[dict]:
        return list(self.sessions)

    def get_contacts(self, kind: str = "friend",
                     limit: int = 100) -> list[dict]:
        self.calls.append((kind, limit))
        return list(self.contacts.get(kind, []))

    def get_openim_corp_names(self) -> dict[str, str]:
        return dict(self.corps)

    def get_nickname(self, username: str) -> str:
        for rows in self.contacts.values():
            for r in rows:
                if r["username"] == username:
                    return r.get("remark") or r.get("nick_name") or ""
        return ""


def _svc(wx: _FakeWx) -> WeChatService:
    svc = WeChatService(None, None)  # type: ignore[arg-type]
    svc._wxdb = wx
    return svc


# 现场数据的精简还原：同名普通好友 + 企业微信联系人
_SESSIONS = [{"username": "wxid_swn51ujex4v921"},
             {"username": "brandsessionholder"},
             {"username": "filehelper"},
             {"username": "50640000722@chatroom"}]
_CONTACTS = {
    "chatroom": [{"username": "50640000722@chatroom",
                  "nick_name": "邢不行A谷量化学习营"}],
    "friend": [{"username": "wxid_swn51ujex4v921", "nick_name": "邢不行"},
               {"username": "wxid_other", "nick_name": "邢志萍"}],
    "openim": [{"username": "25984984314758775@openim", "nick_name": "邢不行"},
               {"username": "25984985906987917@openim", "nick_name": "客服"}],
}
# 企业微信联系人 → 所属企业简称（真实库由 openim_wording 表解析）
_CORPS = {"25984984314758775@openim": "磐达斯",
          "25984985906987917@openim": "中国人民财产保险"}


class TestOpenimVisible:
    """企业微信联系人必须出现在候选列表里（本次反馈的直接原因）。"""

    def test_openim_contacts_in_list(self):
        svc = _svc(_FakeWx(_SESSIONS, _CONTACTS))
        items = {i["username"]: i for i in svc._build_contact_list()}
        assert "25984984314758775@openim" in items
        hit = items["25984984314758775@openim"]
        assert hit["display_name"] == "邢不行"
        assert hit["kind"] == "friend"     # 一对一私聊，归入 friend
        assert hit["openim"] is True

    def test_openim_searchable_by_keyword(self):
        svc = _svc(_FakeWx(_SESSIONS, _CONTACTS))
        hits = svc.search_sessions(keyword="邢不行", kind="friend")
        users = [i["username"] for i in hits]
        # 同名两条都要能搜到，且都带正确的 openim 标记
        assert users == ["wxid_swn51ujex4v921", "25984984314758775@openim"]
        assert [i["openim"] for i in hits] == [False, True]

    def test_openim_id_searchable(self):
        """按企业微信 username 子串也能命中（便于日志/接口里直接查）。"""
        svc = _svc(_FakeWx(_SESSIONS, _CONTACTS))
        hits = svc.search_sessions(keyword="25984984314758775")
        assert [i["username"] for i in hits] == ["25984984314758775@openim"]

    def test_kefu_accounts_count_as_openim(self):
        """@kefu 客服号与 @openim 同样处理（is_openim 覆盖两种后缀）。"""
        assert WeChatService.is_openim("123@openim") is True
        assert WeChatService.is_openim("abc@kefu.openim") is True
        assert WeChatService.is_openim("wxid_abc") is False
        assert WeChatService.is_openim("123@chatroom") is False


class TestFullContactList:
    """好友全量读取：不再被 limit=2000 截断。"""

    def test_contacts_read_without_limit(self):
        wx = _FakeWx(_SESSIONS, _CONTACTS)
        _svc(wx)._build_contact_list()
        kinds = [c[0] for c in wx.calls]
        assert kinds == ["chatroom", "friend", "openim"]
        # limit=0 表示不限；2000 会把 3 万好友里的绝大多数截掉
        assert all(c[1] == 0 for c in wx.calls)

    def test_contact_beyond_2000_is_searchable(self):
        """排在 2000 条之外的好友同样能被搜到。"""
        friends = [{"username": "wxid_%05d" % i, "nick_name": "好友%d" % i}
                   for i in range(3000)]
        friends.append({"username": "wxid_swn51ujex4v921", "nick_name": "邢不行"})
        svc = _svc(_FakeWx([], {"chatroom": [], "friend": friends,
                                "openim": []}))
        hits = svc.search_sessions(keyword="邢不行", kind="friend")
        assert [i["username"] for i in hits] == ["wxid_swn51ujex4v921"]


class TestSystemAccountFilter:
    """微信内置系统会话不该出现在监控候选里。"""

    def test_system_accounts_excluded(self):
        svc = _svc(_FakeWx(_SESSIONS, _CONTACTS))
        users = [i["username"] for i in svc._build_contact_list()]
        assert "brandsessionholder" not in users
        assert "filehelper" not in users

    def test_is_system_account(self):
        assert _is_system_account("filehelper") is True
        assert _is_system_account("brandsessionholder") is True
        assert _is_system_account("gh_abc123") is False      # 公众号另行过滤
        assert _is_system_account("wxid_swn51ujex4v921") is False
        assert _is_system_account("25984984314758775@openim") is False
        assert _is_system_account("") is True


class TestMatchRanking:
    """搜索按匹配质量排序：完全一致 > 前缀 > 包含 > 仅账号命中。"""

    def test_match_rank(self):
        rank = WeChatService._match_rank
        assert rank("邢不行", "wxid_a", "邢不行") == 0
        assert rank("邢不行量化", "wxid_a", "邢不行") == 1
        assert rank("小邢不行", "wxid_a", "邢不行") == 2
        assert rank("张三", "wxid_邢不行", "邢不行") == 3
        assert rank("张三", "wxid_a", "邢不行") == 4

    def test_exact_match_first(self):
        """完全一致的联系人排在「名称包含关键词」的一堆人之前。

        好友基数 3 万条时，若边收集边截断，目标会被淹没在前 50 条之外。
        """
        friends = [{"username": "wxid_%03d" % i, "nick_name": "邢不行%d" % i}
                   for i in range(60)]
        friends.append({"username": "wxid_target", "nick_name": "邢不行"})
        svc = _svc(_FakeWx([], {"chatroom": [], "friend": friends,
                                "openim": []}))
        hits = svc.search_sessions(keyword="邢不行", kind="friend", limit=50)
        assert hits[0]["username"] == "wxid_target"
        assert len(hits) == 50             # 仍按 limit 截断

    def test_empty_keyword_keeps_original_order(self):
        """空关键词（默认列表）不打乱顺序：仍按会话优先 + 账号序。"""
        svc = _svc(_FakeWx(_SESSIONS, _CONTACTS))
        names = [i["username"] for i in svc.search_sessions(keyword="")]
        assert names[:2] == ["wxid_swn51ujex4v921", "50640000722@chatroom"]


class TestCorpSuffix:
    """企业微信联系人按微信端习惯显示「昵称@企业简称」。"""

    def test_display_name_gets_corp_suffix(self):
        svc = _svc(_FakeWx(_SESSIONS, _CONTACTS, _CORPS))
        items = {i["username"]: i for i in svc._build_contact_list()}
        hit = items["25984984314758775@openim"]
        assert hit["display_name"] == "邢不行@磐达斯"
        assert hit["corp"] == "磐达斯"
        assert hit["openim"] is True
        assert hit["kind"] == "friend"

    def test_no_corp_keeps_plain_name(self):
        """解析不出企业名时只显示昵称（前端用「企业微信」标签兜底）。"""
        svc = _svc(_FakeWx(_SESSIONS, _CONTACTS))
        items = {i["username"]: i for i in svc._build_contact_list()}
        assert items["25984984314758775@openim"]["display_name"] == "邢不行"
        assert items["25984984314758775@openim"]["corp"] == ""

    def test_remark_wins_over_nickname(self):
        """用户手动备注优先（微信里有备注就不再显示「昵称@企业」）。"""
        contacts = dict(_CONTACTS)
        contacts["openim"] = [{"username": "25984984314758775@openim",
                               "nick_name": "邢不行", "remark": "量化课-邢老师"}]
        svc = _svc(_FakeWx(_SESSIONS, contacts, _CORPS))
        items = {i["username"]: i for i in svc._build_contact_list()}
        hit = items["25984984314758775@openim"]
        assert hit["display_name"] == "量化课-邢老师"
        assert hit["corp"] == "磐达斯"        # 企业信息仍保留，供前端判断

    def test_with_corp_helper(self):
        f = WeChatService._with_corp
        assert f("邢不行", "磐达斯") == "邢不行@磐达斯"
        assert f("邢不行", "") == "邢不行"
        assert f("", "磐达斯") == ""
        assert f("邢不行@磐达斯", "磐达斯") == "邢不行@磐达斯"   # 不重复拼接

    def test_search_by_corp_name(self):
        """带企业名后可搜「企业简称」找到人。"""
        svc = _svc(_FakeWx(_SESSIONS, _CONTACTS, _CORPS))
        hits = svc.search_sessions(keyword="磐达斯", kind="friend")
        assert [i["username"] for i in hits] == ["25984984314758775@openim"]

    def test_corp_suffix_still_exact_match(self):
        """「邢不行@磐达斯」搜「邢不行」仍算完全匹配（与普通好友同权重）。"""
        assert WeChatService._match_rank(
            "邢不行@磐达斯", "25984984314758775@openim", "邢不行") == 0


class TestSchema:
    def test_openim_defaults_false(self):
        """老调用方不传 openim/corp 时不受影响（默认 False / 空串）。"""
        item = SessionItemOut(username="wxid_a", display_name="张三",
                              kind="friend")
        assert item.openim is False
        assert item.corp == ""
