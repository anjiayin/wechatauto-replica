# -*- coding: utf-8 -*-
"""任务 #2：界面「微信号（alias）」透出与回退规则测试。

用户原话：界面上凡是显示 wxid_xxx 的位置，改显示微信里可见的微信号
（contact.alias）；不要与「显示名（昵称/备注）」混为一谈。

锁定契约：
1. ``WeChatService.get_wechat_id``：alias 优先 → 备注/昵称回退 → 空串
   （绝不把对用户无意义的 wxid 塞进微信号位）；
2. 后端 schema 带 alias/session_alias/sender_alias 字段，路由负责补齐；
3. 前端 types.ts 字段与后端一一对应（文本级守门，防改了一边漏一边）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from app.schemas import (  # noqa: E402
    AnalysisLogOut,
    GroupMemberOut,
    MonitorTargetOut,
    OpportunityListItem,
    ReplyTargetOut,
    SessionItemOut,
)
from app.api.router_analysis import _alias_of as log_alias_of  # noqa: E402
from app.api.router_opportunities import (  # noqa: E402
    _alias_of as opp_alias_of,
    _with_displays,
)
from app.api.router_targets import _with_alias  # noqa: E402
from app.wechat_service import WeChatService  # noqa: E402


class _AliasWx:
    """WeChatDB 替身：alias / nickname 分别可控。"""

    def __init__(self, aliases: dict[str, str] | None = None,
                 names: dict[str, str] | None = None) -> None:
        self.aliases = aliases or {}
        self.names = names or {}

    def get_alias(self, username: str) -> str:
        return self.aliases.get(username, "")

    def get_nickname(self, username: str) -> str:
        return self.names.get(username, username) or username


def _svc(wx: _AliasWx | None = None) -> WeChatService:
    svc = WeChatService(None, None)  # type: ignore[arg-type]
    if wx is not None:
        svc._wxdb = wx  # type: ignore[assignment]
    return svc


class TestGetWechatId:
    """alias 优先 → 备注/昵称 → 空串，绝不回退内部 wxid。"""

    def test_alias_wins(self):
        wx = _AliasWx(
            aliases={"wxid_a": "Xiangyanedu"},
            names={"wxid_a": "张鹏 大淼淼爸爸"})
        assert _svc(wx).get_wechat_id("wxid_a") == "Xiangyanedu"

    def test_fallback_to_remark_or_nickname(self):
        """alias 为空（大量账号没设微信号）时回退备注/昵称。"""
        wx = _AliasWx(aliases={"wxid_b": ""},
                      names={"wxid_b": "MiKE 淼淼英语老师"})
        assert _svc(wx).get_wechat_id("wxid_b") == "MiKE 淼淼英语老师"

    def test_wxid_never_leaks_as_wechat_id(self):
        """解析不到（昵称解析结果 == username）返回空串，不露 wxid。"""
        wx = _AliasWx(names={"wxid_raw": "wxid_raw"})
        assert _svc(wx).get_wechat_id("wxid_raw") == ""

    def test_group_chatroom_has_no_wechat_id(self):
        """群聊没有微信号；不能把群名塞进微信号位（会与显示名重复）。"""
        wx = _AliasWx(names={"123@chatroom": "某群"})
        assert _svc(wx).get_wechat_id("123@chatroom") == ""

    def test_empty_username_returns_empty(self):
        assert _svc(_AliasWx()).get_wechat_id("") == ""

    def test_no_wxdb_returns_empty(self):
        assert _svc().get_wechat_id("wxid_a") == ""

    def test_no_get_alias_method_falls_back(self):
        """老替身没实现 get_alias 时仍能回退昵称（不抛、不误伤）。"""
        class OldWx:
            @staticmethod
            def get_nickname(username: str) -> str:
                return "老朋友"

        svc = WeChatService(None, None)  # type: ignore[arg-type]
        svc._wxdb = OldWx()  # type: ignore[assignment]
        assert svc.get_wechat_id("wxid_old") == "老朋友"

    def test_read_error_falls_back(self):
        """contact.db 正忙（get_alias 抛异常）时回退昵称，不拖垮接口。"""
        class BrokenWx(_AliasWx):
            def get_alias(self, username: str) -> str:
                raise RuntimeError("contact.db 正被微信写入")

        wx = BrokenWx(names={"wxid_c": "回退昵称"})
        assert _svc(wx).get_wechat_id("wxid_c") == "回退昵称"

    def test_alias_cached(self):
        """同一 username 只读一次 alias（一页几十行逐条补全也可接受）。"""
        calls: list[str] = []

        class CountingWx(_AliasWx):
            def get_alias(self, username: str) -> str:
                calls.append(username)
                return "cached_alias"

        svc = _svc(CountingWx())
        assert svc.get_wechat_id("wxid_d") == "cached_alias"
        assert svc.get_wechat_id("wxid_d") == "cached_alias"
        assert calls == ["wxid_d"]


class TestBackendSchemas:
    """schema 里必须有微信号字段，且默认空串（向后兼容老数据/老调用）。"""

    def test_session_item_has_alias(self):
        item = SessionItemOut(username="wxid_a", display_name="张三",
                              kind="friend")
        assert item.alias == ""

    def test_monitor_target_has_alias(self):
        item = MonitorTargetOut(id=1, username="wxid_a",
                                display_name="张三", kind="friend", enabled=1)
        assert item.alias == ""

    def test_opportunity_has_session_and_sender_alias(self):
        item = OpportunityListItem(id=1)
        assert item.session_alias == ""
        assert item.sender_alias == ""

    def test_analysis_log_has_aliases(self):
        item = AnalysisLogOut(id=1)
        assert item.session_alias == ""
        assert item.sender_alias == ""

    def test_reply_target_and_group_member_have_alias(self):
        assert ReplyTargetOut().sender_alias == ""
        assert GroupMemberOut().alias == ""


class TestRouterEnrichment:
    """路由把微信号补进响应；读失败留空，不影响主流程。"""

    def test_opportunity_displays_include_alias(self):
        svc = _svc(_AliasWx(
            aliases={"wxid_room": "群主微信号", "wxid_sender": "sender_alias"},
            names={"wxid_room": "某群", "wxid_sender": "发送人"}))
        rows = [{"id": 1, "session_username": "wxid_room",
                 "session_name": "某群", "sender_wxid": "wxid_sender",
                 "sender_name": "发送人"}]
        out = _with_displays(rows, svc)[0]
        assert out["session_alias"] == "群主微信号"
        assert out["sender_alias"] == "sender_alias"
        # 显示名与微信号是两套字段，不能互相覆盖
        assert out["session_display"] == "某群"
        assert out["sender_display"] == "发送人"

    def test_opportunity_alias_read_failure_leaves_empty(self):
        """替身没有 get_wechat_id（降级）时补空串，商机行仍正常返回。"""
        out = _with_displays(
            [{"id": 1, "session_username": "wxid_a", "sender_wxid": "wxid_b"}],
            object())[0]
        assert out["session_alias"] == ""
        assert out["sender_alias"] == ""

    def test_monitor_target_alias_enrichment(self):
        svc = _svc(_AliasWx(aliases={"wxid_t": "target_alias"},
                            names={"wxid_t": "目标"}))
        out = _with_alias(
            {"id": 1, "username": "wxid_t", "display_name": "目标",
             "kind": "friend", "enabled": 1}, svc)
        assert out["alias"] == "target_alias"

    def test_analysis_log_alias_helper(self):
        svc = _svc(_AliasWx(aliases={"wxid_l": "log_alias"},
                            names={"wxid_l": "日志发送人"}))
        assert log_alias_of(svc, "wxid_l") == "log_alias"
        assert opp_alias_of(svc, "wxid_l") == "log_alias"
        assert log_alias_of(svc, "") == ""


class TestFrontBackFieldConsistency:
    """前端 types.ts 必须同步后端新增的展示字段（文本守门）。"""

    def test_types_ts_declares_alias_fields(self):
        # __file__ = .../kefu-system/tests/test_wechat_id_display.py
        kefu_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))  # .../kefu-system
        types_path = os.path.join(kefu_root, "frontend", "src", "api",
                                  "types.ts")
        with open(types_path, "r", encoding="utf-8") as f:
            text = f.read()
        for token in ("alias: string", "session_alias", "sender_alias"):
            assert token in text, f"前端 types.ts 缺少字段：{token}"
