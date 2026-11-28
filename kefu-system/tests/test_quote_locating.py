# -*- coding: utf-8 -*-
"""引用消息定位专项回归（2026-11-15 实测事故：回复时好时坏）。

事故复盘
--------
用户点「引用最新一条消息」后：第一次成功，第二、三次失败（滑块一直滚动
却不输入 / 「未找到「引用」菜单项」），改用纯文本或 @ 就恢复正常。

三条确定性原因（都不是随机）：
1. 微信 @ 消息里是 ``@昵称\\u20055``，而 ``_quote_snippet`` 用
   ``" ".join(text.split())`` 把 U+2005 折叠成普通空格；UIA 消息行 Name
   仍保留 U+2005，``text in name`` 永远 False → 引用目标找不到；
2. ``find_in_message_list`` 向历史最多滚 12 次且**失败后不复位**，聊天
   面板停在历史消息处 → 输入框探测失败、右键点错位置、污染下一次发送；
3. ``_find_row_by_text`` 把滚动命中的 rect 当控件传给 ``_right_click_row``
   （AttributeError 被吞），且 ``_right_click_row`` 返回 bool 却在上层被
   ``_set_cursor(*pos)`` 解包 → 即使匹配成功也走不通。

本文件锁死上述修复，外加「引用最新一条」意图不透传的回归。
"""
from __future__ import annotations

import os
import sys

# 仓库根（wechatauto 包父级）——与 test_uia_env.py 同一写法
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import pytest
from PIL import Image

from app.sender import SendTask, Sender
from wechatauto import uia_driver as uia_mod
from wechatauto.text_norm import contains_normalized, normalize_match_text
from wechatauto.uia_driver import WeChatUIA


# ---------------------------------------------------------------------------
# UIA 替身（不碰真实微信窗口）
# ---------------------------------------------------------------------------
class R:
    def __init__(self, left=100, top=200, right=300, bottom=260):
        self.left, self.top = left, top
        self.right, self.bottom = right, bottom


class Row:
    def __init__(self, name, rect=None, cls="mmui::ChatBubbleItemView"):
        self.Name = name
        self.ClassName = cls
        self.BoundingRectangle = rect or R()


class Lst:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.BoundingRectangle = R(0, 0, 800, 600)

    def GetChildren(self):
        return list(self._rows)


def _fake_grab(bbox=None):
    """返回纯白图，避免测试里真的截屏。"""
    w = max(1, bbox[2] - bbox[0]) if bbox else 10
    h = max(1, bbox[3] - bbox[1]) if bbox else 10
    return Image.new("RGB", (w, h), "white")


@pytest.fixture
def uia():
    return object.__new__(WeChatUIA)


def _task(**kw):
    base = dict(opportunity_id=1, session_username="111@chatroom",
                session_name="家人群", text="报价稍后发您")
    base.update(kw)
    return SendTask(**base)


# ---------------------------------------------------------------------------
# 1. Unicode 空白归一化
# ---------------------------------------------------------------------------
class TestTextNorm:
    def test_at_message_u2005_matches_plain_space(self):
        """本机家人群实测原文 '@诸神黄昏\\u20055' 与清洗后片段必须互相匹配。"""
        db_content = "@诸神黄昏\u20055"
        snippet = "@诸神黄昏 5"          # _quote_snippet 折叠后的形式
        assert db_content != snippet
        assert normalize_match_text(db_content) == normalize_match_text(snippet)
        assert contains_normalized(snippet, db_content)
        assert contains_normalized(db_content, snippet)

    def test_other_unicode_spaces_fold_too(self):
        assert contains_normalized("刹车片 报价", "刹车片\u00a0\u3000报价")

    def test_empty_needle_never_matches(self):
        assert not contains_normalized("", "任意文本")
        assert not contains_normalized("  \u2005 ", "任意文本")
        assert normalize_match_text(None) == ""


