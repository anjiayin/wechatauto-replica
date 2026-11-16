# -*- coding: utf-8 -*-
"""寇豆码 v4 三修：公众号过滤 / 首屏时间排序 / 加载优化正确性锁。

测试分两层：
1) 底层生产 SQL——用临时 SQLite（字段形态对齐真实 contact.db，含
   verify_flag）直接跑 ``WeChatDB.get_contacts/get_contact/is_friend``，
   避免替身把 SQL 口径绕过去；同时验证老库无 verify_flag 列时的保守降级。
2) 服务层——用真实形态替身 + 临时库驱动的 ``WeChatService``，锁死
   「搜黑马不出 i黑马」「首屏全量排序后再截 limit」「批次读取不逐条开库」
   「全量好友（>2000）仍可搜到」等契约。

现场实锤（kefu-system/data/wechat_cache/contact__contact.db）：
- i黑马 wxid_2965349653612：local_type=1、verify_flag=24、remark=''、
  extra_buffer 含「北京创业未来传媒技术有限公司」「<bizuserattr>」「@i黑马」。
- lt=1 + verify_flag=0：715 条（558 有 remark）= 真好友；
  lt=1 + verify_flag≠0：约 905 条，remark 全空 = 公众号，例外数 0。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from app.wechat_service import WeChatService  # noqa: E402
from wechatauto.db import WeChatDB  # noqa: E402

_SELF = "wxid_self"

# 真实数据形态的 2 位真好友（lt=1 + verify_flag=0 + 有备注）
TRUE_FRIENDS = [
    {"username": "wxid_9941039410111", "nick_name": "张鹏",
     "alias": "Xiangyanedu", "remark": "张鹏 大淼淼爸爸",
     "local_type": 1, "verify_flag": 0, "delete_flag": 0},
    {"username": "wxid_3f76xuokjw8b72", "nick_name": "MiKE",
     "alias": "uzairsultan", "remark": "MiKE 淼淼英语老师",
     "local_type": 1, "verify_flag": 0, "delete_flag": 0},
]
# 用户点名的公众号：lt=1 但 verify_flag=24（i黑马）
OFFICIAL_IHEIMA = {
    "username": "wxid_2965349653612", "nick_name": "i黑马",
    "alias": "iheima", "remark": "", "local_type": 1,
    "verify_flag": 24, "delete_flag": 0,
}
# 同桶公众号变体（verify_flag=8 等）
OFFICIAL_VF8 = {
    "username": "wxid_official_vf8", "nick_name": "某认证订阅号",
    "alias": "sub_account", "remark": "", "local_type": 1,
    "verify_flag": 8, "delete_flag": 0,
}
OPENIM_FRIENDS = [
    {"username": "11111@openim", "nick_name": "企业微信5号",
     "alias": "", "remark": "", "local_type": 5,
     "verify_flag": 0, "delete_flag": 0},
    {"username": "22222@openim", "nick_name": "企业微信6号",
     "alias": "", "remark": "", "local_type": 6,
     "verify_flag": 0, "delete_flag": 0},
    {"username": "33333@kefu.openim", "nick_name": "客服7号",
     "alias": "", "remark": "", "local_type": 7,
     "verify_flag": 0, "delete_flag": 0},
]
CHATROOMS = [
    {"username": "50640000722@chatroom", "nick_name": "测试群",
     "alias": "", "remark": "", "local_type": 2,
     "verify_flag": 0, "delete_flag": 0},
]
GROUP_NON_FRIENDS = [
    {"username": "wxid_o12edjzelv8r22", "nick_name": "淼淼",
     "alias": "", "remark": "", "local_type": 3,
     "verify_flag": 0, "delete_flag": 0},
]


def _insert_rows(conn: sqlite3.Connection, rows: list[dict],
                 with_verify_flag: bool) -> None:
    """按 get_contacts SELECT 的列顺序插入 contact 行。"""
    for r in rows:
        base = (r["username"], r.get("local_type", 0), r.get("alias", ""),
                r.get("nick_name", ""), r.get("remark", ""), "",
                r.get("big_head_url", ""), r.get("delete_flag", 0))
        if with_verify_flag:
            conn.execute(
                "INSERT INTO contact VALUES (?,?,?,?,?,?,?,?,?)",
                base + (r.get("verify_flag", 0),))
        else:
            conn.execute("INSERT INTO contact VALUES (?,?,?,?,?,?,?,?)", base)


def _make_contact_db(path: str, rows: list[dict],
                     with_verify_flag: bool = True) -> None:
    """造临时 contact.db（真实列形态，可选是否含 verify_flag 列）。"""
    conn = sqlite3.connect(path)
    if with_verify_flag:
        conn.execute(
            "CREATE TABLE contact ("
            "username TEXT, local_type INTEGER, alias TEXT, nick_name TEXT, "
            "remark TEXT, description TEXT, big_head_url TEXT, "
            "delete_flag INTEGER, verify_flag INTEGER)")
    else:
        conn.execute(
            "CREATE TABLE contact ("
            "username TEXT, local_type INTEGER, alias TEXT, nick_name TEXT, "
            "remark TEXT, description TEXT, big_head_url TEXT, "
            "delete_flag INTEGER)")
    _insert_rows(conn, rows, with_verify_flag)
    conn.commit()
    conn.close()


class _TempDbWx(WeChatDB):
    """绕过解密 __init__，指向临时 contact.db 的 WeChatDB 子类。

    生产 get_contacts/get_contact/is_friend/get_nickname/get_alias 全部
    走真实 SQL；仅会话来源改为可控列表。
    """

    def __init__(self, db_path: str, sessions: list[dict],
                 session_times: dict[str, int] | None = None) -> None:
        self.db_dir = ""
        self.account = f"{_SELF}_aaaa"
        self.account_dir = ""
        self.workdir = ""
        self._db_files: list = []
        self._qa_db_path = db_path
        self._qa_sessions = list(sessions)
        self._qa_times = dict(session_times or {})

    def _contact_conn(self):
        conn = sqlite3.connect(self._qa_db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_sessions(self, limit: int = 100) -> list[dict]:
        return list(self._qa_sessions)

    def get_session_times(self) -> dict[str, int]:
        return dict(self._qa_times)

    def get_openim_corp_names(self) -> dict[str, str]:
        return {}

    def get_self_info(self) -> dict:
        return {"username": self.wxid, "nick_name": "我", "remark": ""}


def _svc_with(wx) -> WeChatService:
    svc = WeChatService(None, None)  # type: ignore[arg-type]
    svc._wxdb = wx  # type: ignore[assignment]
    return svc


@pytest.fixture
def real_shape_db(tmp_path):
    """真实形态 contact.db：2 真好友 + i黑马 + vf8 公众号 + 企业微信 + 群 + lt=3。"""
    path = str(tmp_path / "contact.db")
    rows = (TRUE_FRIENDS + [OFFICIAL_IHEIMA, OFFICIAL_VF8]
            + OPENIM_FRIENDS + CHATROOMS + GROUP_NON_FRIENDS)
    _make_contact_db(path, rows, with_verify_flag=True)
    return path


# ===========================================================================
# 任务 #1：verify_flag 过滤公众号
# ===========================================================================
class TestVerifyFlagDBLayer:
    """生产 SQL：好友必须 local_type=1 且 verify_flag=0。"""

    def test_get_contacts_friend_excludes_verify_flag_nonzero(
            self, real_shape_db):
        db = _TempDbWx(real_shape_db, [])
        names = {c["username"] for c in db.get_contacts("friend")}
        assert names == {f["username"] for f in TRUE_FRIENDS}

    def test_get_contacts_friend_result_carries_verify_flag(
            self, real_shape_db):
        db = _TempDbWx(real_shape_db, [])
        by_name = {c["username"]: c for c in db.get_contacts("friend")}
        assert by_name["wxid_9941039410111"]["verify_flag"] == 0
        assert by_name["wxid_3f76xuokjw8b72"]["verify_flag"] == 0

    def test_get_contact_returns_verify_flag(self, real_shape_db):
        db = _TempDbWx(real_shape_db, [])
        assert db.get_contact("wxid_2965349653612")["verify_flag"] == 24
        assert db.get_contact("wxid_official_vf8")["verify_flag"] == 8
        assert db.get_contact("wxid_9941039410111")["verify_flag"] == 0

    def test_is_friend_false_for_official_true_for_friends(self, real_shape_db):
        db = _TempDbWx(real_shape_db, [])
        assert db.is_friend("wxid_2965349653612") is False   # i黑马
        assert db.is_friend("wxid_official_vf8") is False    # vf=8 公众号
        for f in TRUE_FRIENDS:
            assert db.is_friend(f["username"]) is True

    def test_openim_and_chatroom_unaffected(self, real_shape_db):
        db = _TempDbWx(real_shape_db, [])
        for o in OPENIM_FRIENDS:
            assert db.is_friend(o["username"]) is True
            assert o["username"] in {c["username"]
                                     for c in db.get_contacts("openim")}
        assert db.is_friend("50640000722@chatroom") is False
        assert "50640000722@chatroom" in {
            c["username"] for c in db.get_contacts("chatroom")}

    def test_lt3_non_friend_still_excluded(self, real_shape_db):
        db = _TempDbWx(real_shape_db, [])
        assert db.is_friend("wxid_o12edjzelv8r22") is False
        assert "wxid_o12edjzelv8r22" not in {
            c["username"] for c in db.get_contacts("friend")}

    def test_old_db_without_verify_flag_degrades_conservatively(self, tmp_path):
        """老库无 verify_flag 列：只按 local_type 判定，不误删已知好友。"""
        path = str(tmp_path / "contact_old.db")
        _make_contact_db(path, TRUE_FRIENDS, with_verify_flag=False)
        db = _TempDbWx(path, [])
        assert {c["username"] for c in db.get_contacts("friend")} == {
            f["username"] for f in TRUE_FRIENDS}
        assert db.get_contact("wxid_9941039410111")["verify_flag"] == 0
        for f in TRUE_FRIENDS:
            assert db.is_friend(f["username"]) is True

    def test_unreadable_verify_flag_does_not_drop_known_friend(
            self, real_shape_db):
        """verify_flag 值读不懂（异常字符串）时保守放行，不误删好友。"""
        conn = sqlite3.connect(real_shape_db)
        conn.execute(
            "UPDATE contact SET verify_flag='bad_value' WHERE username=?",
            ("wxid_9941039410111",))
        conn.commit()
        conn.close()
        db = _TempDbWx(real_shape_db, [])
        # SQL 比较 'bad_value'=0 为假 → 该行不进好友列表（SQL 层无法容错），
        # 但单点 is_friend 读到异常值时应保守保留。
        assert db.is_friend("wxid_9941039410111") is True

    def test_null_verify_flag_is_treated_as_friend(self, real_shape_db):
        """verify_flag=NULL 保守按好友处理，不误删（真实库中无 NULL，防御性）。"""
        conn = sqlite3.connect(real_shape_db)
        conn.execute(
            "UPDATE contact SET verify_flag=NULL WHERE username=?",
            ("wxid_9941039410111",))
        conn.commit()
        conn.close()
        db = _TempDbWx(real_shape_db, [])
        assert "wxid_9941039410111" in {
            c["username"] for c in db.get_contacts("friend")}
        assert db.is_friend("wxid_9941039410111") is True


class TestVerifyFlagServiceLayer:
    """服务层：搜索候选与私信按钮口径同步。"""

    @pytest.fixture
    def svc(self, real_shape_db):
        sessions = [{"username": r["username"]} for r in (
            TRUE_FRIENDS + [OFFICIAL_IHEIMA, OFFICIAL_VF8]
            + OPENIM_FRIENDS + CHATROOMS + GROUP_NON_FRIENDS)]
        return _svc_with(_TempDbWx(real_shape_db, sessions))

    def test_search_heima_does_not_return_official(self, svc):
        """用户现象：搜「黑马」不再出现公众号 i黑马。"""
        hits = {i["username"] for i in svc.search_sessions(keyword="黑马")}
        assert "wxid_2965349653612" not in hits
        assert hits == set()

    def test_search_iheima_alias_does_not_return_official(self, svc):
        """搜微信号 iheima 同样不返回公众号。"""
        hits = {i["username"] for i in svc.search_sessions(keyword="iheima")}
        assert "wxid_2965349653612" not in hits
        assert hits == set()

    def test_search_miaomiao_still_only_two_friends(self, svc):
        """搜「淼淼」仍只回 MIKE、张鹏 2 位 lt=1 verify_flag=0 好友。"""
        hits = {i["username"] for i in
                svc.search_sessions(keyword="淼淼", kind="friend")}
        assert hits == {f["username"] for f in TRUE_FRIENDS}

    def test_official_not_in_private_candidates(self, svc):
        items = {i["username"]: i for i in
                 svc._build_contact_list()}
        for u in ("wxid_2965349653612", "wxid_official_vf8"):
            assert u not in items, f"公众号混入私聊候选：{u}"

    def test_private_reply_button_disabled_for_official(self, svc):
        """私信回复按钮对公众号置灰（服务层 is_friend=False）。"""
        assert svc.is_friend("wxid_2965349653612") is False
        assert svc.is_friend("wxid_official_vf8") is False

    def test_private_reply_button_enabled_for_true_friends(self, svc):
        for f in TRUE_FRIENDS:
            assert svc.is_friend(f["username"]) is True
        for o in OPENIM_FRIENDS:
            assert svc.is_friend(o["username"]) is True

    def test_chatrooms_unaffected(self, svc):
        items = {i["username"]: i for i in svc._build_contact_list()}
        assert items["50640000722@chatroom"]["kind"] == "group"
        assert items["50640000722@chatroom"]["display_name"] == "测试群"


# ===========================================================================
# 任务 #2：首屏按最近有消息优先 + 时间降序（群聊/私聊一致）
# ===========================================================================
class _SortWx:
    """轻量替身：会话/联系人/时间映射完全可控。"""

    def __init__(self, sessions: list[dict], friends: list[dict],
                 chatrooms: list[dict], times: dict[str, int]):
        self.sessions = sessions
        self.contacts = {"friend": friends, "openim": [],
                         "chatroom": chatrooms}
        self.times = times

    def get_sessions(self, limit: int = 100) -> list[dict]:
        return list(self.sessions)

    def get_contacts(self, kind: str = "friend", limit: int = 0) -> list[dict]:
        return list(self.contacts.get(kind, []))

    def get_session_times(self) -> dict[str, int]:
        return dict(self.times)

    def get_openim_corp_names(self) -> dict[str, str]:
        return {}

    def get_self_info(self) -> dict:
        return {"username": _SELF, "nick_name": "我", "remark": ""}

    def get_nickname(self, username: str) -> str:
        for rows in self.contacts.values():
            for r in rows:
                if r["username"] == username:
                    return r.get("remark") or r.get("nick_name") or username
        return username

    def get_alias(self, username: str) -> str:
        for rows in self.contacts.values():
            for r in rows:
                if r["username"] == username:
                    return r.get("alias") or ""
        return ""


def _sort_fixture():
    friends = [
        {"username": "u_friend_old", "nick_name": "老好友", "alias": "",
         "remark": "老好友", "verify_flag": 0},
        {"username": "u_friend_new", "nick_name": "新好友", "alias": "",
         "remark": "新好友", "verify_flag": 0},
        {"username": "u_friend_none", "nick_name": "无消息好友", "alias": "",
         "remark": "无消息好友", "verify_flag": 0},
    ]
    chatrooms = [
        {"username": "g_old@chatroom", "nick_name": "老群", "alias": "",
         "remark": "", "verify_flag": 0},
        {"username": "g_new@chatroom", "nick_name": "新群", "alias": "",
         "remark": "", "verify_flag": 0},
        {"username": "g_none@chatroom", "nick_name": "无消息群", "alias": "",
         "remark": "", "verify_flag": 0},
    ]
    sessions = [{"username": r["username"]} for r in (chatrooms + friends)]
    times = {"g_old@chatroom": 1000, "g_new@chatroom": 9000,
             "u_friend_old": 2000, "u_friend_new": 8000}
    return _SortWx(sessions, friends, chatrooms, times)


class TestFirstScreenSorting:
    """首屏（keyword 空）：有消息在前、last_timestamp 降序、群聊私聊一致。"""

    def test_group_first_screen_sorted_by_time_desc(self):
        svc = _svc_with(_sort_fixture())
        names = [i["username"] for i in
                 svc.search_sessions(keyword="", kind="group")]
        assert names == ["g_new@chatroom", "g_old@chatroom",
                         "g_none@chatroom"]

    def test_friend_first_screen_sorted_by_time_desc(self):
        svc = _svc_with(_sort_fixture())
        names = [i["username"] for i in
                 svc.search_sessions(keyword="", kind="friend")]
        assert names == ["u_friend_new", "u_friend_old", "u_friend_none"]

    def test_all_kind_puts_messaged_sessions_before_silent(self):
        svc = _svc_with(_sort_fixture())
        names = [i["username"] for i in
                 svc.search_sessions(keyword="", kind="all")]
        assert names[:4] == ["g_new@chatroom", "u_friend_new",
                             "u_friend_old", "g_old@chatroom"]
        # 无消息组内按原缓存顺序稳定：会话表先 chatroom 后 friend。
        assert names[-2:] == ["g_none@chatroom", "u_friend_none"]

    def test_sort_before_truncate_limit(self):
        """关键：先全量排序再截 limit，不能先截断（前端 limit=50）。"""
        wx = _sort_fixture()
        # 把时间最新的会话放到缓存顺序末尾（超出 limit=2 的位置）
        wx.sessions = [{"username": "g_none@chatroom"},
                       {"username": "g_old@chatroom"},
                       {"username": "g_new@chatroom"}]
        svc = _svc_with(wx)
        names = [i["username"] for i in
                 svc.search_sessions(keyword="", kind="group", limit=2)]
        assert names == ["g_new@chatroom", "g_old@chatroom"], \
            "先截断再排序会漏掉全量里时间最靠前的会话"

    def test_no_message_group_keeps_stable_cache_order(self):
        wx = _sort_fixture()
        wx.times = {}
        wx.sessions = [{"username": "g_old@chatroom"},
                       {"username": "g_new@chatroom"},
                       {"username": "g_none@chatroom"}]
        svc = _svc_with(wx)
        names = [i["username"] for i in
                 svc.search_sessions(keyword="", kind="group")]
        assert names == ["g_old@chatroom", "g_new@chatroom",
                         "g_none@chatroom"]

    def test_keyword_keeps_match_rank_then_time_desc(self):
        """有关键词：匹配质量主排序，同组内时间降序。"""
        friends = [
            {"username": "u_exact_old", "nick_name": "淼淼", "alias": "",
             "remark": "淼淼", "verify_flag": 0},
            {"username": "u_exact_new", "nick_name": "淼淼老师", "alias": "",
             "remark": "淼淼老师", "verify_flag": 0},
            {"username": "u_partial", "nick_name": "某某淼淼", "alias": "",
             "remark": "某某淼淼", "verify_flag": 0},
        ]
        wx = _SortWx([{"username": r["username"]} for r in friends],
                     friends, [],
                     {"u_exact_old": 100, "u_exact_new": 9000,
                      "u_partial": 9900})
        svc = _svc_with(wx)
        names = [i["username"] for i in
                 svc.search_sessions(keyword="淼淼", kind="friend")]
        # 匹配质量主排序优先于时间：
        # rank0「淼淼」完全相等 → 第一；rank1「淼淼老师」前缀 → 第二；
        # rank2「某某淼淼」包含 → 最后，即使它 last_timestamp 最大。
        assert names == ["u_exact_old", "u_exact_new", "u_partial"]


# ===========================================================================
# 任务 #3：加载优化——批量替代逐条 + 全量好友不回退
# ===========================================================================
class _CountingDbWx(_TempDbWx):
    """统计 get_nickname/get_alias 调用次数的 WeChatDB（验证批量路径）。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.nickname_calls = 0
        self.alias_calls = 0

    def get_nickname(self, user: str) -> str:
        self.nickname_calls += 1
        return super().get_nickname(user)

    def get_alias(self, user: str) -> str:
        self.alias_calls += 1
        return super().get_alias(user)


