# -*- coding: utf-8 -*-
"""UIA 唤醒配额保护 + 「树就绪再放行」回归（2026-11-28 打包版实机日志）。

1.4.7 实机（18:06~18:24）暴露两个独立问题：

  ① 后台预热巡检在用户尚未发送时就连续做 5 次「gate 1→0→1」重触发，
     把 5 次/300s 限流配额烧光（18:13:51 之后所有发送直接判失败）。
     修法：预热传 allow_rearm=False、最多补 1 个字节、退避上限 300s。
  ② ensure_window 只要看到 mmui::MainWindow 类名就返回成功，此时
     MainView/MainTabBar 子控件还没建完（18:20:21 补丁成功，2s 后
     「未找到 MainTabBar」→ 18:20:30 判 tree_not_ready；下一轮补丁后
     4s 切页就成功了）。修法：刚写过补丁时补等 is_ready 最多 10s。

全部使用受控桩，不碰真实微信窗口。
"""
from __future__ import annotations

import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import wechatauto.uia_driver as drv  # noqa: E402
from wechatauto.uia_driver import WeChatUIA  # noqa: E402


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def _bare_uia(monkeypatch):
    """object.__new__ 构造的 uia + 假时钟；窗口枚举/可见性全部打桩。"""
    clk = _Clock()
    monkeypatch.setattr(drv, "time", clk)
    uia = object.__new__(WeChatUIA)
    monkeypatch.setattr(uia, "_restore_window_visible", lambda: True)
    monkeypatch.setattr(uia, "_wechat_hwnds", lambda: [111])
    monkeypatch.setattr(uia, "_find_main", lambda: None)
    monkeypatch.setattr(uia, "is_ready", lambda: False)
    return uia, clk


class TestRearmQuota:
    def test_background_warm_never_rearms(self, monkeypatch):
        """预热路径（allow_rearm=False）：gate=1 也不做 1→0→1 脉冲。"""
        uia, _clk = _bare_uia(monkeypatch)
        wakes = []

        def fake_wake(rearm=False):
            wakes.append(rearm)
            # 模拟 0→1 写入成功；巡检最多补 1 个字节后不得再写。
            uia._last_patch_written = not rearm

        monkeypatch.setattr(uia, "_wake_accessibility", fake_wake)
        monkeypatch.setattr(uia, "_rearm_allowed", lambda: True)

        assert uia._wake_and_wait_ready(12.0, allow_rearm=False) is False
        assert wakes == [False]            # 只普通补丁一次，绝不重触发

    def test_send_path_may_rearm_once(self, monkeypatch):
        """发送路径保留唯一一次受控重触发（gate=1 但树被回收的死锁）。"""
        uia, _clk = _bare_uia(monkeypatch)
        wakes = []

        def fake_wake(rearm=False):
            wakes.append(rearm)
            # gate 本来就是 1（普通补丁零写入）→ 才轮到重触发。
            uia._last_patch_written = bool(rearm)

        monkeypatch.setattr(uia, "_wake_accessibility", fake_wake)
        monkeypatch.setattr(uia, "_rearm_allowed", lambda: True)

        assert uia._wake_and_wait_ready(12.0) is False
        assert wakes.count(True) == 1
        assert wakes.count(False) == 1


class TestEnsureWindowReadyGate:
    def _uia_for_wake_branch(self, monkeypatch):
        uia = object.__new__(WeChatUIA)
        uia.timeout = 15.0
        # 只替换模块引用，不动全局 time.sleep
        monkeypatch.setattr(drv, "time", types.SimpleNamespace(
            time=time.time, monotonic=time.monotonic,
            sleep=lambda s: None))
        monkeypatch.setattr(uia, "_restore_window_visible", lambda: True)
        monkeypatch.setattr(uia, "_login_window", lambda: None)
        monkeypatch.setattr(uia, "_wechat_hwnds", lambda: [111])

        def fake_wait_main(*a, **k):
            uia._win = object()          # _wait_main 只保证根窗口出现

        monkeypatch.setattr(uia, "_wait_main", fake_wait_main)
        return uia

    def test_waits_for_mainview_after_real_patch(self, monkeypatch):
        """刚写过补丁：根窗口出现后继续等 MainView 物化再放行。"""
        uia = self._uia_for_wake_branch(monkeypatch)
        monkeypatch.setattr(uia, "_wake_accessibility",
                            lambda rearm=False: setattr(
                                uia, "_last_patch_written", True))
        calls = {"n": 0}

        def fake_ready():
            calls["n"] += 1
            return calls["n"] >= 3       # 前两次未物化，第三次就绪

        monkeypatch.setattr(uia, "is_ready", fake_ready)
        assert uia.ensure_window() is True
        assert calls["n"] >= 3

    def test_no_extra_wait_when_nothing_written(self, monkeypatch):
        """没写补丁（gate 已 1 / 被限流）：不干等，立即按现状返回。"""
        uia = self._uia_for_wake_branch(monkeypatch)
        monkeypatch.setattr(uia, "_wake_accessibility",
                            lambda rearm=False: setattr(
                                uia, "_last_patch_written", False))
        calls = {"n": 0}

        def fake_ready():
            calls["n"] += 1
            return False

        monkeypatch.setattr(uia, "is_ready", fake_ready)
        assert uia.ensure_window() is False
        assert calls["n"] <= 2          # 不做 10s 轮询


class TestThrottleReason:
    def _gui(self):
        from wechatauto.guia import WeChatGUI
        g = object.__new__(WeChatGUI)
        g._uia = None
        g._uia_tried = False
        g._uia_retry_after = 0.0
        g._last_open_error = ''
        g._get_uia = lambda *a, **k: None
        return g

    def test_fresh_throttle_reported(self, monkeypatch):
        monkeypatch.setattr(WeChatUIA, "_throttled_pid", 9016)
        monkeypatch.setattr(WeChatUIA, "_throttled_at", time.monotonic())
        g = self._gui()
        assert g.open_chat("安佳胤") is False
        assert g.last_open_error() == 'tree_throttled'

    def test_stale_throttle_degrades_to_uia_unavailable(self, monkeypatch):
        monkeypatch.setattr(WeChatUIA, "_throttled_pid", 9016)
        monkeypatch.setattr(WeChatUIA, "_throttled_at",
                            time.monotonic() - 301)
        g = self._gui()
        assert g.open_chat("安佳胤") is False
        assert g.last_open_error() == 'uia_unavailable'
