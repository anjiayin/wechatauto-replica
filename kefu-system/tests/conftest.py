# -*- coding: utf-8 -*-
"""pytest 全局隔离：任何测试都不得真的点击桌面系统托盘图标。

生产里微信关到托盘时，:func:`wechatauto.tray_restore.restore_wechat_from_tray`
会用**真实鼠标**单击任务栏「微信」图标，让 Qt 自己 show 主窗口。这对单元
测试是必须隔离的桌面副作用。

本 fixture 默认注入「自动唤起失败」，于是原有托盘用例继续锁住
fail-closed 兜底语义；需要验证「唤起成功」的用例在自己内部把该函数
换回真实实现或直接打桩成 True（见 ``test_tray_restore.py``）。
"""
from __future__ import annotations

import os
import sys

# 仓库根（wechatauto 包父级）——与测试模块里的 sys.path 写法一致，
# conftest 先于测试模块加载，必须自己先加，否则 import 不到 wechatauto。
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import pytest                                     # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_desktop_tray_click(monkeypatch):
    from wechatauto import tray_restore
    monkeypatch.setattr(tray_restore, "restore_wechat_from_tray",
                        lambda *a, **k: False)
