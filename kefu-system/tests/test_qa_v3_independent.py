# -*- coding: utf-8 -*-
"""QA v3 独立回归（严过关 / 第二层质量关卡）。

本文件**不复用**工程师自测用例的断言，独立设计以下高价值场景：

修复 1（好友口径 local_type=1）：
- 真实数据形态下搜「淼淼」只回 2 位 lt=1 真好友，5 个 lt=3 全挡；
- local_type 全矩阵（0/1/2/3/4/5/6/7）+ delete_flag + 系统号/自己/群/公众号
  的 is_friend / get_contacts("friend") 口径；
- 私信回复门控：lt=3 置灰、lt=1/5/6/7 可用；商机创建/回复链路不回归；
- lt=0 带 alias 的独立判断：172 个 gh_ 公众号 + 62 个 wxid_ + 5 个其它，
  flag 全 0、remark 全空，判为非好友不过误伤（附真实数据只读复核）。

修复 2（微信号 alias 显示）：
- alias 优先 → 备注/昵称回退 → 回退值==username 返回空串 → 群聊空串 →
  svc None / 读失败降级空串；进程内缓存只读一次；
- 前后端 schema 字段一致（后端默认空串 + 前端 types.ts 守门）；
- 别名纳入会话搜索命中、_match_rank 权重；resolve_wechat_id 单一实现收敛。

所有测试只用临时 SQLite / 内存替身，不碰 UIA 实机、不改生产数据。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_KEFU_ROOT = _TESTS_DIR.parent
_REPO_ROOT = _KEFU_ROOT.parent
for _p in (str(_REPO_ROOT), str(_KEFU_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import HTTPException  # noqa: E402

from wechatauto.db import WeChatDB  # noqa: E402
from app import wechat_service as wsmod  # noqa: E402
from app.api import router_opportunities as opp_route  # noqa: E402
from app.api import router_targets as tgt_route  # noqa: E402
from app.sender_display import resolve_wechat_id  # noqa: E402
from app.schemas import (  # noqa: E402
    AnalysisLogOut, GroupMemberOut, MonitorTargetOut, OpportunityListItem,
    ReplyTargetOut, SessionItemOut,
)
from app.wechat_service import WeChatService  # noqa: E402

_SELF = "wxid_self"
_REAL_CONTACT_DB = (_KEFU_ROOT / "data" / "wechat_cache"
                    / "contact__contact.db")


# ---------------------------------------------------------------------------
# 公共替身 / fixture
# ---------------------------------------------------------------------------
def _make_wxdb(path: str, self_user: str = _SELF) -> WeChatDB:
    """造一个只读临时 contact 库的 WeChatDB（不碰真实微信缓存）。"""
    db = object.__new__(WeChatDB)
    db.account = f"{self_user}_aaaa"   # wxid property 去掉末尾 _4位
    db.get_self_info = lambda: {"username": self_user}  # type: ignore

    def _conn() -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    db._contact_conn = _conn  # type: ignore
    return db


@pytest.fixture
def contact_db(tmp_path):
    """真实形态联系人表：2 lt=1 淼淼好友 + 5 lt=3 非好友 + 全 local_type 边界。

    字段顺序与 :meth:`WeChatDB.get_contacts` 的 SELECT 对齐。
    """
    path = str(tmp_path / "contact.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE contact ("
        "username TEXT, local_type INTEGER, alias TEXT, nick_name TEXT, "
        "remark TEXT, description TEXT, big_head_url TEXT, delete_flag INTEGER)"
    )
    rows = [
        # —— 用户点名的 2 位通讯录真好友（lt=1, flag=3, alias/remark 非空）——
        ("wxid_9941039410111", 1, "Xiangyanedu", "张鹏", "张鹏 大淼淼爸爸", "", "", 0),
        ("wxid_3f76xuokjw8b72", 1, "uzairsultan", "MiKE", "MiKE 淼淼英语老师", "", "", 0),
        # —— 搜「淼淼」混入的 5 个群里见过的非好友（lt=3, flag=4, alias/remark 全空）——
        ("wxid_o12edjzelv8r22", 3, "", "淼淼", "", "", "", 0),
        ("wxid_n53xa2hqwz7e21", 3, "", "🍎田淼淼~康宝莱营养顾问", "", "", "", 0),
        ("wxid_ybdhzq01eiz322", 3, "", "肖淼淼", "", "", "", 0),
        ("wxid_vpf9h0izg19b22", 3, "", "悠水瑜伽淼淼", "", "", "", 0),
        ("wxid_0ecrf6i90u1p21", 3, "", "鑫淼淼", "", "", "", 0),
        # —— lt=0：非好友交互者（含 239 个带 alias 的真实形态）——
        ("wxid_lt0_alias", 0, "some_alias", "单向好友", "", "", "", 0),
        ("gh_lt0_official", 0, "official_alias", "公众号交互", "", "", "", 0),
        # —— lt=2 群 / lt=4 公众号 ——
        ("123456@chatroom", 2, "", "某群", "", "", "", 0),
        ("gh_abc123", 4, "gh_alias", "公众号", "", "", "", 0),
        # —— 企业微信 5/6/7 ——
        ("11111@openim", 5, "", "企业微信好友5", "", "", "", 0),
        ("22222@openim", 6, "", "企业微信好友6", "", "", "", 0),
        ("33333@kefu.openim", 7, "", "客服7", "", "", "", 0),
        # —— lt=1 桶里需按 username 排除的：gh_ / 系统号 / 自己 / 已删 ——
        ("gh_in_lt1", 1, "wxzhifu", "微信支付", "", "", "", 0),
        ("newsapp", 1, "", "系统号", "", "", "", 0),
        (_SELF, 1, "", "我自己", "", "", "", 0),
        ("wxid_deleted", 1, "deleted_alias", "已删好友", "", "", "", 1),
    ]
    conn.executemany("INSERT INTO contact VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


def _names(db: WeChatDB, kind: str = "friend") -> set[str]:
    return {c["username"] for c in db.get_contacts(kind)}


class _AliasWx:
    """轻量 WeChatDB 替身：alias / nickname 可控。"""

    def __init__(self, aliases=None, names=None):
        self.aliases = aliases or {}
        self.names = names or {}

    def get_alias(self, username: str) -> str:
        return self.aliases.get(username, "")

    def get_nickname(self, username: str) -> str:
        return self.names.get(username, username) or username


def _svc_with(wx) -> WeChatService:
    svc = WeChatService(None, None)  # type: ignore[arg-type]
    if wx is not None:
        svc._wxdb = wx  # type: ignore[assignment]
    return svc


# ---------------------------------------------------------------------------
# 修复 1：好友口径
# ---------------------------------------------------------------------------
class TestFriendSemanticsIndependent:
    """get_contacts("friend") / is_friend 的 local_type 口径独立验证。"""

    def test_search_miamiao_only_two_lt1_friends(self, contact_db):
        """搜「淼淼」：2 位 lt=1 真好友在，5 个 lt=3 全部被挡。"""
        db = _make_wxdb(contact_db)
        friends = db.get_contacts("friend")
        hit = [c for c in friends if "淼淼" in (
            (c["nick_name"] or "") + (c["remark"] or "") + (c["alias"] or ""))]
        assert {c["username"] for c in hit} == {
            "wxid_9941039410111", "wxid_3f76xuokjw8b72"}
        assert all(c["local_type"] == 1 for c in hit)

    def test_five_lt3_all_blocked(self, contact_db):
        db = _make_wxdb(contact_db)
        names = _names(db)
        for u in ("wxid_o12edjzelv8r22", "wxid_n53xa2hqwz7e21",
                  "wxid_ybdhzq01eiz322", "wxid_vpf9h0izg19b22",
                  "wxid_0ecrf6i90u1p21"):
            assert u not in names, f"lt=3 群内非好友漏过好友候选：{u}"

    def test_full_local_type_matrix_in_get_contacts(self, contact_db):
        """friend 只含 lt=1 且非 gh_/系统号/自己/已删；openim 含 5/6/7。"""
        db = _make_wxdb(contact_db)
        names = _names(db)
        # 保留：2 位 lt=1 真好友
        assert {"wxid_9941039410111", "wxid_3f76xuokjw8b72"} <= names
        # 排除：lt=0/2/3/4、gh_、系统号、自己、已删
        for u in ("wxid_lt0_alias", "gh_lt0_official", "123456@chatroom",
                  "gh_abc123", "gh_in_lt1", "newsapp", _SELF, "wxid_deleted"):
            assert u not in names, f"非好友/异常账号漏过：{u}"
        assert _names(db, "openim") == {
            "11111@openim", "22222@openim", "33333@kefu.openim"}

    @pytest.mark.parametrize("username,expected", [
        ("wxid_9941039410111", True),      # lt=1 真好友
        ("wxid_3f76xuokjw8b72", True),
        ("wxid_o12edjzelv8r22", False),    # lt=3 群内非好友
        ("wxid_lt0_alias", False),         # lt=0 带 alias 也不算好友
        ("gh_lt0_official", False),
        ("123456@chatroom", False),        # lt=2 群
        ("gh_abc123", False),              # lt=4 公众号
        ("11111@openim", True),            # lt=5 企业微信
        ("22222@openim", True),            # lt=6
        ("33333@kefu.openim", True),       # lt=7
        ("gh_in_lt1", False),              # lt=1 但 gh_ 公众号
        ("newsapp", False),                # 系统号
        (_SELF, False),                    # 自己
        ("wxid_deleted", False),           # delete_flag=1
        ("wxid_not_in_db", False),         # 表里没有
    ])
    def test_is_friend_full_matrix(self, contact_db, username, expected):
        db = _make_wxdb(contact_db)
        assert db.is_friend(username) is expected, (
            f"is_friend({username}) 应为 {expected}")

    def test_service_is_friend_delegates_and_self_guard(self, contact_db):
        """WeChatService.is_friend 委托 DB 且自己永远 False。"""
        svc = _svc_with(_make_wxdb(contact_db))
        assert svc.is_friend("wxid_9941039410111") is True
        assert svc.is_friend("wxid_o12edjzelv8r22") is False
        assert svc.is_friend(_SELF) is False

    def test_service_is_friend_false_when_db_degraded(self):
        svc = WeChatService(None, None)  # type: ignore[arg-type]
        assert svc.is_friend("wxid_9941039410111") is False


class TestLt0IndependentJudgment:
    """lt=0 带 alias 是否误伤「仅聊天好友」——独立判断：不误伤。"""

    @pytest.mark.parametrize("username", [
        "wxid_lt0_alias",      # wxid_ 形态（62 条真实样本之一）
        "gh_lt0_official",     # gh_ 公众号（172 条）
    ])
    def test_lt0_alias_not_friend(self, contact_db, username):
        db = _make_wxdb(contact_db)
        assert db.is_friend(username) is False
        assert username not in _names(db)

    def test_real_db_lt0_alias_are_official_or_no_remark(self):
        """真实 contact.db 只读复核：lt=0 带 alias 的记录全部 flag=0、remark 空。

        172 个 gh_ 公众号 + 62 个 wxid_ + 5 个其它，均非通讯录好友；
        真正的「仅聊天好友」仍在通讯录（lt=1，可设备注、flag 带好友位），
        所以排除 lt=0 不会误伤用户认为的好友。
        """
        if not _REAL_CONTACT_DB.exists():
            pytest.skip("真实 contact.db 不存在，跳过实数据复核")
        con = sqlite3.connect(
            f"file:{_REAL_CONTACT_DB}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT username, alias, remark, flag FROM contact "
                "WHERE local_type=0 AND delete_flag=0 AND alias<>''").fetchall()
        finally:
            con.close()
        assert rows, "真实库应有 lt=0 带 alias 记录"
        assert all(not (r[2] or "").strip() for r in rows), "lt=0 不应有备注"
        assert all((r[3] or 0) in (0, 2147483648) for r in rows), (
            "lt=0 无好友位 flag")
        gh = sum(1 for r in rows if r[0].startswith("gh_"))
        assert gh >= 0.5 * len(rows), "lt=0 带 alias 里公众号应占多数"


class TestReplyTargetFriendGate:
    """群商机「私信回复」按钮：lt=3 置灰、lt=1/5/6/7 可用；链路不回归。"""

    @staticmethod
    def _opp(sender: str) -> dict:
        return {
            "id": 1, "session_username": "999@chatroom",
            "session_name": "某群", "sender_wxid": sender,
            "sender_name": "发送人",
        }

    def _targets(self, sender: str, contact_db):
        svc = _svc_with(_make_wxdb(contact_db))
        db = _make_wxdb(contact_db)  # 只借 get_opportunity 的替身语义
        db.get_opportunity = lambda oid: self._opp(sender)  # type: ignore
        monkeypatch_svc = svc
        # 直接调路由函数（绕过 Depends）：临时替换可选服务提供者
        orig = opp_route.get_wechat_service_optional
        opp_route.get_wechat_service_optional = lambda: monkeypatch_svc  # type: ignore
        try:
            return opp_route.get_reply_targets(1, db=db)
        finally:
            opp_route.get_wechat_service_optional = orig  # type: ignore

    @pytest.mark.parametrize("sender,expected", [
        ("wxid_o12edjzelv8r22", False),   # lt=3 → 私信置灰
        ("wxid_lt0_alias", False),        # lt=0 → 置灰
        ("wxid_9941039410111", True),     # lt=1 → 可用
        ("11111@openim", True),           # lt=5 → 可用
        ("22222@openim", True),           # lt=6 → 可用
        ("33333@kefu.openim", True),      # lt=7 → 可用
    ])
    def test_private_reply_gate(self, contact_db, sender, expected):
        out = self._targets(sender, contact_db)
        assert out.sender_is_friend is expected, (
            f"sender={sender} 私信可用性应为 {expected}")
        if expected:
            assert out.reason == ""
        else:
            assert out.reason, "置灰时应给出可读原因"

    def test_dm_target_blocks_nonfriend(self, contact_db):
        """私信回复真正入队前的校验：lt=3 抛 400，lt=1 放行。"""
        svc = _svc_with(_make_wxdb(contact_db))
        with pytest.raises(HTTPException) as ei:
            opp_route._dm_target(self._opp("wxid_o12edjzelv8r22"), svc)
        assert ei.value.status_code == 400
        target, _ = opp_route._dm_target(self._opp("wxid_9941039410111"), svc)
        assert target == "wxid_9941039410111"


# ---------------------------------------------------------------------------
# 修复 2：微信号显示
# ---------------------------------------------------------------------------
class TestWechatIdDisplayIndependent:
    """get_wechat_id / resolve_wechat_id 的回退与降级。"""

    def test_alias_priority(self):
        wx = _AliasWx(
            aliases={"wxid_a": "Xiangyanedu"},
            names={"wxid_a": "张鹏 大淼淼爸爸"})
        assert _svc_with(wx).get_wechat_id("wxid_a") == "Xiangyanedu"

    def test_alias_empty_falls_back_to_name(self):
        wx = _AliasWx(aliases={"wxid_b": ""},
                      names={"wxid_b": "MiKE 淼淼英语老师"})
        assert _svc_with(wx).get_wechat_id("wxid_b") == "MiKE 淼淼英语老师"

    def test_fallback_equal_username_returns_empty(self):
        """回退值==username（解析不到）→ 空串，绝不把 wxid 塞进微信号位。"""
        wx = _AliasWx(names={"wxid_raw": "wxid_raw"})
        assert _svc_with(wx).get_wechat_id("wxid_raw") == ""

    def test_chatroom_returns_empty(self):
        """群没有微信号；不能把群名塞进微信号位。"""
        wx = _AliasWx(names={"123@chatroom": "某群"})
        assert _svc_with(wx).get_wechat_id("123@chatroom") == ""

    def test_empty_username_returns_empty(self):
        assert _svc_with(_AliasWx()).get_wechat_id("") == ""

    def test_no_wxdb_returns_empty(self):
        assert _svc_with(None).get_wechat_id("wxid_a") == ""

    def test_read_failure_falls_back(self):
        class BrokenWx(_AliasWx):
            def get_alias(self, username):
                raise RuntimeError("contact.db 正被微信写入")

        assert _svc_with(BrokenWx(
            names={"wxid_c": "回退昵称"})).get_wechat_id("wxid_c") == "回退昵称"

    def test_alias_cached_read_once(self):
        calls = []

        class CountingWx(_AliasWx):
            def get_alias(self, username):
                calls.append(username)
                return "cached"

        svc = _svc_with(CountingWx())
        assert svc.get_wechat_id("wxid_d") == "cached"
        assert svc.get_wechat_id("wxid_d") == "cached"
        assert calls == ["wxid_d"]

    def test_get_alias_from_real_temp_db(self, contact_db):
        db = _make_wxdb(contact_db)
        assert db.get_alias("wxid_9941039410111") == "Xiangyanedu"
        assert db.get_alias("wxid_o12edjzelv8r22") == ""   # lt=3 无 alias
        assert db.get_alias("wxid_missing") == ""

    @pytest.mark.parametrize("svc,username,expected", [
        (None, "wxid_a", ""),                       # svc None 降级
        (object(), "wxid_a", ""),                   # 无 get_wechat_id 方法
        ("not-a-service", "wxid_a", ""),
    ])
    def test_resolve_wechat_id_degrades_empty(self, svc, username, expected):
        assert resolve_wechat_id(svc, username) == expected

    def test_resolve_wechat_id_wraps_service(self):
        svc = _svc_with(_AliasWx(aliases={"wxid_a": "real_alias"}))
        assert resolve_wechat_id(svc, "wxid_a") == "real_alias"

    def test_resolve_wechat_id_single_implementation(self):
        """三个 router 的别名包装都应指向 sender_display.resolve_wechat_id。"""
        from app.api import router_analysis as ana_route
        assert ana_route._alias_of is resolve_wechat_id
        assert opp_route._alias_of is resolve_wechat_id
        assert tgt_route.resolve_wechat_id is resolve_wechat_id


class TestAliasSearchIndependent:
    """别名纳入会话搜索命中与排序权重。"""

    def test_search_by_alias_only(self):
        svc = _svc_with(_AliasWx())
        # 直接注入缓存（等价于 _build_contact_list 产物）避免撞微信库
        svc._contact_cache = [
            {"username": "wxid_x", "display_name": "张鹏",
             "alias": "Xiangyanedu", "kind": "friend", "openim": False},
            {"username": "wxid_y", "display_name": "MiKE",
             "alias": "uzairsultan", "kind": "friend", "openim": False},
        ]
        svc._contact_cache_ts = time.monotonic()
        # 搜索过滤沿用既有大小写敏感语义（与显示名/username 一致）；
        # 用微信号原样大小写检索即可命中（大小写不敏感仅体现在 _match_rank 排序）。
        hits = svc.search_sessions(keyword="Xiangyanedu", kind="friend")
        assert [h["username"] for h in hits] == ["wxid_x"]

    def test_match_rank_alias_weights(self):
        rank = WeChatService._match_rank
        assert rank("张鹏", "wxid_x", "xiangyanedu", "Xiangyanedu") == 0
        assert rank("张鹏", "wxid_x", "xiangy", "Xiangyanedu") == 1
        assert rank("张鹏", "wxid_x", "yan", "Xiangyanedu") == 2
        assert rank("张鹏", "wxid_x", "wxid_x", "") == 3
        assert rank("张鹏", "wxid_x", "nomatch", "") == 4


class TestContractAndRegression:
    """前后端字段契约 + 修复不破坏商机/监控链路。"""

    def test_backend_schema_alias_defaults(self):
        assert SessionItemOut(username="u", display_name="n", kind="friend").alias == ""
        assert MonitorTargetOut(id=1, username="u", display_name="n",
                                kind="friend", enabled=1).alias == ""
        assert OpportunityListItem(id=1).session_alias == ""
        assert OpportunityListItem(id=1).sender_alias == ""
        assert AnalysisLogOut(id=1).session_alias == ""
        assert AnalysisLogOut(id=1).sender_alias == ""
        assert ReplyTargetOut().sender_alias == ""
        assert GroupMemberOut().alias == ""

    def test_frontend_types_declares_all_alias_fields(self):
        types_path = _KEFU_ROOT / "frontend" / "src" / "api" / "types.ts"
        text = types_path.read_text(encoding="utf-8")
        for token in ("alias: string", "session_alias", "sender_alias"):
            assert token in text, f"前端 types.ts 缺少字段：{token}"

    def test_opportunity_display_enrichment_regression(self):
        """商机列表显示名与微信号两套字段互不覆盖；降级不 500。"""
        svc = _svc_with(_AliasWx(
            aliases={"wxid_room": "群主微信号", "wxid_sender": "sender_alias"},
            names={"wxid_room": "某群", "wxid_sender": "发送人"}))
        rows = [{"id": 1, "session_username": "wxid_room",
                 "session_name": "某群", "sender_wxid": "wxid_sender",
                 "sender_name": "发送人"}]
        out = opp_route._with_displays(rows, svc)[0]
        assert out["session_alias"] == "群主微信号"
        assert out["sender_alias"] == "sender_alias"
        assert out["session_display"] == "某群"
        assert out["sender_display"] == "发送人"
        degraded = opp_route._with_displays(rows, object())[0]
        assert degraded["session_alias"] == ""

    def test_monitor_target_alias_enrichment_regression(self):
        """监控对象补 alias 不影响 id/username/enabled 等主字段。"""
        svc = _svc_with(_AliasWx(aliases={"wxid_t": "target_alias"},
                                 names={"wxid_t": "目标"}))
        out = tgt_route._with_alias(
            {"id": 1, "username": "wxid_t", "display_name": "目标",
             "kind": "friend", "enabled": 1}, svc)
        assert out["alias"] == "target_alias"
        assert out["id"] == 1 and out["username"] == "wxid_t"
        assert out["enabled"] == 1
