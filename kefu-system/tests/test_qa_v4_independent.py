# -*- coding: utf-8 -*-
"""QA v4 独立回归验证（第二层质量关卡）。

与工程师自测（test_kou_v4_fixes.py）刻意采用**不同角度**：
1. 直接读仓库内的**真实 contact.db / kefu.sqlite3**（只读），独立复核
   verify_flag 分组、i黑马公众号特征、905 条 vf!=0 是否会误伤真好友/
   监控目标——不依赖工程师写的合成替身；
2. 首屏排序用 65/60 条规模构造，验证**全局 top-N 截断**（不是 3 条的玩具例）；
3. 批量优化用「禁止逐条查询」的严格替身和字段矩阵，验证口径与回退路径；
4. 商机私信/回复 target、监控目标链路回归。

铁律：只读真实库，不写业务库、不 commit、不碰 UIA/版本文件。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest
from fastapi import HTTPException

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, os.path.join(_ROOT, "kefu-system")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.api.router_opportunities import _dm_target  # noqa: E402
from app.wechat_service import WeChatService  # noqa: E402
from wechatauto.db import WeChatDB  # noqa: E402

_REAL_CONTACT = os.path.join(
    _ROOT, "kefu-system", "data", "wechat_cache", "contact__contact.db")
_REAL_BIZ = os.path.join(_ROOT, "kefu-system", "data", "kefu.sqlite3")

_SELF = "wxid_qa_self"
_IHEIMA = "wxid_2965349653612"
_MIKE = "wxid_3f76xuokjw8b72"
_ZHANG = "wxid_9941039410111"
# 真实库 lt=1 + vf!=0 且 username 非 gh_ 的 15 条，逐条核对全是公众号/官方号
_KNOWN_OFFICIAL_NICKS = {
    "i黑马", "财付通", "中国南方航空", "QQ安全中心", "腾讯大讲堂",
    "该账号已注销", "微信公众平台", "腾讯企业邮箱", "麦当劳",
    "公众平台安全助手", "月光博客", "腾讯科技", "招商银行信用卡",
    "微信团队", "",
}


# ---------------------------------------------------------------------------
# 通用构造工具
# ---------------------------------------------------------------------------
def _row(username: str, nick: str = "", alias: str = "", remark: str = "",
         local_type: int = 1, vf: int = 0,
         delete_flag: int = 0) -> dict:
    """造一条与 get_contacts 返回形态一致的联系人行。"""
    return {
        "username": username, "alias": alias, "nick_name": nick,
        "remark": remark, "description": "", "big_head_url": "",
        "local_type": local_type, "verify_flag": vf,
        "delete_flag": delete_flag,
    }


def _svc(wx) -> WeChatService:
    svc = WeChatService(None, None)  # type: ignore[arg-type]
    svc._wxdb = wx  # type: ignore[assignment]
    return svc


class _StubWx:
    """服务层替身：会话/联系人/时间映射完全可控；可禁止逐条查库。"""

    def __init__(self, sessions, contacts=None, times=None,
                 self_id: str = _SELF, corp=None, degrade_kinds=None) -> None:
        self.degrade_kinds = set(degrade_kinds or ())
        self.sessions = list(sessions)
        self.contacts = {"chatroom": [], "friend": [], "openim": []}
        for kind, rows in (contacts or {}).items():
            self.contacts[kind] = list(rows)
        self.times = dict(times or {})
        self.self_id = self_id
        self.corp = dict(corp or {})
        self.nickname_calls: list[str] = []
        self.alias_calls: list[str] = []
        self.forbid_per_row = False
        self.friend_fn = None

    def get_contacts(self, kind: str = "friend", limit: int = 0) -> list[dict]:
        if kind in self.degrade_kinds:
            raise RuntimeError("模拟微信库未就绪：" + kind)
        rows = list(self.contacts.get(kind, []))
        return rows[:limit] if limit and limit > 0 else rows

    def get_sessions(self, limit: int = 100) -> list[dict]:
        return list(self.sessions[:limit])

    def get_session_times(self) -> dict:
        return dict(self.times)

    def get_openim_corp_names(self) -> dict:
        return dict(self.corp)

    def get_self_info(self) -> dict:
        return {"username": self.self_id, "nick_name": "我", "remark": ""}

    def _find(self, username: str) -> dict:
        for rows in self.contacts.values():
            for r in rows:
                if r["username"] == username:
                    return r
        return {}

    def get_nickname(self, username: str) -> str:
        self.nickname_calls.append(username)
        if self.forbid_per_row:
            raise AssertionError("批量路径不应逐条调 get_nickname: " + username)
        r = self._find(username)
        return str(r.get("remark") or r.get("nick_name") or username)

    def get_alias(self, username: str) -> str:
        self.alias_calls.append(username)
        if self.forbid_per_row:
            raise AssertionError("批量路径不应逐条调 get_alias: " + username)
        return str(self._find(username).get("alias") or "")

    def is_friend(self, username: str) -> bool:
        if self.friend_fn is not None:
            return bool(self.friend_fn(username))
        return any(r["username"] == username
                   for r in self.contacts["friend"] + self.contacts["openim"])


def _make_contact_db(path: str, rows: list[dict],
                     with_verify_flag: bool = True) -> None:
    cols = ("username TEXT, local_type INTEGER, alias TEXT, nick_name TEXT, "
            "remark TEXT, description TEXT, big_head_url TEXT, "
            "delete_flag INTEGER")
    if with_verify_flag:
        cols += ", verify_flag INTEGER"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE contact (" + cols + ")")
        for r in rows:
            base = (r["username"], r.get("local_type", 1), r.get("alias", ""),
                    r.get("nick_name", ""), r.get("remark", ""), "", "",
                    r.get("delete_flag", 0))
            if with_verify_flag:
                conn.execute("INSERT INTO contact VALUES (?,?,?,?,?,?,?,?,?)",
                             base + (r.get("verify_flag", 0),))
            else:
                conn.execute("INSERT INTO contact VALUES (?,?,?,?,?,?,?,?)",
                             base)
        conn.commit()
    finally:
        conn.close()


class _SqlWx(WeChatDB):
    """绕过解密 __init__，用真实生产 SQL 读临时/真实 contact.db。"""

    def __init__(self, path: str, sessions=None, times=None,
                 self_id: str = _SELF, readonly: bool = False) -> None:
        self._qa_path = path
        self._qa_readonly = readonly
        self._sessions = list(sessions or [])
        self._times = dict(times or {})
        self.account = f"{self_id}_aaaa"
        self.db_dir = ""
        self.account_dir = ""
        self.workdir = ""
        self._db_files: list = []

    def _contact_conn(self):
        if self._qa_readonly:
            conn = sqlite3.connect(
                "file:" + self._qa_path.replace("\\", "/") + "?mode=ro",
                uri=True)
        else:
            conn = sqlite3.connect(self._qa_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_sessions(self, limit: int = 100) -> list[dict]:
        return list(self._sessions)

    def get_session_times(self) -> dict:
        return dict(self._times)

    def get_nickname(self, username: str) -> str:
        conn = self._contact_conn()
        try:
            r = conn.execute(
                "SELECT remark,nick_name FROM contact WHERE username=?",
                (username,)).fetchone()
        finally:
            conn.close()
        return str((r["remark"] or r["nick_name"] or username) if r
                   else username)

    def get_alias(self, username: str) -> str:
        conn = self._contact_conn()
        try:
            r = conn.execute(
                "SELECT alias FROM contact WHERE username=?",
                (username,)).fetchone()
        finally:
            conn.close()
        return str(r["alias"] or "") if r else ""

    def get_openim_corp_names(self) -> dict:
        return {}

    def get_self_info(self) -> dict:
        return {"username": self.wxid, "nick_name": "我", "remark": ""}


@pytest.fixture(scope="module")
def real_contact_db():
    if not os.path.isfile(_REAL_CONTACT):
        pytest.skip("真实 contact.db 不存在，跳过实数据复核")
    return _REAL_CONTACT


def _read_sqlite(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro",
                           uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ===========================================================================
# A. 真实数据：verify_flag 会不会误伤真好友（最高风险点）
# ===========================================================================
class TestRealDataNoFalsePositive:

    def test_vf_nonzero_group_has_no_remark_and_no_biz_feature(
            self, real_contact_db):
        """真实库 vf!=0 的 905 条：remark 全空，无一是用户会认为的好友。"""
        conn = _read_sqlite(real_contact_db)
        try:
            groups = {
                r["verify_flag"]: (r["n"], r["r"])
                for r in conn.execute(
                    "SELECT verify_flag, COUNT(*) n, "
                    "SUM(CASE WHEN TRIM(COALESCE(remark,''))<>'' "
                    "THEN 1 ELSE 0 END) r "
                    "FROM contact WHERE local_type=1 AND delete_flag=0 "
                    "GROUP BY verify_flag").fetchall()
            }
            zero_n = groups.get(0, (0, 0))[0]
            nonzero = [(vf, n) for vf, (n, r) in groups.items() if vf]
            assert zero_n >= 700, "vf=0 真好友桶数量异常"
            assert sum(n for _, n in nonzero) >= 800
            # 核心：vf!=0 里带 remark 的例外数必须为 0（误伤判据）
            assert all(r == 0 for vf, (n, r) in groups.items() if vf), groups
            # 非 gh_ 的同桶记录逐条核对全是官方号/公众号
            rows = conn.execute(
                "SELECT nick_name FROM contact WHERE local_type=1 "
                "AND delete_flag=0 AND verify_flag<>0 "
                "AND username NOT LIKE 'gh\\_%' ESCAPE '\\'").fetchall()
            assert rows, "应存在 i黑马这类非 gh_ 公众号"
            assert all(str(r[0]) in _KNOWN_OFFICIAL_NICKS for r in rows), \
                [r[0] for r in rows]
        finally:
            conn.close()

    def test_iheima_is_official_two_friends_are_real(self, real_contact_db):
        """i黑马 vf=24 公众号实锤；两位淼淼好友 vf=0 且有 remark。"""
        conn = _read_sqlite(real_contact_db)
        try:
            def get(u):
                r = conn.execute(
                    "SELECT username,local_type,verify_flag,remark,alias,"
                    "nick_name FROM contact WHERE username=?", (u,)).fetchone()
                assert r is not None, u
                return r
            i = get(_IHEIMA)
            assert (i["local_type"], i["verify_flag"]) == (1, 24)
            assert not str(i["remark"] or "").strip()
            assert str(i["alias"]) == "iheima"
            for u in (_MIKE, _ZHANG):
                r = get(u)
                assert (r["local_type"], r["verify_flag"]) == (1, 0)
                assert "淼淼" in str(r["remark"])
        finally:
            conn.close()

    def test_vf_nonzero_not_among_monitoring_targets(self, real_contact_db):
        """用户已加入监控的 18 个对象，没有任何一个 vf!=0（不会误伤监控链路）。"""
        if not os.path.isfile(_REAL_BIZ):
            pytest.skip("业务库不存在")
        conn = _read_sqlite(real_contact_db)
        biz = _read_sqlite(_REAL_BIZ)
        try:
            users = {r[0] for r in biz.execute(
                "SELECT DISTINCT username FROM monitor_targets "
                "WHERE username<>''")}
            assert users, "监控目标不应为空"
            bad = conn.execute(
                "SELECT username FROM contact WHERE local_type=1 "
                "AND delete_flag=0 AND verify_flag<>0").fetchall()
            assert not (users & {r[0] for r in bad}), \
                "监控目标里出现 vf!=0 账号，过滤会误伤"
        finally:
            conn.close()
            biz.close()

    def test_real_db_search_heima_empty_miaomiao_two(self, real_contact_db):
        """真实库驱动生产 SQL + 服务层：黑马为空、淼淼仍只 2 位真好友。"""
        sessions = [{"username": u} for u in (
            _IHEIMA, _MIKE, _ZHANG, "123@chatroom",
            "wxid_group_nonfriend_miaomiao")]
        db = _SqlWx(real_contact_db, sessions=sessions, readonly=True)
        svc = _svc(db)
        assert svc.search_sessions(keyword="黑马", kind="friend") == []
        assert svc.search_sessions(keyword="iheima", kind="friend") == []
        hits = {i["username"] for i in
                svc.search_sessions(keyword="淼淼", kind="friend")}
        assert hits == {_MIKE, _ZHANG}
        friends = {c["username"] for c in db.get_contacts("friend")}
        assert _IHEIMA not in friends
        assert {_MIKE, _ZHANG} <= friends

    def test_real_db_is_friend_matrix(self, real_contact_db):
        """真实数据库逐点判定：公众号 False、真好友/企业微信 True、群 False。"""
        conn = _read_sqlite(real_contact_db)
        try:
            openim = conn.execute(
                "SELECT username FROM contact WHERE local_type IN (5,6,7) "
                "AND delete_flag=0 LIMIT 1").fetchone()
        finally:
            conn.close()
        db = _SqlWx(real_contact_db, readonly=True)
        svc = _svc(db)
        assert db.is_friend(_IHEIMA) is False
        assert svc.is_friend(_IHEIMA) is False
        for u in (_MIKE, _ZHANG):
            assert db.is_friend(u) is True
            assert svc.is_friend(u) is True
        assert db.is_friend("123@chatroom") is False
        if openim:
            assert db.is_friend(openim[0]) is True

    def test_monitoring_targets_still_visible_in_candidates(
            self, real_contact_db):
        """18 个监控目标在首屏候选里仍可见（filehelper 属既有系统号排除）。"""
        if not os.path.isfile(_REAL_BIZ):
            pytest.skip("业务库不存在")
        biz = _read_sqlite(_REAL_BIZ)
        try:
            users = [r[0] for r in biz.execute(
                "SELECT DISTINCT username FROM monitor_targets "
                "WHERE username<>'' ORDER BY username")]
        finally:
            biz.close()
        db = _SqlWx(real_contact_db, sessions=[{"username": u} for u in users],
                    readonly=True)
        svc = _svc(db)
        visible = {i["username"] for i in
                   svc.search_sessions(keyword="", limit=100)}
        expected = set(users) - {"filehelper"}   # 既有系统号语义
        assert expected <= visible, sorted(expected - visible)
        assert "filehelper" not in visible


# ===========================================================================
# B. SQL 语义：过滤位置、降级、企业微信 early return
# ===========================================================================
class TestSqlSemanticsIndependent:

    @pytest.fixture
    def shape_db(self, tmp_path):
        path = str(tmp_path / "contact.db")
        rows = [
            _row("u_friend", "真好友", "wxid_a", "备注A", vf=0),
            _row("u_official", "认证号", "iheima", "", vf=24),
            _row("g@chatroom", "群", "", "", local_type=2, vf=24),
            _row("corp@openim", "企业", "", "", local_type=5, vf=24),
            _row("gh_abc", "订阅号", "", "", local_type=1, vf=8),
        ]
        _make_contact_db(path, rows)
        return path

    def test_vf_filter_only_applies_to_friend_kind(self, shape_db):
        """vf 过滤只叠加在 friend 分支，不污染群/企业微信/公众号/全量。"""
        db = _SqlWx(shape_db)
        assert {c["username"] for c in db.get_contacts("friend")} == {"u_friend"}
        assert "g@chatroom" in {c["username"]
                                for c in db.get_contacts("chatroom")}
        assert "corp@openim" in {c["username"]
                                 for c in db.get_contacts("openim")}
        assert "gh_abc" in {c["username"]
                            for c in db.get_contacts("official")}
        assert {c["username"] for c in db.get_contacts("all")} >= {
            "u_friend", "u_official", "corp@openim", "gh_abc"}

    def test_enterprise_wechat_early_return_ignores_vf(
            self, shape_db):
        """5/6/7 在 vf 判定前 early return；delete_flag 仍先生效。"""
        conn = sqlite3.connect(shape_db)
        try:
            conn.execute(
                "UPDATE contact SET verify_flag=99 WHERE username='corp@openim'")
            conn.commit()
        finally:
            conn.close()
        db = _SqlWx(shape_db)
        assert db.is_friend("corp@openim") is True     # vf=99 也不叠加
        assert db.is_friend("u_official") is False    # 普通好友 vf!=0 仍挡
        conn = sqlite3.connect(shape_db)
        try:
            conn.execute(
                "UPDATE contact SET delete_flag=1 WHERE username='corp@openim'")
            conn.commit()
        finally:
            conn.close()
        assert db.is_friend("corp@openim") is False   # 删除位高于 early return

    def test_old_db_without_vf_column_degrades_without_crash(self, tmp_path):
        """老库无 verify_flag 列：SQL 不带过滤，好友不丢，不报错。"""
        path = str(tmp_path / "old.db")
        _make_contact_db(path, [_row("u_friend", "好友"),
                                _row("u_fake_official", "疑似号", vf=24)],
                         with_verify_flag=False)
        db = _SqlWx(path)
        names = {c["username"] for c in db.get_contacts("friend")}
        assert {"u_friend", "u_fake_official"} <= names
        assert all(c["verify_flag"] == 0 for c in db.get_contacts("friend"))
        assert db.get_contact("u_friend")["verify_flag"] == 0
        assert db.is_friend("u_friend") is True
        assert db.is_friend("u_fake_official") is True   # 老库无法分辨，保守放行

    def test_vf_null_passes_and_dirty_string_is_documented(self, tmp_path):
        """NULL 放行；脏字符串在 SQL 层被丢弃但 is_friend 保守放行。

        后者是当前实现的**已知边界**（真实列是 INTEGER，脏串仅在替身/人造
        库可出现）；这里以表征测试锁定现状，避免后续无声变化。
        """
        path = str(tmp_path / "null_dirty.db")
        _make_contact_db(path, [_row("u_null", "空标志"),
                                _row("u_dirty", "脏标志")])
        conn = sqlite3.connect(path)
        try:
            conn.execute("UPDATE contact SET verify_flag=NULL "
                         "WHERE username='u_null'")
            conn.execute("UPDATE contact SET verify_flag='bad' "
                         "WHERE username='u_dirty'")
            conn.commit()
        finally:
            conn.close()
        db = _SqlWx(path)
        friends = {c["username"] for c in db.get_contacts("friend")}
        assert "u_null" in friends and db.is_friend("u_null") is True
        assert "u_dirty" not in friends          # SQL 层无法容错脏串
        assert db.is_friend("u_dirty") is True   # 单点判定按 0 放行
        assert WeChatDB._parse_verify_flag("bad") == 0
        assert WeChatDB._parse_verify_flag(None) == 0

    def test_stub_downgrade_still_blocks_official_in_candidates(self):
        """替身 get_contacts 未按 vf 过滤时，_friend_row_ok 再挡一道。"""
        friends = [
            _row(_IHEIMA, "i黑马", "iheima", "", vf=24),
            _row("u_null_vf", "空标志", "", "", vf=None),
            _row("u_real", "真好友", "", "备注"),
        ]
        wx = _StubWx([{"username": r["username"]} for r in friends],
                     contacts={"friend": friends})
        svc = _svc(wx)
        hits = {i["username"] for i in
                svc.search_sessions(keyword="", kind="friend")}
        assert _IHEIMA not in hits
        assert {"u_null_vf", "u_real"} <= hits


# ===========================================================================
# C. 首屏排序：全局 top-N 与脏值安全
# ===========================================================================
class TestFirstScreenGlobalTopN:

    def _mass_wx(self, kind: str, count: int = 65):
        prefix = "g" if kind == "group" else "u"
        suffix = "@chatroom" if kind == "group" else ""
        rows = []
        for i in range(count):
            u = f"{prefix}_m{i:03d}{suffix}"
            rows.append(_row(u, f"成员{i:03d}", local_type=2 if kind == "group" else 1))
        # 缓存/会话顺序故意逆序：时间最靠前的放在索引末尾（>limit 的位置）
        sessions = [{"username": r["username"]} for r in reversed(rows)]
        times = {r["username"]: 1000 + i for i, r in enumerate(rows)}
        wx = _StubWx(sessions,
                     contacts={"chatroom" if kind == "group" else "friend": rows},
                     times=times)
        return wx, rows

    @pytest.mark.parametrize("kind", ["group", "friend"])
    def test_limit50_selects_global_top_from_65(self, kind):
        """前端 limit=50：必须选出全量 65 条里时间最靠前的 50 条。"""
        wx, rows = self._mass_wx(kind)
        svc = _svc(wx)
        got = svc.search_sessions(keyword="", kind=kind, limit=50)
        expected = [r["username"] for r in reversed(rows)][:50]
        assert len(got) == 50
        assert [i["username"] for i in got] == expected, \
            "先截断再排序会漏掉尾部的时间最新会话"

    def test_messaged_before_silent_and_stable_within_silent(self):
        """有消息在前、无消息在后；无消息组保持原缓存顺序。"""
        rows = [_row(f"u_silent{i}", f"静默{i}") for i in range(3)]
        rows += [_row(f"u_msg{i}", f"活跃{i}") for i in range(3)]
        sessions = [{"username": r["username"]} for r in rows]
        wx = _StubWx(sessions, contacts={"friend": rows},
                     times={"u_msg0": 10, "u_msg1": 30, "u_msg2": 20})
        got = [i["username"] for i in _svc(wx).search_sessions(
            keyword="", kind="friend")]
        assert got[:3] == ["u_msg1", "u_msg2", "u_msg0"]
        assert got[3:] == ["u_silent0", "u_silent1", "u_silent2"]

    def test_dirty_timestamps_are_safe_on_first_screen(self):
        """首屏：字符串/负数/None 时间戳均不崩，None 按无消息处理。"""
        rows = [_row("u_str", "字符串"), _row("u_pos", "正数"),
                _row("u_neg", "负数"), _row("u_none", "空值"),
                _row("u_zero", "零")]
        wx = _StubWx([{"username": r["username"]} for r in rows],
                     contacts={"friend": rows},
                     times={"u_str": "123", "u_pos": 9, "u_neg": -5,
                            "u_none": None, "u_zero": 0})
        got = [i["username"] for i in _svc(wx).search_sessions(
            keyword="", kind="friend")]
        assert set(got) == {r["username"] for r in rows}
        assert got.index("u_str") < got.index("u_pos") < got.index("u_neg")
        assert got.index("u_neg") < got.index("u_none")
        assert got[-2:] == ["u_none", "u_zero"]   # 无消息组稳定

    def test_keyword_rank_first_then_time_and_global_top50(self):
        """关键词：匹配质量主排序 + 同组时间降序 + 全局 top50 截断。"""
        rows = [_row(f"u_hit{i:03d}", f"测试淼{i:03d}")
                for i in range(60)]
        rows.append(_row("u_exact_old", "淼淼"))  # rank0，时间最旧
        sessions = [{"username": r["username"]} for r in reversed(rows)]
        times = {r["username"]: 1000 + i for i, r in enumerate(rows[:60])}
        times["u_exact_old"] = 1
        svc = _svc(_StubWx(sessions, contacts={"friend": rows}, times=times))
        got = [i["username"] for i in svc.search_sessions(
            keyword="淼", kind="friend", limit=50)]
        assert got[0] == "u_exact_old"             # 精确匹配压过时间
        expected = [r["username"] for r in reversed(rows[:60])][:49]
        assert got[1:] == expected                 # 同组内全量排序后取 top49

    @pytest.mark.xfail(strict=True,
                       reason="既有边界：keyword 分支 -ts 未做 int 转换，"
                              "None/字符串会 TypeError（非本轮引入）")
    @pytest.mark.parametrize("dirty", [None, "123"])
    def test_keyword_dirty_timestamp_should_not_crash(self, dirty):
        """期望（非本轮范围）：脏时间戳在关键词搜索也不应抛异常。"""
        row = _row("u_x", "淼淼好友")
        wx = _StubWx([{"username": "u_x"}], contacts={"friend": [row]},
                     times={"u_x": dirty})
        got = _svc(wx).search_sessions(keyword="淼", kind="friend")
        assert [i["username"] for i in got] == ["u_x"]


# ===========================================================================
# D. 批量优化：零逐条查询 / 口径一致 / 回退 / 2101 好友
# ===========================================================================
class TestBatchLoadingIndependent:

    def test_zero_per_row_queries_for_all_session_rows(self):
        """100 个会话全在联系人映射中：严禁逐条 get_nickname/get_alias。"""
        rows = [_row(f"u_{i:03d}", f"好友{i}", f"wxid_alias_{i}")
                for i in range(60)]
        rows += [_row(f"g_{i:03d}@chatroom", f"群{i}",
                      local_type=2) for i in range(40)]
        wx = _StubWx([{"username": r["username"]} for r in rows],
                     contacts={"friend": rows[:60], "chatroom": rows[60:]})
        wx.forbid_per_row = True
        items = _svc(wx).search_sessions(keyword="", limit=100)
        assert len(items) == 100
        assert wx.nickname_calls == [] and wx.alias_calls == []

    @pytest.mark.parametrize("alias,remark,nick,expected_key", [
        ("wxid_abc", "备注", "昵称", "alias"),
        ("", "备注", "昵称", "remark"),
        ("", "", "昵称", "nick"),
        ("", "", "", "username"),
    ])
    def test_batch_alias_matches_per_row_get_wechat_id(
            self, alias, remark, nick, expected_key):
        """批量 alias 与逐条 get_wechat_id 口径一致（含 remark>nick>username）。"""
        u = "u_matrix"
        row = _row(u, nick, alias, remark)
        wx = _StubWx([{"username": u}], contacts={"friend": [row]})
        svc = _svc(wx)
        item = svc.search_sessions(keyword="", kind="friend")[0]
        expected = {"alias": alias, "remark": remark, "nick": nick,
                    "username": ""}[expected_key]
        assert item["alias"] == expected
        assert svc.get_wechat_id(u) == expected

    def test_chatroom_alias_empty_in_batch_and_fallback(self):
        """群聊微信号批量/回退都为空串。"""
        u = "g_matrix@chatroom"
        row = _row(u, "群", "should_not_use", local_type=2)
        wx = _StubWx([{"username": u}], contacts={"chatroom": [row]})
        svc = _svc(wx)
        assert svc.search_sessions(keyword="", kind="group")[0]["alias"] == ""
        assert svc.get_wechat_id(u) == ""

    def test_missing_contact_row_falls_back_exactly_once(self):
        """contact 表无对应行：回退逐条查询且各只调一次，不崩。"""
        u = "u_session_only"
        # 好友表读不到（降级）时才放行非好友白名单外的会话，走逐条回退路径
        wx = _StubWx([{"username": u}], contacts={"friend": []},
                     degrade_kinds={"friend"})
        svc = _svc(wx)
        item = svc.search_sessions(keyword="", kind="friend")[0]
        assert item["display_name"] == u and item["alias"] == ""
        assert wx.nickname_calls.count(u) == 1
        assert wx.alias_calls.count(u) == 1

    def test_item_fields_complete_for_both_paths(self):
        """会话路径与整表路径产出的字段集合一致且完整。"""
        in_row = _row("u_in", "在表内", "alias_in")
        wx = _StubWx([{"username": "u_in"}, {"username": "u_out"}],
                     contacts={"friend": [in_row]},
                     degrade_kinds={"friend"})
        items = {i["username"]: i
                 for i in _svc(wx).search_sessions(keyword="", kind="friend")}
        keys = {"username", "display_name", "alias", "kind", "openim",
                "corp", "is_self"}
        assert set(items) == {"u_in", "u_out"}
        for item in items.values():
            assert keys <= set(item), item
        assert items["u_in"]["alias"] == "alias_in"
        assert items["u_in"]["display_name"] == "在表内"

    def test_2101_friends_tail_still_searchable(self, tmp_path):
        """超过历史 2000 截断线：压轴好友仍可搜到（全量取回不截断）。"""
        path = str(tmp_path / "many.db")
        rows = [_row(f"u_{i:05d}", f"普通好友{i:05d}", f"alias_{i:05d}")
                for i in range(2100)]
        rows.append(_row("zzz_tail_friend", "压轴好友", "tail_alias_zzz"))
        _make_contact_db(path, rows)
        db = _SqlWx(path, sessions=[{"username": "zzz_tail_friend"}])
        assert len(db.get_contacts("friend")) >= 2101
        svc = _svc(db)
        assert any(i["username"] == "zzz_tail_friend"
                   for i in svc.search_sessions(keyword="压轴好友"))
        assert any(i["username"] == "zzz_tail_friend"
                   for i in svc.search_sessions(keyword="tail_alias_zzz"))


# ===========================================================================
# E. 链路回归：私信 target / 回复 target / 监控候选
# ===========================================================================
class TestChainRegression:

    @pytest.fixture
    def dm_db(self, tmp_path):
        path = str(tmp_path / "dm.db")
        rows = [
            _row(_IHEIMA, "i黑马", "iheima", "", vf=24),
            _row(_MIKE, "MiKE", "uzairsultan", "MiKE 淼淼英语老师"),
            _row("111@chatroom", "群聊", local_type=2),
        ]
        _make_contact_db(path, rows)
        return _svc(_SqlWx(path))

    def test_dm_target_blocks_official_allows_friend(self, dm_db):
        """商机私信回复：i黑马 400 拦截，真好友放行。"""
        opp = {"session_username": "111@chatroom",
               "sender_wxid": _IHEIMA, "sender_name": "i黑马"}
        with pytest.raises(HTTPException) as err:
            _dm_target(opp, dm_db)
        assert err.value.status_code == 400
        opp["sender_wxid"] = _MIKE
        target, name = _dm_target(opp, dm_db)
        assert target == _MIKE and "淼淼" in name

    def test_reply_target_button_state(self, dm_db, monkeypatch):
        """回复去向：公众号 sender_is_friend=False，真好友 True。"""
        from app.api import router_opportunities as ro

        class _FakeBizDb:
            def get_opportunity(self, opp_id):
                return {"id": opp_id, "session_username": "111@chatroom",
                        "sender_wxid": _IHEIMA, "sender_name": "i黑马",
                        "session_name": "群聊"}

        monkeypatch.setattr(ro, "get_wechat_service_optional", lambda: dm_db)
        out = ro.get_reply_targets(1, db=_FakeBizDb())
        assert out.sender_is_friend is False
        assert "不是你的微信好友" in out.reason
        _FakeBizDb.get_opportunity = lambda self, opp_id: {
            "id": opp_id, "session_username": "111@chatroom",
            "sender_wxid": _MIKE, "sender_name": "MiKE",
            "session_name": "群聊"}
        out = ro.get_reply_targets(1, db=_FakeBizDb())
        assert out.sender_is_friend is True
