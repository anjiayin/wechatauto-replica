# -*- coding: utf-8 -*-
"""微信适配程序配置 - 支持 wx_adapter_config.json 外部配置"""

import os
import json
import sys

# ===== 默认配置 =====
_DEFAULTS = {
    "ws_host": "127.0.0.1",
    "ws_port": 3002,
    "poll_interval": 5.0,
    "send_lock_timeout": 10,
    "send_retry_count": 3,
    "send_verify": True,
    "send_min_interval": 2.0,
    "log_level": "INFO",
    "reconnect_interval": 5
}

def _find_config_file():
    """按优先级查找配置文件 wx_adapter_config.json"""
    candidates = []

    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        candidates.append(os.path.join(exe_dir, 'wx_adapter_config.json'))

    candidates.append(os.path.join(os.getcwd(), 'wx_adapter_config.json'))

    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(pkg_dir, 'wx_adapter_config.json'))

    project_root = os.path.dirname(pkg_dir)
    candidates.append(os.path.join(project_root, 'wx_adapter_config.json'))

    for path in candidates:
        if os.path.isfile(path):
            return path

    return None


def _load_config():
    """加载配置文件，合并默认值"""
    config = dict(_DEFAULTS)
    config_path = _find_config_file()

    if config_path:
        try:
            with open(config_path, 'r', encoding='utf-8-sig') as f:
                user_config = json.load(f)
            config.update(user_config)
            print('[CONFIG] 已加载配置文件: ' + config_path)
        except Exception as e:
            print('[CONFIG] 配置文件读取失败: ' + str(e) + '，使用默认配置')
    else:
        default_path = os.path.join(os.getcwd(), 'wx_adapter_config.json')
        try:
            with open(default_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            print('[CONFIG] 已生成默认配置文件: ' + default_path)
        except Exception:
            pass

    return config


_config = _load_config()

# ===== 对外暴露的配置项 =====
WS_HOST = _config.get("ws_host", _DEFAULTS["ws_host"])
WS_PORT = int(_config.get("ws_port", _DEFAULTS["ws_port"]))
POLL_INTERVAL = float(_config.get("poll_interval", _DEFAULTS["poll_interval"]))
SEND_LOCK_TIMEOUT = int(_config.get("send_lock_timeout", _DEFAULTS["send_lock_timeout"]))
SEND_RETRY_COUNT = int(_config.get("send_retry_count", _DEFAULTS["send_retry_count"]))
SEND_VERIFY = bool(_config.get("send_verify", _DEFAULTS["send_verify"]))
SEND_MIN_INTERVAL = float(_config.get("send_min_interval", _DEFAULTS["send_min_interval"]))
LOG_LEVEL = str(_config.get("log_level", _DEFAULTS["log_level"]))
RECONNECT_INTERVAL = int(_config.get("reconnect_interval", _DEFAULTS["reconnect_interval"]))
