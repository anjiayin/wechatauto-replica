# -*- coding: utf-8 -*-
"""「引用 + @」组合回复的组件层回归（2026-11-15 实测事故）。

事故复盘
--------
同时勾选「@发送人」和「引用最新一条消息」时，只发出了 @，引用被静默丢弃，
界面仍提示成功。根因是 ``SenderThread._do_send`` 用 if/return 互斥分支，
@ 分支先返回；修复后引用意图与 @ 意图一起进入同一条草稿。

UiA 侧的硬约束：引用卡片建立后**绝不能**再 Ctrl+A/Delete，否则引用块被
删掉、消息退化成普通 @ 消息。本文件锁死这个顺序。
"""
from __future__ import annotations

import os
import sys

# 仓库根（wechatauto 包父级）——与 test_uia_env.py 同一写法
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import pytest

from wechatauto.guia import WeChatGUI
from wechatauto.uia_driver import WeChatUIA


class _Edit:
    """只记录按键的输入框替身。"""

    def __init__(self):
        self.keys = []
        self.clicks = 0

    def SendKeys(self, keys, waitTime=0):
        self.keys.append(keys)

    def Click(self):
        self.clicks += 1


@pytest.fixture
def uia():
    return object.__new__(WeChatUIA)


@pytest.fixture
def gui():
    return object.__new__(WeChatGUI)


class TestUIAAtQuoteCombo:
    def _wire(self, uia, monkeypatch, quote_calls):
        edit = _Edit()
        monkeypatch.setattr(uia, "ensure_window", lambda: True)
        monkeypatch.setattr(uia, "same_chat", lambda a, b: True)
        monkeypatch.setattr(uia, "current_chat", lambda: "家人群")
        monkeypatch.setattr(uia, "_chat_input", lambda: edit)
        monkeypatch.setattr(
            uia, "_prepare_quote",
            lambda t: quote_calls.append(t) or edit)
        monkeypatch.setattr(uia, "_pick_mention",
                            lambda name, timeout=2.0: True)
        monkeypatch.setattr(uia, "_paste_append", lambda e, t: True)
        monkeypatch.setattr("wechatauto.uia_driver.time.sleep",
                            lambda s: None)
        return edit

    def test_combo_prepares_quote_then_ats_without_clearing(
            self, uia, monkeypatch):
        """引用卡片建立后不清草稿，@ 逐个进同一条，只回车一次。"""
        quote_calls = []
        edit = self._wire(uia, monkeypatch, quote_calls)

        assert uia.at_members(["张三", "李四"], "收到", who="家人群",
                              quote=True, quote_target_text=None) is True

        assert quote_calls == [None]                     # 引用最新一条
        assert not any("Ctrl" in k for k in edit.keys)  # 不能删引用卡片
        assert edit.keys.count("@") == 2
        assert edit.keys[-1] == "{Enter}"                # 只发一条消息

    def test_combo_with_target_text_is_forwarded(self, uia, monkeypatch):
        quote_calls = []
        self._wire(uia, monkeypatch, quote_calls)

        assert uia.at_members(["张三"], "收到", quote=True,
                              quote_target_text="上周的报价单") is True
        assert quote_calls == ["上周的报价单"]

    def test_plain_at_still_clears_draft(self, uia, monkeypatch):
        """不带引用时保持原有清草稿行为（防上次失败残留）。"""
        quote_calls = []
        edit = self._wire(uia, monkeypatch, quote_calls)

        assert uia.at_members(["张三"], "收到") is True
        assert quote_calls == []
        assert any("Ctrl" in k for k in edit.keys)

    def test_quote_message_reuses_prepare(self, uia, monkeypatch):
        """纯引用路径与组合路径共用同一份引用准备逻辑。"""
        quote_calls = []
        edit = self._wire(uia, monkeypatch, quote_calls)

        assert uia.quote_message("收到", target_text="原文") is True
        assert quote_calls == ["原文"]
        assert not any("Ctrl" in k for k in edit.keys)
        assert edit.keys[-1] == "{Enter}"


class TestGuiAtQuoteCombo:
    def test_uia_flags_are_forwarded(self, gui, monkeypatch):
        seen = []

        class FakeUia:
            def current_chat(self):
                return "家人群"

            def same_chat(self, cur, target):
                return cur == target

            def at_members(self, names, text, who=None, quote=False,
                           quote_target_text=None):
                seen.append((names, text, who, quote, quote_target_text))
                return True

        monkeypatch.setattr(gui, "_get_uia", lambda: FakeUia())
        resp = gui.at_members(["张三"], "收到", who="家人群", verify=False,
                              quote=True, quote_target_text="原文")

        assert resp.is_success is True
        assert seen == [(["张三"], "收到", "家人群", True, "原文")]
        assert resp.get("data", {}).get("chat_verified") is True

    def test_uia_unavailable_fails_loudly(self, gui, monkeypatch):
        """UIA 不可用时必须明确失败，不能静默退化成「只发 @」。"""
        monkeypatch.setattr(gui, "_get_uia", lambda: None)
        resp = gui.at_members(["张三"], "收到", quote=True)

        assert resp.is_success is False
        # UIA 不可用一律 fail-closed，不再静默发出「只有 @ 没有引用」
        assert "阻止发送" in str(resp.get("message", ""))
        assert resp.get("data", {}).get("chat_verified") is False