class TestBatchLoadingOptimization:
    """首屏显示名/微信号从整表映射取，不逐条打开 contact.db。"""

    def test_sessions_in_contact_table_do_not_trigger_per_row_db_calls(
            self, real_shape_db):
        sessions = [{"username": r["username"]} for r in (
            TRUE_FRIENDS + OPENIM_FRIENDS + CHATROOMS)]
        wx = _CountingDbWx(real_shape_db, sessions)
        svc = _svc_with(wx)
        items = svc._build_contact_list()
        assert len(items) > 0
        assert wx.nickname_calls == 0, \
            f"仍在逐条查 nickname（{wx.nickname_calls} 次）"
        assert wx.alias_calls == 0, \
            f"仍在逐条查 alias（{wx.alias_calls} 次）"

    def test_empty_alias_contacts_do_not_trigger_per_row_db_calls(self,
                                                                  tmp_path):
        """第二路全量联系人：alias 为空也用整表行回退，不逐条开库。"""
        path = str(tmp_path / "contact_no_alias.db")
        rows = [{
            "username": f"wxid_no_alias_{i:03d}", "local_type": 1,
            "alias": "", "nick_name": f"无微信号好友{i}",
            "remark": "", "verify_flag": 0, "delete_flag": 0,
        } for i in range(100)]
        _make_contact_db(path, rows, with_verify_flag=True)
        wx = _CountingDbWx(path, [])  # 无会话：全走第二路联系人构建
        svc = _svc_with(wx)
        items = svc._build_contact_list()
        assert len(items) == 100
        assert wx.nickname_calls == 0
        assert wx.alias_calls == 0, \
            f"无 alias 好友仍逐条查库（{wx.alias_calls} 次）"
        # 回退到昵称的口径与 get_wechat_id 一致
        assert items[0]["alias"] == "无微信号好友0"

    def test_fields_still_complete_after_batch(self, real_shape_db):
        sessions = [{"username": r["username"]} for r in (
            TRUE_FRIENDS + OPENIM_FRIENDS + CHATROOMS)]
        svc = _svc_with(_CountingDbWx(real_shape_db, sessions))
        items = {i["username"]: i for i in svc._build_contact_list()}
        assert items["wxid_9941039410111"]["display_name"] == "张鹏 大淼淼爸爸"
        assert items["wxid_9941039410111"]["alias"] == "Xiangyanedu"
        assert items["50640000722@chatroom"]["alias"] == ""
        assert items["11111@openim"]["openim"] is True
        assert items["wxid_9941039410111"]["kind"] == "friend"
        assert items["50640000722@chatroom"]["kind"] == "group"

    def test_all_friends_still_searchable_beyond_old_2000_cap(self, tmp_path):
        """红线：全量好友仍可搜——造 2100 人，最后一个必须能搜到。"""
        path = str(tmp_path / "contact_many.db")
        rows: list[dict] = []
        for i in range(2100):
            rows.append({
                "username": f"wxid_bulk_{i:05d}", "local_type": 1,
                "alias": f"alias_{i:05d}", "nick_name": f"批量好友{i}",
                "remark": f"批量好友{i}", "verify_flag": 0,
                "delete_flag": 0,
            })
        rows.append({
            "username": "wxid_bulk_99999", "local_type": 1,
            "alias": "last_friend", "nick_name": "压轴好友",
            "remark": "压轴好友", "verify_flag": 0, "delete_flag": 0,
        })
        _make_contact_db(path, rows, with_verify_flag=True)
        # 会话表里没有这些好友：验证「不在会话表但在联系人表」仍可搜到。
        wx = _TempDbWx(path, [])
        svc = _svc_with(wx)
        hits = svc.search_sessions(keyword="压轴好友", kind="friend")
        assert {i["username"] for i in hits} == {"wxid_bulk_99999"}
        assert len(wx.get_contacts("friend")) == 2101

    def test_session_only_row_falls_back_without_crash(self, real_shape_db):
        """好友表读失败（降级）时，contact 表无对应行的会话走回退且不崩。"""

        def _fail_contacts(kind="friend", limit=0):
            if kind in ("friend", "openim"):
                raise RuntimeError("contact.db 暂不可用")
            return []

        sessions = [{"username": "wxid_not_in_contact"}]
        wx = _CountingDbWx(real_shape_db, sessions)
        wx.get_contacts = _fail_contacts  # type: ignore[method-assign]
        svc = _svc_with(wx)
        items = svc._build_contact_list()  # 好友表读失败 → 降级放行
        names = {i["username"] for i in items}
        assert "wxid_not_in_contact" in names
        assert wx.nickname_calls >= 1  # 确实走了回退逐条查询
