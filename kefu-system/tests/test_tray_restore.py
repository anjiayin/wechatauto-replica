# -*- coding: utf-8 -*-
"""托盘态「自动点击图标唤起微信」回归（2026-11-15 18:56 实机验证）。

实测结论（勿推翻）：
* ``SW_SHOW`` 对 Qt hide() 的托盘窗口无效且有害（-32000 伪矩形）；
* 再启动一次 ``Weixin.exe`` 不是唤起，会开新 PID 的新窗口后自行退出；
* **单击托盘「微信」图标**能让 Qt 自己 show：主窗 592108 由
  visible=False 变 True、iconic=False、rect 不变、PID 不变。

本文件全部打桩，不碰真实桌面（conftest 默认把唤起函数隔离成失败，
需要测真实逻辑的用例在这里换回 ``_REAL_RESTORE``）。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import wechatauto.tray_restore as tr
from wechatauto import guia as guia_mod
from wechatauto.guia import WeChatGUI

_REAL_RESTORE = tr.restore_wechat_from_tray   # fixture 执行前保存真实实现

MAIN_HWND = 592108
SHELL_HWND = 65706
OVERFLOW_HWND = 65758
ICON_BOX = (1628, 1000, 1668, 1040)          # 实测「微信」托盘图标
ICON_CENTER = (1648, 1020)
CHEVRON_BOX = (1676, 1040, 1700, 1080)        # 实测「通知 V 形」
CHEVRON_CENTER = (1688, 1060)


class _Rect:
    def __init__(self, box):
        self.left, self.top, self.right, self.bottom = box


class _Ctrl:
    def __init__(self, name="", aid="", box=(0, 0, 10, 10), children=()):
        self.Name = name
        self.AutomationId = aid
        self.BoundingRectangle = _Rect(box)
        self._children = list(children)

    def GetChildren(self):
        return list(self._children)


class _Win32Gui:
    def __init__(self, visible_map, iconic_map=None):
        self.visible = dict(visible_map)
        self.iconic = dict(iconic_map or {})
        self.classes = {
            SHELL_HWND: "Shell_TrayWnd",
            OVERFLOW_HWND: "NotifyIconOverflowWindow",
            MAIN_HWND: "Qt51514QWindowIcon",
        }

    def EnumWindows(self, cb, _):
        for h in self.classes:
            cb(h, None)
        return True

    def GetClassName(self, h):
        return self.classes.get(h, "")

    def IsWindowVisible(self, h):
        return bool(self.visible.get(h, False))

    def IsIconic(self, h):
        return bool(self.iconic.get(h, False))

    def ShowWindow(self, h, cmd):                # 铁律：绝不 SW_SHOW
        raise AssertionError("托盘唤起路径绝不能调用 ShowWindow")


class _Clock:
    """假时钟：sleep 推进时间，让「轮询等待溢出面板」的测试不再真等。"""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _Win32Api:
    def __init__(self, on_click=None):
        self.cursor = (1735, 136)
        self.clicks = []
        self._on_click = on_click

    def GetCursorPos(self):
        return self.cursor

    def SetCursorPos(self, pos):
        self.cursor = tuple(pos)

    def mouse_event(self, flags, *_):
        if flags == 2:                          # MOUSEEVENTF_LEFTDOWN
            self.clicks.append(self.cursor)
            if self._on_click:
                self._on_click(*self.cursor)


class _Auto:
    def __init__(self, roots):
        self.roots = roots

    def ControlFromHandle(self, h):
        return self.roots.get(h)


def _install(monkeypatch, *, shell_children=(), overflow_children=(),
             show_on_click=True, show_on_double=False, main_visible=False,
             has_chevron=True, has_uia=True):
    """装配托盘 UIA / Win32 桩，返回可断言的环境对象。"""
    wg = _Win32Gui(
        {SHELL_HWND: True, OVERFLOW_HWND: False, MAIN_HWND: main_visible})
    icon_hits = [0]

    def on_click(x, y):
        if has_chevron and (x, y) == CHEVRON_CENTER:
            wg.visible[OVERFLOW_HWND] = not wg.visible[OVERFLOW_HWND]
        elif (x, y) == ICON_CENTER:
            icon_hits[0] += 1
            # show_on_double=False/True 模拟「单击无效、双击才唤起」的桌面
            if show_on_click or (show_on_double and icon_hits[0] >= 3):
                wg.visible[MAIN_HWND] = True   # 模拟 Qt 自己 show

    api = _Win32Api(on_click)
    chevron = _Ctrl("通知 V 形", "1502", CHEVRON_BOX) if has_chevron else None
    shell_kids = ([chevron] if chevron else []) + list(shell_children)
    roots = {
        SHELL_HWND: _Ctrl(children=shell_kids),
        OVERFLOW_HWND: _Ctrl(children=list(overflow_children)),
    }
    monkeypatch.setattr(tr, "_HAS_UIA", has_uia)
    monkeypatch.setattr(tr, "win32gui", wg)
    monkeypatch.setattr(tr, "win32api", api)
    monkeypatch.setattr(tr, "win32con",
                        SimpleNamespace(MOUSEEVENTF_LEFTDOWN=2,
                                        MOUSEEVENTF_LEFTUP=4))
    monkeypatch.setattr(tr, "auto", _Auto(roots))
    monkeypatch.setattr(tr, "time", _Clock())
    monkeypatch.setattr(tr, "restore_wechat_from_tray", _REAL_RESTORE)
    return SimpleNamespace(wg=wg, api=api, chevron=chevron)


def _icon_ctrl(name="微信"):
    return _Ctrl(name, "", ICON_BOX)


# ---------------------------------------------------------------------------
# 1. tray_restore 模块本身
# ---------------------------------------------------------------------------
class TestTrayRestoreModule:
    def test_icon_in_visible_area_clicks_icon_only(self, monkeypatch):
        env = _install(monkeypatch, shell_children=[_Ctrl("扬声器"), _icon_ctrl()])
        assert tr.restore_wechat_from_tray(MAIN_HWND, timeout=0.5) is True
        assert env.api.clicks == [ICON_CENTER]
        assert env.wg.visible[OVERFLOW_HWND] is False   # 没展开溢出区
        assert env.api.cursor == (1735, 136)            # 鼠标还给用户

    def test_icon_in_overflow_expands_clicks_and_collapses(self, monkeypatch):
        env = _install(monkeypatch,
                       overflow_children=[_Ctrl("Proxifier"), _icon_ctrl()])
        assert tr.restore_wechat_from_tray(MAIN_HWND, timeout=0.5) is True
        assert env.api.clicks == [CHEVRON_CENTER, ICON_CENTER, CHEVRON_CENTER]
        assert env.wg.visible[OVERFLOW_HWND] is False   # 已收起
        assert env.api.cursor == (1735, 136)

    def test_no_icon_never_clicks_other_tray_icons(self, monkeypatch):
        env = _install(monkeypatch,
                       shell_children=[_Ctrl("扬声器")],
                       overflow_children=[_Ctrl("Proxifier"),
                                          _Ctrl("Realtek高清晰音频管理器")])
        assert tr.restore_wechat_from_tray(MAIN_HWND, timeout=0.5) is False
        assert ICON_CENTER not in env.api.clicks
        assert set(env.api.clicks) <= {CHEVRON_CENTER}  # 只有展开/收起
        assert env.api.cursor == (1735, 136)

    @pytest.mark.parametrize("name", [
        "微信 - 1 个运行窗口",     # 任务栏按钮，绝不能点
        "微信1",
        "Weixin 助手",
    ])
    def test_name_match_is_exact(self, monkeypatch, name):
        env = _install(monkeypatch, shell_children=[_Ctrl(name)])
        assert tr.restore_wechat_from_tray(MAIN_HWND, timeout=0.5) is False
        assert ICON_CENTER not in env.api.clicks

    def test_click_but_window_stays_hidden_times_out(self, monkeypatch):
        env = _install(monkeypatch, shell_children=[_icon_ctrl()],
                       show_on_click=False)
        assert tr.restore_wechat_from_tray(MAIN_HWND, timeout=0.01) is False
        # 单击 1 次 + 补发双击 2 次，都没有唤起 → 仍然 fail-closed
        assert env.api.clicks == [ICON_CENTER] * 3
        assert env.api.cursor == (1735, 136)

    def test_double_click_fallback_when_single_click_ignored(self, monkeypatch):
        """19:03 实测：单击 3 秒无反应、双击 0.5 秒唤起，必须补双击。"""
        env = _install(monkeypatch, shell_children=[_icon_ctrl()],
                       show_on_click=False, show_on_double=True)
        assert tr.restore_wechat_from_tray(MAIN_HWND, timeout=3.0) is True
        assert env.api.clicks == [ICON_CENTER] * 3
        assert env.api.cursor == (1735, 136)

    def test_picks_largest_icon_button_not_same_name_text_label(
            self, monkeypatch):
        """溢出面板里同名文本标签 37x22 与图标按钮 40x40 并存时选大的。"""
        label = _Ctrl("微信", "", (1666, 978, 1703, 1000))   # 37x22
        button = _icon_ctrl()                                  # 40x40
        env = _install(monkeypatch, shell_children=[label, button])
        assert tr.restore_wechat_from_tray(MAIN_HWND, timeout=1.0) is True
        assert env.api.clicks == [ICON_CENTER]

    def test_already_visible_does_not_touch_desktop(self, monkeypatch):
        env = _install(monkeypatch, main_visible=True,
                       shell_children=[_icon_ctrl()])
        assert tr.restore_wechat_from_tray(MAIN_HWND) is True
        assert env.api.clicks == []

    def test_no_uia_support_fails_without_clicking(self, monkeypatch):
        env = _install(monkeypatch, shell_children=[_icon_ctrl()], has_uia=False)
        assert tr.restore_wechat_from_tray(MAIN_HWND) is False
        assert env.api.clicks == []

    def test_no_chevron_and_no_icon_fails_cleanly(self, monkeypatch):
        env = _install(monkeypatch, has_chevron=False)
        assert tr.restore_wechat_from_tray(MAIN_HWND) is False
        assert env.api.clicks == []

    def test_english_icon_name_supported(self, monkeypatch):
        env = _install(monkeypatch, shell_children=[_icon_ctrl("WeChat")])
        assert tr.restore_wechat_from_tray(MAIN_HWND, timeout=0.5) is True
        assert env.api.clicks == [ICON_CENTER]


# ---------------------------------------------------------------------------
# 2. WeChatGUI.ensure_visible 集成
# ---------------------------------------------------------------------------
class _User32:
    def __init__(self, visible=0):
        self.visible = visible
        self.shown = []

    def IsWindowVisible(self, h):
        return self.visible

    def IsIconic(self, h):
        return False

    def IsWindow(self, h):
        return True

    def ShowWindow(self, h, cmd):
        self.shown.append((h, cmd))

    def GetForegroundWindow(self):
        return 0


def _gui(user32):
    g = object.__new__(WeChatGUI)
    g._input = SimpleNamespace(_user32=user32)
    g.main_hwnd = MAIN_HWND
    g.render_hwnd = 111
    g._last_open_error = ''
    g._last_visible_ok = False
    g._last_visible_ts = 0
    return g


class TestEnsureVisibleAutoRestore:
    def _patch_common(self, g, monkeypatch, touched):
        monkeypatch.setattr(guia_mod.time, "sleep", lambda s: None)
        for name in ("_minimize_blockers", "bring_to_front",
                     "_update_render_rect", "_rebind_if_window_changed"):
            monkeypatch.setattr(
                g, name,
                lambda *a, _n=name, **k: touched.append(_n) or True)
        monkeypatch.setattr(g, "is_alive", lambda: True)

    def test_tray_auto_restore_continues_sending(self, monkeypatch):
        """托盘态点击图标成功 → 清缓存、置前、返回 True，不再 fail-closed。"""
        u = _User32(visible=0)
        g = _gui(u)
        touched = []
        self._patch_common(g, monkeypatch, touched)
        g._uia = object()
        g._current_chat = "家人群"
        g._last_input_box = (1, 2, 3, 4)

        def restored(hwnd, timeout=3.0):
            u.visible = 1                      # 模拟 Qt 自己 show
            return True

        monkeypatch.setattr(tr, "restore_wechat_from_tray", restored)
        assert g.ensure_visible() is True
        assert u.shown == []                   # 绝不 SW_SHOW
        assert "bring_to_front" in touched
        assert "_rebind_if_window_changed" in touched
        assert g._uia is None and g._current_chat is None
        assert g._last_input_box is None
        assert g.last_open_error() == ''
        assert g._was_tray_hidden is False

    def test_tray_restore_failure_still_fails_closed(self, monkeypatch):
        """点不到图标 / 点了没起来：仍是 0 秒失败，一行窗口操作都不做。"""
        u = _User32(visible=0)
        g = _gui(u)
        touched = []
        self._patch_common(g, monkeypatch, touched)
        # conftest 已把唤起函数隔离成 False，这里不再覆盖
        assert g.ensure_visible() is False
        assert u.shown == []
        assert touched == []
        assert g.last_open_error() == 'tray_hidden'
        assert g._was_tray_hidden is True