# ---------------------------------------------------------------------------
# 2. UIA 引用定位
# ---------------------------------------------------------------------------
class TestFindRowByText:
    def test_visible_row_with_u2005_matches_and_returns_coord(self, uia, monkeypatch):
        """可视区命中：归一化匹配生效，且返回右键坐标（不是控件/bool）。"""
        monkeypatch.setattr(uia, "_message_list",
                            lambda: Lst([Row("@诸神黄昏\u20055")]))
        monkeypatch.setattr(uia, "_right_click_row", lambda row: (210, 230))
        assert uia._find_row_by_text("@诸神黄昏 5") == (210, 230)

    def test_empty_target_skips_lookup(self, uia, monkeypatch):
        called = []
        monkeypatch.setattr(uia, "_message_list",
                            lambda: called.append(1) or Lst([]))
        assert uia._find_row_by_text("   ") is None
        assert called == []

    def test_scroll_hit_passes_rect_to_right_click(self, uia, monkeypatch):
        """滚动命中的是 rect 而非控件：_right_click_row 必须能接，且返回坐标。"""
        monkeypatch.setattr(uia, "_message_list", lambda: Lst([]))
        rect = R(120, 300, 320, 360)
        monkeypatch.setattr(
            uia, "find_in_message_list",
            lambda *a, **k: ("mmui::ChatBubbleItemView", "@诸神黄昏\u20055", rect))
        seen = []
        monkeypatch.setattr(uia, "_right_click_row",
                            lambda row: seen.append(row) or (220, 330))
        assert uia._find_row_by_text("@诸神黄昏 5") == (220, 330)
        assert len(seen) == 1 and hasattr(seen[0], "right")


class TestRightClickRow:
    def test_accepts_plain_rect_and_returns_coord(self, uia, monkeypatch):
        """旧实现 row.BoundingRectangle → AttributeError 被吞；bool 又不可解包。"""
        monkeypatch.setattr("PIL.ImageGrab.grab", _fake_grab)
        monkeypatch.setattr(uia, "_set_cursor", lambda x, y: None)
        monkeypatch.setattr(uia, "_right_click", lambda: None)
        assert uia._right_click_row(R(100, 200, 300, 260)) == (200, 230)

    def test_accepts_uia_control(self, uia, monkeypatch):
        monkeypatch.setattr("PIL.ImageGrab.grab", _fake_grab)
        monkeypatch.setattr(uia, "_set_cursor", lambda x, y: None)
        monkeypatch.setattr(uia, "_right_click", lambda: None)
        assert uia._right_click_row(Row("任意")) == (200, 230)

    def test_too_small_rect_returns_none(self, uia, monkeypatch):
        monkeypatch.setattr(uia, "_set_cursor", lambda x, y: None)
        assert uia._right_click_row(R(0, 0, 3, 3)) is None


class TestLatestRowRetry:
    """「引用最新一条」读消息列表：窗口刚从托盘/最小化恢复时树未物化，
    一次读空就放弃会让整条发送白丢（2026-11-28 实测 1.3s 快速失败）。"""

    def _prep(self, uia, monkeypatch):
        monkeypatch.setattr(uia_mod, "_LATEST_ROW_WAIT", 0)   # 测试不真等
        monkeypatch.setattr(uia, "ensure_window", lambda: True)
        clicked = []
        monkeypatch.setattr(uia, "_set_cursor", lambda x, y: None)
        monkeypatch.setattr(uia, "_right_click", lambda: clicked.append(1))
        monkeypatch.setattr("PIL.ImageGrab.grab", _fake_grab)
        return clicked

    def test_empty_list_retries_then_fails_closed(self, uia, monkeypatch):
        """一直读空：重试到上限后仍返回 None，绝不右键、也不降级成直接发。"""
        clicked = self._prep(uia, monkeypatch)
        reads = []
        monkeypatch.setattr(uia, "_message_list",
                            lambda: reads.append(1) or None)
        assert uia._right_click_latest_row(None) is None
        assert len(reads) == uia_mod._LATEST_ROW_ATTEMPTS
        assert clicked == []

    def test_recovers_when_tree_materializes(self, uia, monkeypatch):
        """第二次读到消息行就立刻右键，不再多轮询。"""
        clicked = self._prep(uia, monkeypatch)
        lists = [None, Lst([Row("图片消息")]), "不应再读"]
        it = iter(lists)
        monkeypatch.setattr(uia, "_message_list", lambda: next(it))
        assert uia._right_click_latest_row(None) is not None
        assert clicked == [1]
        assert next(it, None) == "不应再读"

    def test_picks_bottom_row_and_skips_separators(self, uia, monkeypatch):
        """抽出 _latest_message_row 后筛选规则不变：跳过时间行/系统行/矮占位行。"""
        rows = [Row("时间", R(100, 100, 300, 130), cls="mmui::ChatItemView"),
                Row("系统", R(100, 140, 300, 170),
                    cls="mmui::ChatSystemInfoItemView"),
                Row("未布局", R(100, 180, 300, 190)),
                Row("上一条", R(100, 200, 300, 260)),
                Row("最新条", R(100, 300, 300, 380))]
        monkeypatch.setattr(uia, "_message_list", lambda: Lst(rows))
        ctrl, rect = uia._latest_message_row()
        assert ctrl is rows[-1] and ctrl.Name == "最新条"
        assert rect.top == 300


