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
