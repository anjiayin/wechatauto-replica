# -*- coding: utf-8 -*-
"""「关到系统托盘」发送安全回归（2026-11-15 11:33~11:35 实测事故）。

事故复盘
--------
用户前三次发送正常，点微信右上角关闭按钮把微信收进系统托盘后再发：
- 11:33:44 `微信主窗口关闭到托盘（隐藏），已自动恢复可见` —— 代码用
  Win32 ``SW_SHOW`` 强行显示 Qt 自己 hide() 的窗口；Qt 内部仍是托盘态，
  UIA 树死活不物化，窗口被卡成 ``visible=True + iconic=True`` 的
  -32000 伪矩形 → 用户看到「界面不能动」。
- 11:33:28 / 11:33:49(rearm 1→0→1，2 次写) / 11:34:57 同一 PID 连写
  6 次 gate byte → 11:35:20「已热激活 5 次仍反复回收，建议重启微信」，
  Qt 输入系统被写挂。
- 11:34:02 唤醒等 16s 超时才 fail-closed（用户贴的 18.3s），11:35:28
  后台预热巡检又白等 31s。

修复后的硬约束（本文件锁死）
----------------------------
- 关到托盘 = ``IsWindowVisible=False``（Qt hide），与 Win32 最小化
  （``IsIconic=True`` 但 visible 仍为 True）**必须区分**。
- 托盘态：不 ShowWindow / 不写 gate byte / 不等待 / 不置前 / 不重绑，
  0 秒 fail-closed，提示用户去点托盘图标。
- 真最小化仍走 SW_RESTORE + 热激活（实测有效，不能一起禁掉）。
- 后台预热巡检在托盘态静默跳过，绝不调 warm_up。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from wechatauto import guia as guia_mod
from wechatauto import uia_driver as drv
from wechatauto.guia import WeChatGUI
from wechatauto.uia_driver import WeChatUIA
from app.sender import Sender


class _User32:
    """只实现 ensure_visible 用到的 Win32 查询，记录所有 ShowWindow。"""

    def __init__(self, visible=0, iconic=0, alive=1):
        self.visible = visible
        self.iconic = iconic
        self.alive = alive
        self.calls = []

    def IsWindowVisible(self, hwnd):
        return self.visible

    def IsIconic(self, hwnd):
        return self.iconic

    def IsWindow(self, hwnd):
        return self.alive

    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("ShowWindow", hwnd, cmd))

    def GetForegroundWindow(self):
        return 0


def _gui(user32):
    g = object.__new__(WeChatGUI)
    g._input = SimpleNamespace(_user32=user32)
    g.main_hwnd = 2294954
    g.render_hwnd = 111
    g._last_open_error = ''
    g._last_visible_ok = False
    g._last_visible_ts = 0
    return g


# ---------------------------------------------------------------------------
# 1. WeChatGUI：托盘态识别与 ensure_visible 短路
# ---------------------------------------------------------------------------
class TestTrayHiddenGuia:
    @pytest.mark.parametrize("visible,iconic,expected", [
        (0, 0, True),      # 关到托盘：Qt hide
        (0, 1, True),      # 托盘 + 残留 iconic 标志，仍按托盘处理
        (1, 1, False),     # 真最小化：visible 仍为 True
        (1, 0, False),     # 正常可见
    ])
    def test_is_tray_hidden_distinguishes_from_minimized(
            self, visible, iconic, expected):
        g = _gui(_User32(visible=visible, iconic=iconic))
        assert g.is_tray_hidden() is expected

    def test_is_tray_hidden_dead_handle_returns_false(self):
        g = _gui(_User32(visible=0, alive=0))
        assert g.is_tray_hidden() is False

    def test_ensure_visible_tray_short_circuits_everything(self, monkeypatch):
        """托盘态：0 秒失败，一行窗口操作都不做。"""
        u = _User32(visible=0, iconic=0)
        g = _gui(u)
        touched = []
        for name in ("_minimize_blockers", "bring_to_front",
                     "_update_render_rect", "_rebind_if_window_changed",
                     "is_alive"):
            monkeypatch.setattr(
                g, name,
                lambda *a, _n=name, **k: touched.append(_n) or True)
        monkeypatch.setattr(
            guia_mod, "_restore_keep_maximize",
            lambda *a, **k: touched.append("restore"))
        assert g.ensure_visible() is False
        assert u.calls == [], "托盘态绝不能 SW_SHOW（会把窗口卡成伪矩形）"
        assert touched == [], "托盘态绝不能置前/重绑/探测遮挡窗口"
        assert g.last_open_error() == "tray_hidden"
        assert g._last_visible_ok is False

    def test_real_minimization_still_restores_and_activates(self, monkeypatch):
        """真最小化仍恢复：SW_RESTORE 有效，不能和托盘一起禁掉。"""
        u = _User32(visible=1, iconic=1)
        g = _gui(u)
        touched = []

        def _restore(user32, hwnd):
            touched.append("restore")
            u.iconic = 0            # 模拟 SW_RESTORE 生效
        monkeypatch.setattr(guia_mod, "_restore_keep_maximize", _restore)
        monkeypatch.setattr(guia_mod.time, "sleep", lambda s: None)
        for name in ("_minimize_blockers", "bring_to_front",
                     "_update_render_rect"):
            monkeypatch.setattr(
                g, name,
                lambda *a, _n=name, **k: touched.append(_n) or True)
        monkeypatch.setattr(g, "_rebind_if_window_changed", lambda: False)
        monkeypatch.setattr(g, "is_alive", lambda: True)
        assert g.ensure_visible() is True
        assert "restore" in touched
        assert "bring_to_front" in touched
        assert g.last_open_error() == ''

    def test_reopening_from_tray_invalidates_stale_caches(self, monkeypatch):
        """托盘→用户点开图标：缓存的引擎/会话/矩形/句柄必须全部作废。"""
        u = _User32(visible=0, iconic=0)
        g = _gui(u)
        touched = []
        monkeypatch.setattr(guia_mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(g, "_minimize_blockers",
                            lambda: touched.append("blockers"))
        monkeypatch.setattr(
            g, "bring_to_front",
            lambda keep_topmost=False: touched.append("front") or True)
        monkeypatch.setattr(g, "_update_render_rect",
                            lambda: touched.append("rect"))
        monkeypatch.setattr(g, "is_alive", lambda: True)
        monkeypatch.setattr(
            g, "_rebind_if_window_changed",
            lambda: touched.append("rebind") or False)
        monkeypatch.setattr(guia_mod, "_restore_keep_maximize",
                            lambda *a, **k: touched.append("restore"))

        assert g.ensure_visible() is False       # 托盘：快速失败
        assert g._was_tray_hidden is True

        # 用户点开托盘图标，窗口恢复为正常可见
        u.visible, u.iconic = 1, 0
        g._uia = object()
        g._uia_tried = True
        g._uia_retry_after = 999.0
        g._current_chat = "家人群"
        g._last_input_box = (1, 2, 3, 4)
        assert g.ensure_visible() is True
        assert g._was_tray_hidden is False
        assert g._current_chat is None
        assert g._last_input_box is None
        assert g._uia is None
        assert g._uia_tried is False
        assert g._uia_retry_after == 0
        assert "rebind" in touched

    def test_warm_up_skips_tray_without_touching_uia(self, monkeypatch):
        g = _gui(_User32(visible=0))
        g._uia = None
        monkeypatch.setattr(g, "is_tray_hidden", lambda: True)
        monkeypatch.setattr(WeChatUIA, "warm_tree",
                            lambda *a, **k: pytest.fail("托盘态不应预热"))
        assert g.warm_up(timeout=30) is False


# ---------------------------------------------------------------------------
# 2. uia_driver：托盘态绝不恢复窗口、绝不写 gate byte
# ---------------------------------------------------------------------------
def _fake_enum(monkeypatch, states):
    """states: [(hwnd, title, visible, iconic)]，返回 ShowWindow 调用记录。"""
    calls = []
    by_hwnd = {h: (t, v, i) for h, t, v, i in states}
    monkeypatch.setattr(drv, "_HAS_WIN32", True)
    monkeypatch.setattr(drv.win32gui, "GetWindowText",
                        lambda h: by_hwnd[h][0])
    monkeypatch.setattr(drv.win32gui, "IsWindowVisible",
                        lambda h: by_hwnd[h][1])
    monkeypatch.setattr(drv.win32gui, "IsIconic",
                        lambda h: by_hwnd[h][2])
    def _show(h, c):
        calls.append((h, c))
        if c == 9:                       # SW_RESTORE 生效：iconic 归零
            by_hwnd[h] = (by_hwnd[h][0], True, False)
    monkeypatch.setattr(drv.win32gui, "ShowWindow", _show)
    monkeypatch.setattr(drv.win32gui, "EnumWindows",
                        lambda cb, _: [cb(h, None) for h in by_hwnd])
    return calls


class TestTrayHiddenDriver:
    @pytest.mark.parametrize("states,expect_ok,expect_calls,expect_flag", [
        ([(1, "微信", 0, 0)], False, [], True),         # 托盘：不碰
        ([(1, "微信", 1, 1)], True, [(1, 9)], False),   # 最小化：SW_RESTORE
        ([(1, "微信", 1, 0)], True, [], False),         # 正常：不动
    ])
    def test_restore_window_visible(self, monkeypatch, states, expect_ok,
                                    expect_calls, expect_flag):
        calls = _fake_enum(monkeypatch, states)
        uia = object.__new__(WeChatUIA)
        assert uia._restore_window_visible() is expect_ok
        assert calls == expect_calls
        assert uia._tray_hidden_detected is expect_flag

    def test_wake_wait_ready_tray_fails_without_any_patch(self, monkeypatch):
        uia = object.__new__(WeChatUIA)
        uia._tray_hidden_detected = True
        monkeypatch.setattr(uia, "_restore_window_visible", lambda: False)
        monkeypatch.setattr(
            uia, "_wake_accessibility",
            lambda *a, **k: pytest.fail("托盘态不应打补丁（含 rearm）"))
        monkeypatch.setattr(
            uia, "_find_main",
            lambda *a, **k: pytest.fail("托盘态不应等控件树"))
        assert uia._wake_and_wait_ready(timeout=30) is False

    def test_ensure_window_tray_skips_wake_branch(self, monkeypatch):
        uia = object.__new__(WeChatUIA)
        uia.timeout = 30.0
        monkeypatch.setattr(uia, "_find_main", lambda: None)
        monkeypatch.setattr(uia, "_restore_window_visible",
                            lambda: setattr(uia, "_tray_hidden_detected", True)
                            or False)
        for name in ("_login_window", "_wake_accessibility", "_wait_main",
                     "_set_screen_reader_flag", "wake"):
            monkeypatch.setattr(
                uia, name,
                lambda *a, _n=name, **k: pytest.fail(f"托盘态不应调用 {_n}"))
        assert uia.ensure_window() is False

    def test_rearm_blocked_shortly_after_real_patch(self, monkeypatch):
        """距任何一次真实补丁不足 45s 时禁止 1→0→1 脉冲。"""
        uia = object.__new__(WeChatUIA)
        monkeypatch.setattr(WeChatUIA, "_rearm_log", {})
        monkeypatch.setattr(WeChatUIA, "_patch_log", {10856: [1, 1000.0]})
        monkeypatch.setattr(uia, "_wechat_hwnds", lambda: [111])
        monkeypatch.setattr(uia, "_pid_from_hwnd", lambda h: 10856)
        clock = {"t": 1010.0}
        monkeypatch.setattr(drv.time, "monotonic", lambda: clock["t"])
        assert uia._rearm_allowed() is False
        clock["t"] = 1046.0
        assert uia._rearm_allowed() is True


# ---------------------------------------------------------------------------
# 3. sender：后台预热静默跳过 + 托盘专属可操作提示
# ---------------------------------------------------------------------------
class TestTrayHiddenSender:
    def test_prewarm_once_skips_tray_without_warm_up(self, monkeypatch):
        st = object.__new__(Sender)
        gui = SimpleNamespace(
            is_alive=lambda: True,
            is_tray_hidden=lambda: True,
            _uia=None,
            warm_up=lambda *a, **k: pytest.fail("托盘态不应预热"))
        st._ensure_gui = lambda quiet=False: gui
        assert st._prewarm_once() == "no_wechat"

    def test_prewarm_once_still_warms_normal_window(self, monkeypatch):
        st = object.__new__(Sender)
        warmed = []
        gui = SimpleNamespace(
            is_alive=lambda: True,
            is_tray_hidden=lambda: False,
            _uia=None,
            warm_up=lambda timeout=30.0: warmed.append(timeout) or True)
        st._ensure_gui = lambda quiet=False: gui
        st._gui_lock = SimpleNamespace(acquire=lambda timeout=0.5: True,
                                       release=lambda: None)
        assert st._prewarm_once() == "warmed"
        assert warmed == [30.0]

    def test_tray_failure_message_is_actionable(self):
        tray_gui = SimpleNamespace(last_open_error=lambda: "tray_hidden",
                                   is_tray_hidden=lambda: True)
        msg = Sender._window_not_visible_message(tray_gui)
        assert "托盘" in msg and "微信图标" in msg and "已取消发送" in msg

    def test_other_invisible_message_unchanged(self):
        gui = SimpleNamespace(last_open_error=lambda: "",
                              is_tray_hidden=lambda: False)
        assert "遮挡" in Sender._window_not_visible_message(gui)
