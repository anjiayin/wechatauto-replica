# -*- coding: utf-8 -*-
"""托盘/最小化恢复后的发送安全回归（2026-11-15 09:54 / 09:57 实测事故）。

事故复盘
--------
1. 微信「关闭到托盘」后，Qt 回收 UIA 控件树却把 accessibility gate 留在 1：
   热激活读到 1 返回 'already' 什么都不做 → 干等 30s 超时 → 降级截图/OCR
   → render_rect 是 160x28 伪矩形 → ``Coordinate 'right' is less than
   'left'`` 崩溃，白等 56.9s。
2. 最小化恢复后主窗口句柄变了（4000292 → 1378074），``_current_chat``
   缓存却仍指向旧会话；``send_msg`` 拿缓存判 already_open 跳过实时校验，
   把消息打进别的会话 → DB 回读查不到 → 「已操作发送，但数据库未确认」
   还被上层当成成功标记 replied。

修复后的硬约束（本文件锁死）
----------------------------
- 发送链路**只走 UIA**；UIA 不可用/失败一律 fail-closed，绝不调用任何
  截图、OCR、输入框探测、布局校准方法。
- 发送前用 ``uia.current_chat()`` 实时校验会话，缓存不可信；重新打开后
  还要二次校验，仍不匹配就拒绝发送。
- 响应 ``data.chat_verified`` 标记会话是否实时校验过，sender 只在 True 时
  才把「DB 未确认」按已回复处理。
- 句柄漂移后重新绑定并清空会话/输入框缓存。
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


SCREENSHOT_METHODS = (
    "get_input_box", "ocr", "ocr_zoomed", "input_text", "click_send",
    "ensure_visible", "calibrate_layout", "_grab_screen", "_probe_input_box",
    "_append_text_ocr", "_at_member_ocr", "find_session", "_search_chat",
    "_find_quote_menu_point", "_last_message_y", "_locate_last_message_pt",
)


def _gui_without_screenshots(monkeypatch, uia=None):
    """构造不碰真实窗口的 GUI；任何截图路径被调用都会记录下来。"""
    g = object.__new__(WeChatGUI)
    called = []
    monkeypatch.setattr(g, "_get_uia", lambda: uia)
    monkeypatch.setattr(guia_mod, "OCR_FALLBACK_ENABLED", False)
    for name in SCREENSHOT_METHODS:
        monkeypatch.setattr(
            g, name,
            lambda *a, _n=name, **k: called.append(_n) or None)
    return g, called


class _Uia:
    """UIA 引擎替身：可配置当前会话、重新打开结果、发送结果。"""

    def __init__(self, current="家人群", reopen_ok=True,
                 after_reopen=None, send_ok=True, ready=True):
        self.current = current
        self.reopen_ok = reopen_ok
        self.after_reopen = after_reopen
        self.send_ok = send_ok
        self.ready = ready
        self.reopens = []
        self.sends = []

    def current_chat(self):
        return self.current

    def same_chat(self, cur, target):
        return bool(cur) and bool(target) and (
            cur == target or cur.startswith(target) or target.startswith(cur))

    def open_chat(self, who, prefer_openim=False, **kw):
        self.reopens.append((who, prefer_openim))
        if self.reopen_ok and self.after_reopen is not None:
            self.current = self.after_reopen
        return self.reopen_ok

    def openim_label(self):
        return None

    def is_ready(self):
        return self.ready

    def send_text(self, text, who=None):
        self.sends.append(text)
        return self.send_ok

    def quote_message(self, text, who=None, target_text=None):
        self.sends.append(("quote", text))
        return self.send_ok

    def at_members(self, names, text, who=None, quote=False,
                   quote_target_text=None):
        self.sends.append(("at", names, text))
        return self.send_ok

    def fill_text(self, text):
        self.sends.append(("fill", text))
        return self.send_ok

    def dismiss_quote_menu(self):
        pass


# ---------------------------------------------------------------------------
# 1. 发送链路 fail-closed：UIA 不可用不做任何截图/OCR
# ---------------------------------------------------------------------------
class TestSendFailClosed:
    @pytest.mark.parametrize("method,args,kwargs", [
        ("send_msg", ("你好",), {"who": "家人群"}),
        ("quote_msg", ("你好",), {"who": "家人群"}),
        ("at_members", (["张三"], "你好"), {"who": "家人群"}),
        ("at_members", (["张三"], "你好"),
         {"who": "家人群", "quote": True}),
        ("open_chat", ("家人群",), {}),
        ("open_chat_prefer_openim", ("安佳胤",), {}),
        ("input_only", ("你好",), {"who": "家人群"}),
    ])
    def test_uia_unavailable_fails_without_screenshots(
            self, monkeypatch, method, args, kwargs):
        g, called = _gui_without_screenshots(monkeypatch, uia=None)
        resp = getattr(g, method)(*args, **kwargs)
        if method in ("open_chat", "open_chat_prefer_openim"):
            assert resp is False
            assert g._last_open_error == 'uia_unavailable'
        else:
            assert resp.is_success is False
            assert "已阻止发送" in str(resp.get("message", ""))
            assert resp.get("data", {}).get("chat_verified") is False
        assert called == [], f"不该走截图/OCR 路径，实际调用：{called}"

    def test_uia_send_failure_does_not_fall_back_to_ocr(self, monkeypatch):
        """UIA 在但输入失败：直接判失败，不重试、不截图。"""
        uia = _Uia(send_ok=False)
        g, called = _gui_without_screenshots(monkeypatch, uia=uia)
        resp = g.send_msg("你好", who="家人群", verify=False)
        assert resp.is_success is False
        assert resp.get("data", {}).get("chat_verified") is False
        assert uia.sends == ["你好"]
        assert called == []

    def test_quote_failure_cleans_but_does_not_fall_back(self, monkeypatch):
        uia = _Uia(send_ok=False)
        cleaned = []
        monkeypatch.setattr(uia, "dismiss_quote_menu",
                            lambda: cleaned.append(1))
        g, called = _gui_without_screenshots(monkeypatch, uia=uia)
        resp = g.quote_msg("你好", who="家人群")
        assert resp.is_success is False
        assert cleaned == [1]          # 清场仍要做，避免污染下一次
        assert called == []            # 但不降级 OCR

    def test_at_failure_does_not_fall_back(self, monkeypatch):
        uia = _Uia(send_ok=False)
        g, called = _gui_without_screenshots(monkeypatch, uia=uia)
        resp = g.at_members(["张三"], "你好", who="家人群")
        assert resp.is_success is False
        assert called == []


# ---------------------------------------------------------------------------
# 2. 发送前实时会话校验（杜绝发错会话）
# ---------------------------------------------------------------------------
class TestRealtimeChatVerification:
    def test_stale_cache_cannot_bypass_verification(self, monkeypatch):
        """_current_chat 说是目标、UIA 实时却是别的会话 → 拒绝发送。"""
        uia = _Uia(current="同事群", reopen_ok=True,
                   after_reopen="同事群")   # 重开后仍不匹配
        g, called = _gui_without_screenshots(monkeypatch, uia=uia)
        g._current_chat = "家人群"        # 托盘恢复前的陈旧缓存
        resp = g.send_msg("你好", who="家人群", verify=False)
        assert resp.is_success is False
        assert resp.get("data", {}).get("chat_verified") is False
        assert uia.sends == [], "会话没确认就绝不能往输入框打字"
        assert uia.reopens == [("家人群", False)]
        assert called == []

    def test_reopen_then_second_verification_passes(self, monkeypatch):
        uia = _Uia(current="同事群", reopen_ok=True,
                   after_reopen="家人群")
        g, called = _gui_without_screenshots(monkeypatch, uia=uia)
        monkeypatch.setattr(guia_mod.time, "sleep", lambda s: None)
        g._current_chat = "家人群"
        resp = g.send_msg("你好", who="家人群", verify=False)
        assert resp.is_success is True
        assert resp.get("data", {}).get("chat_verified") is True
        assert uia.sends == ["你好"]
        assert called == []

    def test_openim_mismatch_refuses_without_reopen(self, monkeypatch):
        """企业微信同名联系人：本层不做二次 open_chat，免得开成同名好友。"""
        uia = _Uia(current="同名好友")
        g, called = _gui_without_screenshots(monkeypatch, uia=uia)
        resp = g.send_msg("你好", who="安佳胤",
                          who_username="anjy@openim", verify=False)
        assert resp.is_success is False
        assert uia.reopens == []
        assert uia.sends == []
        assert called == []

    def test_target_chat_is_real_time_on_every_send(self, monkeypatch):
        """连续两次发送都要实时读一次 current_chat，不能只信缓存。"""
        uia = _Uia(current="家人群")
        reads = []
        orig = uia.current_chat
        uia.current_chat = lambda: reads.append(1) or orig()
        g, _ = _gui_without_screenshots(monkeypatch, uia=uia)
        assert g.send_msg("一", who="家人群", verify=False).is_success
        assert g.send_msg("二", who="家人群", verify=False).is_success
        assert len(reads) == 2


# ---------------------------------------------------------------------------
# 3. 句柄漂移 / 缓存污染
# ---------------------------------------------------------------------------
class TestWindowHandleDrift:
    def test_rebind_refreshes_window_and_clears_stale_cache(self, monkeypatch):
        g = object.__new__(WeChatGUI)
        g.main_hwnd = 4000292
        g._primary_hwnd = 4000292
        g.render_hwnd = 111
        g.render_rect = (0, 0, 160, 28)      # 托盘伪矩形
        g.pid = 10856
        g._current_chat = "家人群"
        g._last_input_box = (1, 2, 3, 4)
        g._uia = object()
        g._uia_tried = True
        g._uia_retry_after = 999.0
        updated = []
        monkeypatch.setattr(g, "_find_main_window", lambda title: 1378074)
        monkeypatch.setattr(g, "_find_render_window", lambda h: 222)
        monkeypatch.setattr(g, "_update_render_rect",
                            lambda: updated.append(1))
        monkeypatch.setattr(g, "_get_pid", lambda h: 10856)

        assert g._rebind_if_window_changed() is True
        assert g.main_hwnd == 1378074
        assert g._primary_hwnd == 1378074
        assert g.render_hwnd == 222
        assert updated == [1]
        assert g._current_chat is None
        assert g._last_input_box is None
        assert g._uia is None
        assert g._uia_tried is False
        assert g._uia_retry_after == 0

    def test_no_rebind_when_handle_unchanged(self, monkeypatch):
        g = object.__new__(WeChatGUI)
        g.main_hwnd = 1378074
        g._current_chat = "家人群"
        monkeypatch.setattr(g, "_find_main_window", lambda title: 1378074)
        assert g._rebind_if_window_changed() is False
        assert g._current_chat == "家人群"


# ---------------------------------------------------------------------------
# 4. 截图坐标防御（即使 OCR 被禁用，残留路径也不能再冒泡异常）
# ---------------------------------------------------------------------------
class TestScreenshotCoordinateGuard:
    def test_invalid_bbox_fails_fast_without_retry(self, monkeypatch):
        from PIL import ImageGrab
        calls = []
        monkeypatch.setattr(ImageGrab, "grab",
                            lambda **kw: calls.append(kw) or None)
        g = object.__new__(WeChatGUI)
        with pytest.raises(RuntimeError, match="截图区域无效"):
            g._grab_screen((100, 100, 60, 300))
        assert calls == []

    def test_input_box_has_text_invalid_rect_returns_false(self, monkeypatch):
        g = object.__new__(WeChatGUI)
        called = []
        monkeypatch.setattr(g, "_grab_screen",
                            lambda box: called.append(box) or None)
        # 托盘伪矩形：x1-20 < x0+20
        assert g._input_box_has_text((0, 0, 30, 20)) is False
        assert called == []


# ---------------------------------------------------------------------------
# 5. gate=1 但树被回收：受控 0→1 重触发 + 限流
# ---------------------------------------------------------------------------
class _FakeKernel32:
    def __init__(self):
        self.OpenProcess = lambda *a, **k: 4242
        self.CloseHandle = lambda *a, **k: True


def _patchable_uia(monkeypatch, gate=1):
    """绕过真实 Win32 进程句柄，只测 gate 读写语义。"""
    uia = object.__new__(WeChatUIA)
    writes = []
    monkeypatch.setattr(drv, "ctypes", SimpleNamespace(
        windll=SimpleNamespace(kernel32=_FakeKernel32())))
    monkeypatch.setattr(uia, "_pid_from_hwnd", lambda h: 10856)
    monkeypatch.setattr(uia, "_weixin_dll_module",
                        lambda pid: (0x1000, 0, r"C:\Weixin\Weixin.dll"))
    monkeypatch.setattr(uia, "_qaccessible_active_rva", lambda path: 0xB39C9D8)
    monkeypatch.setattr(uia, "_read_process_byte",
                        lambda handle, addr: gate)
    monkeypatch.setattr(uia, "_write_process_byte",
                        lambda handle, addr, v: writes.append(v) or True)
    monkeypatch.setattr(drv.time, "sleep", lambda s: None)
    return uia, writes


class TestGateRearm:
    def test_already_does_not_write(self, monkeypatch):
        uia, writes = _patchable_uia(monkeypatch, gate=1)
        assert uia._hot_activate_accessibility(111) == 'already'
        assert writes == []

    def test_rearm_pulses_1_0_1(self, monkeypatch):
        uia, writes = _patchable_uia(monkeypatch, gate=1)
        assert uia._hot_activate_accessibility(111, rearm=True) == 'patched'
        assert writes == [0, 1]

    def test_normal_patch_writes_once(self, monkeypatch):
        uia, writes = _patchable_uia(monkeypatch, gate=0)
        assert uia._hot_activate_accessibility(111) == 'patched'
        assert writes == [1]

    def test_rearm_cooldown_limits_churn(self, monkeypatch):
        uia = object.__new__(WeChatUIA)
        monkeypatch.setattr(WeChatUIA, "_rearm_log", {})
        # 重触发现在还要求「距任何一次真实补丁 ≥45s」，隔离类级日志
        monkeypatch.setattr(WeChatUIA, "_patch_log", {})
        monkeypatch.setattr(uia, "_wechat_hwnds", lambda: [111])
        monkeypatch.setattr(uia, "_pid_from_hwnd", lambda h: 10856)
        now = {"t": 1000.0}
        monkeypatch.setattr(drv.time, "monotonic", lambda: now["t"])
        assert uia._rearm_allowed() is True
        assert uia._rearm_allowed() is False        # 45s 内不允许第二次
        now["t"] += 46
        assert uia._rearm_allowed() is True

    def test_invisible_window_aborts_immediately(self, monkeypatch):
        """窗口不可见时立刻失败，不干等 30 秒。"""
        uia = object.__new__(WeChatUIA)
        monkeypatch.setattr(uia, "_restore_window_visible", lambda: False)
        monkeypatch.setattr(uia, "_wake_accessibility",
                            lambda *a, **k: pytest.fail("不可见时不应打补丁"))
        assert uia._wake_and_wait_ready(timeout=30) is False
