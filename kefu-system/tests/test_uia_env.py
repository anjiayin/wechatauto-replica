# -*- coding: utf-8 -*-
"""WeChatUIA 环境自动配置（部署客户机零手工配置的保障）。

_ensure_user_env 把 QT_ACCESSIBILITY=1 写进 HKCU\\Environment 并广播
WM_SETTINGCHANGE——客户机上用户手动启动的微信才能继承。这里全部打桩，
不读写真实注册表、不发真实广播。
"""
from __future__ import annotations

import os
import sys

# 仓库根（wechatauto 包父级）——与 test_batch13_openim_aid.py 同一写法
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import winreg

from wechatauto.uia_driver import WeChatUIA


class TestEnsureUserEnv:
    def test_already_set_noop(self, monkeypatch):
        """注册表已是 1：不重写、不广播（幂等）。"""
        calls = {"set": 0, "broadcast": 0}
        monkeypatch.setattr(winreg, "QueryValueEx",
                            lambda k, n: ("1", winreg.REG_SZ))
        monkeypatch.setattr(
            winreg, "SetValueEx",
            lambda *a, **k: calls.__setitem__("set", calls["set"] + 1))
        monkeypatch.setattr(
            WeChatUIA, "_broadcast_env_change",
            staticmethod(
                lambda: calls.__setitem__("broadcast",
                                          calls["broadcast"] + 1)))
        assert WeChatUIA._ensure_user_env() is True
        assert calls == {"set": 0, "broadcast": 0}

    def test_missing_value_writes_and_broadcasts(self, monkeypatch):
        """注册表没有该变量：写 REG_SZ "1" 并广播一次。"""
        written = {}
        monkeypatch.setattr(
            winreg, "QueryValueEx",
            lambda k, n: (_ for _ in ()).throw(FileNotFoundError()))
        monkeypatch.setattr(
            winreg, "SetValueEx",
            lambda k, name, reserved, typ, val:
            written.__setitem__("name", name) or written.__setitem__(
                "type", typ) or written.__setitem__("value", val))
        broadcasts = []
        monkeypatch.setattr(
            WeChatUIA, "_broadcast_env_change",
            staticmethod(lambda: broadcasts.append(1)))
        assert WeChatUIA._ensure_user_env() is True
        assert written == {"name": "QT_ACCESSIBILITY", "type": winreg.REG_SZ,
                           "value": "1"}
        assert len(broadcasts) == 1

    def test_value_with_spaces_counts_as_set(self, monkeypatch):
        """值带空格（如 " 1 "）也算已设置，不重写。"""
        calls = {"set": 0}
        monkeypatch.setattr(winreg, "QueryValueEx",
                            lambda k, n: (" 1 ", winreg.REG_SZ))
        monkeypatch.setattr(
            winreg, "SetValueEx",
            lambda *a, **k: calls.__setitem__("set", calls["set"] + 1))
        monkeypatch.setattr(WeChatUIA, "_broadcast_env_change",
                            staticmethod(lambda: None))
        assert WeChatUIA._ensure_user_env() is True
        assert calls["set"] == 0

    def test_registry_error_returns_false(self, monkeypatch):
        """打不开注册表（权限等）：返回 False、不抛异常。"""
        monkeypatch.setattr(
            winreg, "OpenKey",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
        assert WeChatUIA._ensure_user_env() is False

    def test_qt_accessibility_process_env(self, monkeypatch):
        """进程环境兜底：未设时写 os.environ。"""
        monkeypatch.delenv("QT_ACCESSIBILITY", raising=False)
        WeChatUIA._ensure_qt_accessibility()
        assert os.environ.get("QT_ACCESSIBILITY") == "1"


class TestWakeAccessibilityThrottle:
    """热激活限流：防微信 4.1.15 反复补丁导致的 Qt 输入假死（实测事故）。"""

    def _mk(self, monkeypatch):
        uia = object.__new__(WeChatUIA)
        monkeypatch.setattr(uia, "_wechat_hwnds", lambda: [111])
        monkeypatch.setattr(uia, "_pid_from_hwnd", lambda h: 4242)
        calls = []
        monkeypatch.setattr(uia, "_hot_activate_accessibility",
                            lambda h: calls.append(h) or True)
        return uia, calls

    def test_cooldown_skips_rapid_repate(self, monkeypatch):
        monkeypatch.setattr(WeChatUIA, "_patch_log", {})
        uia, calls = self._mk(monkeypatch)
        assert uia._wake_accessibility() is True
        assert uia._wake_accessibility() is False  # 冷却期内：跳过不补
        assert len(calls) == 1

    def test_max_per_pid_stops_churn(self, monkeypatch):
        monkeypatch.setattr(WeChatUIA, "_patch_log", {})
        monkeypatch.setattr(WeChatUIA, "PATCH_COOLDOWN", 0.0)
        monkeypatch.setattr(WeChatUIA, "PATCH_MAX_PER_PID", 3)
        uia, calls = self._mk(monkeypatch)
        for _ in range(5):
            uia._wake_accessibility()
        assert len(calls) == 3  # 达到上限后不再写内存

    def test_counter_resets_after_long_idle(self, monkeypatch):
        import time as _time
        monkeypatch.setattr(WeChatUIA, "_patch_log", {})
        monkeypatch.setattr(WeChatUIA, "PATCH_COOLDOWN", 0.0)
        monkeypatch.setattr(WeChatUIA, "PATCH_MAX_PER_PID", 1)
        uia, calls = self._mk(monkeypatch)
        uia._wake_accessibility()
        uia._wake_accessibility()
        assert len(calls) == 1
        # 模拟 300s 无补丁（可能已换代）→ 计数重置，允许再次补丁
        WeChatUIA._patch_log[4242][1] = _time.monotonic() - 400
        uia._wake_accessibility()
        assert len(calls) == 2

    def test_failure_does_not_consume_quota(self, monkeypatch):
        monkeypatch.setattr(WeChatUIA, "_patch_log", {})
        monkeypatch.setattr(WeChatUIA, "PATCH_COOLDOWN", 0.0)
        uia = object.__new__(WeChatUIA)
        monkeypatch.setattr(uia, "_wechat_hwnds", lambda: [111])
        monkeypatch.setattr(uia, "_pid_from_hwnd", lambda h: 4242)
        monkeypatch.setattr(uia, "_hot_activate_accessibility",
                            lambda h: False)  # 补丁失败
        assert uia._wake_accessibility() is False
        assert WeChatUIA._patch_log[4242][0] == 0  # 失败不计数


class TestWarmTreeColdPath:
    """warm_tree 冷树路径：_find_main 返回 None（树未物化、类名还是 Qt
    外壳）时必须走热激活+等待，而不是直接判失败。

    2026-11-14 实测踩坑：旧实现 `_find_main() is None → return False`，
    导致预热在冷树场景下永远空转，首次发送仍要付出 15~30 秒物化耗时。
    """

    def _uia(self):
        return object.__new__(WeChatUIA)

    def test_cold_tree_goes_through_wake(self, monkeypatch):
        uia = self._uia()
        # 第一轮 _find_main None（冷树）→ 补丁路径里物化后再找到
        findings = iter([None])
        monkeypatch.setattr(uia, "_find_main", lambda: next(findings, "main"))
        monkeypatch.setattr(uia, "is_ready", lambda: True)
        called = []
        monkeypatch.setattr(uia, "_wake_and_wait_ready",
                            lambda timeout: called.append(timeout) or True)
        assert uia.warm_tree(timeout=30.0) is True
        assert called == [30.0]  # 冷树必须走补丁等待路径

    def test_ready_tree_short_circuits(self, monkeypatch):
        """树已就绪：零操作直接成功，不碰补丁。"""
        uia = self._uia()
        monkeypatch.setattr(uia, "_find_main", lambda: "main")
        monkeypatch.setattr(uia, "is_ready", lambda: True)
        monkeypatch.setattr(
            uia, "_wake_and_wait_ready",
            lambda timeout: (_ for _ in ()).throw(
                AssertionError("就绪树不应走补丁路径")))
        assert uia.warm_tree(timeout=30.0) is True

    def test_wake_failure_propagates_false(self, monkeypatch):
        """补丁路径失败：返回 False（预热退避，不影响发送兜底）。"""
        uia = self._uia()
        monkeypatch.setattr(uia, "_find_main", lambda: None)
        monkeypatch.setattr(uia, "_wake_and_wait_ready",
                            lambda timeout: False)
        assert uia.warm_tree(timeout=30.0) is False
