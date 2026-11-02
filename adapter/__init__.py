# -*- coding: utf-8 -*-
"""微信适配程序 — 基于 wechatauto-replica 本地代码构建。

将项目根目录加入 sys.path，确保导入的是本地 wechatauto 包（已修复 bug）
而不是 pip 安装的版本。
"""

import os, sys

# PyInstaller 打包后不需要手动设置 sys.path（模块已被打包）
if not getattr(sys, 'frozen', False):
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)