# ---------------------------------------------------------------------------
# 3. 消息列表滚动复位
# ---------------------------------------------------------------------------
class TestScrollRestore:
    def test_miss_scrolls_back_to_latest(self, uia, monkeypatch):
        """找不到目标时必须滚回最新，不能把面板留在历史消息处。"""
        monkeypatch.setattr(uia, "_message_list", lambda: Lst([]))
        monkeypatch.setattr(uia, "_set_cursor", lambda x, y: None)
        wheels = []
        monkeypatch.setattr(uia, "_mouse_wheel", lambda d: wheels.append(d))
        assert uia.find_in_message_list(lambda cn, nm: False,
                                        max_scrolls=1) is None
        assert wheels.count(120) == 2                 # 滚向历史 2 次
        assert wheels.count(-120) == 4               # 复位 2+2 次
        assert wheels[-1] == -120

    def test_hit_does_not_scroll_back(self, uia, monkeypatch):
        """命中时右键菜单已弹出，滚轮会把菜单关掉 → 不得复位。"""
        monkeypatch.setattr(uia, "_message_list",
                            lambda: Lst([Row("目标消息")]))
        wheels = []
        monkeypatch.setattr(uia, "_mouse_wheel", lambda d: wheels.append(d))
        got = uia.find_in_message_list(
            lambda cn, nm: "目标消息" in nm, max_scrolls=1)
        assert got is not None
        assert wheels == []

    def test_restore_can_be_disabled(self, uia, monkeypatch):
        monkeypatch.setattr(uia, "_message_list", lambda: Lst([]))
        monkeypatch.setattr(uia, "_set_cursor", lambda x, y: None)
        wheels = []
        monkeypatch.setattr(uia, "_mouse_wheel", lambda d: wheels.append(d))
        uia.find_in_message_list(lambda cn, nm: False, max_scrolls=1,
                                 restore_on_miss=False)
        assert -120 not in wheels


class TestDismissQuoteMenu:
    def test_esc_then_scroll_to_latest(self, uia, monkeypatch):
        calls = []
        monkeypatch.setattr(uia, "_key_esc", lambda: calls.append("esc"))
        monkeypatch.setattr(uia, "_message_list", lambda: Lst([]))
        monkeypatch.setattr(uia, "_set_cursor", lambda x, y: None)
        monkeypatch.setattr(uia, "_mouse_wheel", lambda d: calls.append(d))
        uia.dismiss_quote_menu()
        assert calls[0] == "esc"
        assert all(d == -120 for d in calls[1:])
        assert len(calls) == 17


# ---------------------------------------------------------------------------
# 4. 「引用最新一条」意图
# ---------------------------------------------------------------------------
class TestQuoteLatestIntent:
    def test_latest_skips_text_lookup_even_for_image(self):
        """图片消息没有可识别文字，latest 意图也不该抛 QuoteTargetUnavailable。"""
        s = object.__new__(Sender)
        s._message_lookup = lambda u, i: {"content": "[图片]"}
        s._latest_lookup = lambda u: 99
        task = _task(quote_local_id=5, quote_latest=True)
        assert s._quote_target_text(task) is None

    def test_latest_wins_when_target_is_no_longer_latest(self):
        """自己刚发的回复变成最新时，固定 local_id 会退化；latest 不会。"""
        s = object.__new__(Sender)
        s._message_lookup = lambda u, i: {"content": "@诸神黄昏\u20055"}
        s._latest_lookup = lambda u: 99
        assert s._quote_target_text(_task(quote_local_id=5)) == "@诸神黄昏 5"
        assert s._quote_target_text(
            _task(quote_local_id=5, quote_latest=True)) is None

    def test_explicit_quote_keeps_precise_snippet(self):
        """点选具体某条上下文消息，仍按文本精确定位，绝不默默引用最新。"""
        s = object.__new__(Sender)
        s._message_lookup = lambda u, i: {"content": "张三:\n上周的报价单"}
        s._latest_lookup = lambda u: 99
        assert s._quote_target_text(
            _task(quote_local_id=5)) == "上周的报价单"
